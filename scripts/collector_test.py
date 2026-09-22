#!/usr/bin/env python3
"""Exercise real Arrow links, metric rejection, and trace affinity locally.

Uses rendered production processors/exporters. Replaces Kubernetes discovery,
backend sinks and DNS coordinates only. No cloud, AZ or performance claims.
"""

import collections
import copy
import json
import re
import subprocess
import time
import urllib.request
import uuid

import yaml
from render import ROOT, render


def command(args):
    return subprocess.run(
        args, check=True, capture_output=True, text=True, timeout=40
    ).stdout.strip()


def request(address, path="", body=None):
    req = urllib.request.Request(
        "http://" + address + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        return response.read().decode()


def wait(predicate, description, seconds=60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except (OSError, ValueError, subprocess.CalledProcessError):
            pass
        time.sleep(0.5)
    raise AssertionError("Timed out: " + description)


def exports(directory, name, kind):
    path = directory / name / "export.json"
    if not path.exists():
        return []
    result = []
    for line in path.read_text().splitlines():
        try:
            result.extend(json.loads(line).get(kind, []))
        except json.JSONDecodeError:
            pass  # The exporter may be appending the current record.
    return result


def attrs(mapping):
    return [{"key": k, "value": {"stringValue": str(v)}} for k, v in mapping.items()]


def main():
    render()
    prefix = "olly-collector-" + uuid.uuid4().hex[:8]
    directory = ROOT / "build" / prefix
    directory.mkdir(parents=True)
    values = yaml.safe_load((ROOT / "charts/platform/values.yaml").read_text())
    image = values["images"]["otel"]
    configs = {
        role: yaml.safe_load((ROOT / f"build/configs/otel-{role}/collector.yaml").read_text())
        for role in ["agent", "metrics", "router", "traces"]
    }
    containers = []
    command(
        [
            "docker",
            "network",
            "create",
            "--label",
            "observability.test=collectors",
            prefix,
        ]
    )

    def start(name, config, ports=()):
        out = directory / name
        out.mkdir()
        out.chmod(0o777)  # Local fixture output only; no credentials or production data.
        config_path = directory / (name + ".yaml")
        config_path.write_text(yaml.safe_dump(config, sort_keys=False))
        port_args = [arg for port in [13133, *ports] for arg in ["-p", f"127.0.0.1::{port}"]]
        container = command(
            [
                "docker",
                "run",
                "-d",
                "--name",
                prefix + "-" + name,
                "--network",
                prefix,
                "--network-alias",
                name,
                "--label",
                "observability.test=collectors",
                "-e",
                f"K8S_POD_UID={name}",
                "--memory",
                "512m",
                "-v",
                f"{config_path}:/config.yaml:ro",
                "-v",
                f"{out}:/out",
                *port_args,
                image,
                "--config=/config.yaml",
            ]
        )
        containers.append(container)
        endpoints = {
            port: command(["docker", "port", container, f"{port}/tcp"]) for port in [13133, *ports]
        }
        wait(lambda: request(endpoints[13133]), name + " readiness", seconds=30)
        return endpoints

    def file_sink(config, signal):
        config["exporters"] = {"file": {"path": "/out/export.json", "flush_interval": "100ms"}}
        config["service"]["pipelines"][signal]["exporters"] = ["file"]
        return config

    def metrics():
        return [
            m
            for r in exports(directory, "otel-metrics", "resourceMetrics")
            for s in r["scopeMetrics"]
            for m in s["metrics"]
        ]

    def spans():
        return [
            (i, span)
            for i in range(3)
            for r in exports(directory, f"sampler-{i}", "resourceSpans")
            for s in r["scopeSpans"]
            for span in s["spans"]
        ]

    try:
        metrics_endpoint = start(
            "otel-metrics", file_sink(configs["metrics"], "metrics"), (4318, 8888)
        )
        for i in range(3):
            start(f"sampler-{i}", file_sink(copy.deepcopy(configs["traces"]), "traces"))
        configs["router"]["exporters"]["loadbalancing"]["resolver"]["static"]["hostnames"] = [
            f"sampler-{i}:4317" for i in range(3)
        ]
        router_endpoint = start("otel-router", configs["router"], (8888,))
        agent = configs["agent"]
        del agent["receivers"]["kubeletstats"]
        agent["service"]["pipelines"]["metrics"]["receivers"] = ["otlp"]
        entry = start("agent", agent, (4318,))
        now = time.time_ns()

        def metric(name, labels, instance="writer-a"):
            resource = {"service.name": "fixture", "unbounded.resource.metadata": str(uuid.uuid4())}
            if instance:
                resource["service.instance.id"] = instance
            return {
                "resource": {"attributes": attrs(resource)},
                "scopeMetrics": [
                    {
                        "scope": {"name": "fixture"},
                        "metrics": [
                            {
                                "name": name,
                                "sum": {
                                    "aggregationTemporality": 2,
                                    "isMonotonic": True,
                                    "dataPoints": [
                                        {
                                            "attributes": attrs(labels),
                                            "startTimeUnixNano": str(now - 1_000_000_000),
                                            "timeUnixNano": str(now),
                                            "asInt": "1",
                                        }
                                    ],
                                },
                            }
                        ],
                    }
                ],
            }

        good = [
            metric("safe_requests", {"http.route": "/orders/{id}"}, instance)
            for instance in ["writer-a", "writer-b"]
        ]
        bad = [
            metric("bad_" + key.replace(".", "_"), {key: "unique"})
            for key in ["user_id", "http.target", "db.query.text", "request_id"]
        ]
        bad += [
            metric("too_many_labels", {f"label_{i}": i for i in range(41)}),
            metric("missing_identity", {}, None),
        ]
        request(entry[4318], "/v1/metrics", {"resourceMetrics": good + bad})
        # Defense in depth: bad points that evade the agent still die at the gateway.
        request(metrics_endpoint[4318], "/v1/metrics", {"resourceMetrics": bad})
        wait(lambda: len(metrics()) >= 2, "Arrow metrics exports")
        trace_ids = [f"{i + 1:032x}" for i in range(48)]
        for batch in range(2):
            trace_spans = [
                {
                    "traceId": trace_id,
                    "spanId": f"{batch * 100 + i + 1:016x}",
                    "name": "fixture-error",
                    "kind": 2,
                    "startTimeUnixNano": str(now),
                    "endTimeUnixNano": str(now + 1_000_000),
                    "status": {"code": 2},
                }
                for i, trace_id in enumerate(trace_ids)
            ]
            request(
                entry[4318],
                "/v1/traces",
                {
                    "resourceSpans": [
                        {
                            "resource": {"attributes": attrs({"service.name": "fixture"})},
                            "scopeSpans": [{"scope": {"name": "fixture"}, "spans": trace_spans}],
                        }
                    ]
                },
            )
        wait(
            lambda: len(spans()) >= 96,
            "Arrow + trace-ID routing + production tail sampling",
            seconds=60,
        )
        actual = [m for m in metrics() if not m["name"].startswith("platform.red.")]
        assert len(actual) == 2 and {m["name"] for m in actual} == {"safe_requests"}, actual
        identities = {
            next(
                a["value"]["stringValue"]
                for a in r["resource"]["attributes"]
                if a["key"] == "service.instance.id"
            )
            for r in exports(directory, "otel-metrics", "resourceMetrics")
            if any(
                m["name"] == "safe_requests"
                for scope in r["scopeMetrics"]
                for m in scope["metrics"]
            )
        }
        assert identities == {"writer-a", "writer-b"}, identities
        for metric_export in actual:
            for point in metric_export["sum"]["dataPoints"]:
                keys = {a["key"] for a in point["attributes"]}
                assert "unbounded.resource.metadata" not in keys and "k8s_cluster_name" in keys, (
                    keys
                )
        assignments = collections.defaultdict(list)
        for sampler, span in spans():
            assignments[span["traceId"]].append(sampler)
        assert len(assignments) == 48 and all(
            len(v) == 2 and len(set(v)) == 1 for v in assignments.values()
        ), assignments
        assert len({v[0] for v in assignments.values()}) == 3, (
            "routing did not exercise all samplers"
        )
        telemetry = request(metrics_endpoint[8888], "/metrics")
        assert re.search(
            r"otelcol_processor_filter_datapoints_filtered(?:_total)?[ {]", telemetry
        ), "Metric policy drops must remain observable"
        # These counters exist only on the Arrow receiver; OTLP fallback is disabled.
        for endpoint in [metrics_endpoint, router_endpoint]:
            telemetry = request(endpoint[8888], "/metrics")
            assert "otelcol_otelarrow" in telemetry, "Arrow receiver telemetry absent"
        print(
            "PASS: real Arrow metrics/traces; cardinality and writer-identity rejection; "
            "48 traces/96 spans kept trace affinity across 3 tail samplers."
        )
    except Exception:
        for container in containers:
            log = subprocess.run(
                ["docker", "logs", "--tail", "16", container], capture_output=True, text=True
            )
            print(log.stdout + log.stderr)
        raise
    finally:
        for container in reversed(containers):
            subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=30)
        subprocess.run(["docker", "network", "rm", prefix], capture_output=True, timeout=30)


if __name__ == "__main__":
    main()
