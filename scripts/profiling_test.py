#!/usr/bin/env python3
"""Native config/flag validation; does not claim eBPF capture or a live S3/Raft cluster."""

import subprocess
import time
import uuid
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def run(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=90, **kwargs)


def main():
    name = "olly-profile-" + uuid.uuid4().hex[:8]
    directory = ROOT / "build" / name
    directory.mkdir(parents=True)
    documents = [
        d
        for d in yaml.safe_load_all(
            run(
                [
                    "helm",
                    "template",
                    "olly",
                    "charts/platform",
                    "-n",
                    "observability",
                    "--set",
                    "profiling.enabled=true",
                    "--set",
                    "hubble.enabled=true",
                ]
            ).stdout
        )
        if d
    ]
    (directory / "manifests.yaml").write_text(yaml.safe_dump_all(documents))
    objects = {(d["kind"], d["metadata"]["name"]): d for d in documents}
    alloy = objects["ConfigMap", "alloy-profiles"]["data"]["config.alloy"]
    (directory / "config.alloy").write_text(alloy)
    image = objects["DaemonSet", "alloy-profiles"]["spec"]["template"]["spec"]["containers"][0][
        "image"
    ]
    run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "-v",
            f"{directory}:/config:ro",
            image,
            "validate",
            "/config/config.alloy",
        ]
    )
    invalid = alloy.replace("sample_rate = 19", "nonexistent_setting = 19")
    (directory / "invalid.alloy").write_text(invalid)
    failure = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "-v",
            f"{directory}:/config:ro",
            image,
            "validate",
            "/config/invalid.alloy",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert failure.returncode != 0 and "nonexistent_setting" in failure.stderr + failure.stdout
    config = objects["ConfigMap", "pyroscope-config"]["data"]["config.yaml"]
    (directory / "pyroscope.yaml").write_text(config)
    container = objects["StatefulSet", "pyroscope-metastore"]["spec"]["template"]["spec"][
        "containers"
    ][0]
    # API-only target validates the production YAML and metastore flags without S3/IAM/Raft.
    args = [
        a.replace("/etc/pyroscope/config.yaml", "/config/pyroscope.yaml")
        for a in container["args"]
        if not a.startswith(("-runtime-config.file=", "-target="))
    ]
    try:
        run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "--label",
                "observability.test=profiling",
                "--network",
                "none",
                "-v",
                f"{directory}:/config:ro",
                container["image"],
                *args,
                "-target=api",
            ]
        )
        for _ in range(30):
            log_result = run(["docker", "logs", name])
            logs = log_result.stdout + log_result.stderr
            if "Pyroscope started" in logs:
                break
            status = run(
                ["docker", "inspect", name, "--format", "{{.State.Running}}"]
            ).stdout.strip()
            if status != "true":
                raise AssertionError(
                    run(["docker", "logs", name]).stdout + run(["docker", "logs", name]).stderr
                )
            time.sleep(1)
        else:
            raise AssertionError("Pyroscope configuration did not start: " + logs)
        print(
            "Pinned Alloy validates positive/negative configs; "
            "Pyroscope parses v2 YAML/flags and starts its server."
        )
    finally:
        subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True)
    scrape = objects["OpenTelemetryCollector", "otel-scrape"]["spec"]["config"]
    (directory / "scrape.yaml").write_text(yaml.safe_dump(scrape))
    collector = objects["OpenTelemetryCollector", "otel-scrape"]["spec"]["image"]
    run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "-v",
            f"{directory}:/config:ro",
            collector,
            "validate",
            "--config=/config/scrape.yaml",
        ]
    )
    kubeconform = ROOT / ".cache/tools/kubeconform"
    if not kubeconform.exists():
        kubeconform = Path("kubeconform")
    cilium = run(["helm", "template", "cilium", "integrations/cilium", "-n", "kube-system"])
    (directory / "cilium.yaml").write_text(cilium.stdout)
    result = run(
        [
            str(kubeconform),
            "-strict",
            "-summary",
            "-kubernetes-version",
            "1.35.0",
            "-ignore-missing-schemas",
            str(directory / "manifests.yaml"),
            str(directory / "cilium.yaml"),
        ]
    )
    print(result.stdout.strip())
    print(
        "Hubble scrape config validates. Kernel capture, Cilium policy, S3 and Raft "
        "remain deployment qualification gates."
    )


if __name__ == "__main__":
    main()
