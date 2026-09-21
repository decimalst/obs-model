#!/usr/bin/env python3
"""Execute pinned collector binaries, VRL tests, and shared Redis quota tests."""

import datetime as dt
import json
import os
import subprocess
import time

import yaml
from render import ROOT, render


def run(args, **kwargs):
    print("+", " ".join(map(str, args)), flush=True)
    try:
        return subprocess.run(list(map(str, args)), check=True, **kwargs)
    except subprocess.CalledProcessError as exc:
        if exc.stderr:
            print(str(exc.stderr)[-4000:], flush=True)
        raise


def vector_tests():
    config = yaml.safe_load((ROOT / "build/configs/vector-agent/vector.yaml").read_text())
    config["sources"] = {"kubernetes": {"type": "stdin"}}
    config["sinks"] = {"out": {"type": "blackhole", "inputs": ["cleanup"]}}
    config["data_dir"] = "/tmp/vector"
    now = dt.datetime.now(dt.timezone.utc)
    stamp = lambda h: (now + dt.timedelta(hours=h)).isoformat()  # noqa: E731
    prefix = "observability.good-olly.io/"
    scenarios = [
        ("ordinary-error", "ERROR", {}, True),
        ("debug-default-denied", "DEBUG", {}, False),
        ("trace-default-denied", "TRACE", {}, False),
        ("debug-active", "DEBUG", {"debug-from": stamp(-1), "debug-until": stamp(7)}, True),
        ("debug-expired", "DEBUG", {"debug-from": stamp(-8), "debug-until": stamp(-0.01)}, False),
        ("debug-overlong", "DEBUG", {"debug-from": stamp(-1), "debug-until": stamp(24)}, False),
        ("debug-malformed", "DEBUG", {"debug-from": "bad", "debug-until": "bad"}, False),
        ("debug-future", "DEBUG", {"debug-from": stamp(1), "debug-until": stamp(2)}, False),
    ]
    config["tests"] = []
    for name, level, tags, keep in scenarios:
        test = {
            "name": name,
            "inputs": [
                {
                    "insert_at": "identify",
                    "type": "log",
                    "log_fields": {
                        "message": json.dumps(
                            {"level": level, "message": "test", "service_key": "spoofed/id"}
                        ),
                        "kubernetes": {
                            "pod_namespace": "apps",
                            "pod_labels": {"observability/service": "api"},
                            "pod_annotations": {prefix + k: v for k, v in tags.items()},
                        },
                    },
                }
            ],
        }
        if keep:
            test["outputs"] = [
                {
                    "extract_from": "cleanup",
                    "conditions": [
                        {
                            "type": "vrl",
                            "source": (
                                'assert_eq!(.service_key, "apps/api")\n'
                                "assert!(!exists(.debug_allowed))"
                            ),
                        }
                    ],
                }
            ]
        else:
            test["no_outputs_from"] = ["cleanup"]
        config["tests"].append(test)
    path = ROOT / "build/vector-tests.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    return path


def main():
    render()
    values = yaml.safe_load((ROOT / "charts/platform/values.yaml").read_text())
    for role in ["vector-agent", "vector-gateway", "vector-writer"]:
        run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "-e",
                "VECTOR_SELF_NODE_NAME=test",
                "-e",
                "CLICKHOUSE_PASSWORD=validation",
                "-v",
                f"{ROOT}/build/configs:/configs:ro",
                values["images"]["vector"],
                "validate",
                "--no-environment",
                "--skip-healthchecks",
                f"/configs/{role}/vector.yaml",
            ]
        )
    credentials = ROOT / "build/serviceaccount"
    credentials.mkdir(exist_ok=True)
    run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(credentials / "test.key"),
            "-out",
            str(credentials / "ca.crt"),
            "-days",
            "1",
            "-subj",
            "/CN=runtime-test",
        ],
        capture_output=True,
    )
    (credentials / "token").write_text("runtime-test-fixture")
    for role in ["agent", "metrics", "router", "traces"]:
        run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "-e",
                "K8S_NODE_NAME=test",
                "-v",
                f"{credentials}:/var/run/secrets/kubernetes.io/serviceaccount:ro",
                "-v",
                f"{ROOT}/build/configs:/configs:ro",
                values["images"]["otel"],
                "validate",
                f"--config=/configs/otel-{role}/collector.yaml",
            ]
        )
    run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "-v",
            f"{ROOT}/build/configs:/configs:ro",
            "grafana/tempo:2.9.0",
            "-config.file=/configs/tempo-config/tempo.yaml",
            "-config.verify=true",
        ]
    )
    for target in [
        "distributor",
        "ingester",
        "compactor",
        "querier",
        "query-frontend",
        "query-scheduler",
        "store-gateway",
        "ruler",
        "alertmanager",
    ]:
        run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "-v",
                f"{ROOT}/build/configs:/configs:ro",
                "grafana/mimir:3.2.0",
                "-config.file=/configs/mimir-config/mimir.yaml",
                f"-target={target}",
                *(
                    ["-alertmanager.sharding-ring.instance-availability-zone=a"]
                    if target == "alertmanager"
                    else []
                ),
                "-print.config",
            ],
            capture_output=True,
        )
    vector_tests()
    run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "-v",
            f"{ROOT}/build:/tests:ro",
            values["images"]["vector"],
            "test",
            "/tests/vector-tests.yaml",
        ]
    )
    result = run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--label",
            "observability.test=quota",
            "-p",
            "127.0.0.1::6379",
            "valkey/valkey:8.0.6-alpine",
        ],
        capture_output=True,
        text=True,
    )
    container = result.stdout.strip()
    try:
        address = run(
            ["docker", "port", container, "6379/tcp"], capture_output=True, text=True
        ).stdout.strip()
        for _ in range(30):
            ready = subprocess.run(
                ["docker", "exec", container, "valkey-cli", "ping"], capture_output=True, text=True
            )
            if ready.returncode == 0 and "PONG" in ready.stdout:
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("Valkey did not become ready")
        go = os.environ.get(
            "GO",
            str(ROOT / ".cache/tools/go/bin/go")
            if (ROOT / ".cache/tools/go/bin/go").exists()
            else "go",
        )
        env = dict(os.environ, TEST_REDIS_ADDR=address)
        run(
            [go, "test", "-race", "-count=1", "-v", "./..."],
            cwd=ROOT / "services/log-quota",
            env=env,
        )
    finally:
        run(["docker", "rm", "-f", container], capture_output=True)
    print("Runtime validation passed; no cloud resources were created.")


if __name__ == "__main__":
    main()
