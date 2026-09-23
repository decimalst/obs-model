#!/usr/bin/env python3
"""Native local privacy + pre-tail graph proofs; never uses shared render output."""

import copy
import json
import subprocess
import time
import urllib.parse
import uuid

import yaml
from collector_test import attrs, command, exports, request, wait
from render import ROOT


def rendered(tuning, directory):
    path = directory / "values.yaml"
    path.write_text(yaml.safe_dump(tuning))
    output = command(["helm", "template", "olly", str(ROOT / "charts/platform"), "-f", str(path)])
    return {
        d["metadata"]["name"]: yaml.safe_load(next(iter(d["data"].values())))
        for d in yaml.safe_load_all(output)
        if d
        and d["kind"] == "ConfigMap"
        and d["metadata"]["name"]
        in {
            "otel-agent",
            "otel-metrics",
            "otel-router",
            "otel-traces",
            "vector-agent",
            "vector-gateway",
        }
    }


def vector_checks(directory, values):
    for mode in ["redact", "drop"]:
        configs = rendered({"privacy": {"logs": {"mode": mode}}}, directory)
        for role in ["vector-agent", "vector-gateway"]:
            config = configs[role]
            config["sources"] = {"input": {"type": "stdin"}}
            config["data_dir"] = "/tmp/vector"
            config["transforms"] = {"privacy": config["transforms"]["privacy"]}
            config["transforms"]["privacy"]["inputs"] = ["input"]
            config["sinks"] = {"out": {"type": "blackhole", "inputs": ["privacy"]}}
            config["tests"] = []
            for name, message in {
                "safe": "started request /orders/{id}",
                "email": "customer jane@example.org requested support",
                "bearer": "Authorization: Bearer abc.def.ghi",
                "credential": "password=hunter2 api_key=secret-value",
                "json-credential": '{"password": "secret with spaces", "ok": true}',
            }.items():
                test = {
                    "name": name,
                    "inputs": [
                        {
                            "insert_at": "privacy",
                            "type": "log",
                            "log_fields": {
                                "message": message,
                                "service_key": "shop/api",
                                "private_extra": "secret-extra",
                            },
                        }
                    ],
                }
                if mode == "drop" and name != "safe":
                    test["no_outputs_from"] = ["privacy"]
                else:
                    expression = (
                        "assert_eq!(.message, " + json.dumps(message) + ")"
                        if name == "safe"
                        else 'assert!(contains(string!(.message), "[REDACTED]"))\n'
                        'assert!(!contains(string!(.message), "jane@example.org"))\n'
                        'assert!(!contains(string!(.message), "abc.def.ghi"))\n'
                        'assert!(!contains(string!(.message), "hunter2"))\n'
                        'assert!(!contains(string!(.message), "secret-value"))\n'
                        'assert!(!contains(string!(.message), "secret with spaces"))'
                    )
                    test["outputs"] = [
                        {
                            "extract_from": "privacy",
                            "conditions": [
                                {
                                    "type": "vrl",
                                    "source": expression
                                    + '\nassert_eq!(.service_key, "shop/api")'
                                    + "\nassert!(!exists(.private_extra))",
                                }
                            ],
                        }
                    ]
                config["tests"].append(test)
            # A malformed payload fails closed rather than bypassing processing.
            config["tests"].append(
                {
                    "name": "malformed-message",
                    "inputs": [
                        {
                            "insert_at": "privacy",
                            "type": "log",
                            "log_fields": {"message": {"nested": "secret"}},
                        }
                    ],
                    "no_outputs_from": ["privacy"],
                }
            )
            path = directory / (role + "-" + mode + ".yaml")
            path.write_text(yaml.safe_dump(config))
            command(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "-v",
                    f"{path}:/config.yaml:ro",
                    values["images"]["vector"],
                    "test",
                    "/config.yaml",
                ]
            )
    print(
        "Vector privacy: redact/drop, safe preservation, malformed rejection at agent and gateway."
    )


def span(number, kind=2, code=0, parent=None):
    now = time.time_ns()
    item = {
        "traceId": f"{number:032x}",
        "spanId": f"{number * 2 + (kind == 2):016x}",
        "name": "private-original-operation",
        "kind": kind,
        "startTimeUnixNano": str(now),
        "endTimeUnixNano": str(now + 100_000_000),
        "status": {"code": code, "message": "private-status"},
        "traceState": "private=secret",
        "attributes": attrs(
            {"user.id": "private-user", "db.name": "private-db", "peer.service": "private-peer"}
        )
        + [{"key": "http.response.status_code", "value": {"intValue": "200"}}],
    }
    if parent is not None:
        item["parentSpanId"] = parent
    return item


def send(address, service, spans):
    request(
        address,
        "/v1/traces",
        {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": attrs(
                            {
                                "service.name": service,
                                "service.namespace": "shop",
                                "private.resource": "private-resource",
                            }
                        )
                    },
                    "scopeSpans": [
                        {
                            "scope": {
                                "name": "private-scope",
                                "version": "private-version",
                                "attributes": attrs({"private.scope": "private-scope-value"}),
                            },
                            "spans": spans,
                        }
                    ],
                }
            ]
        },
    )


def main():
    prefix = "olly-privacy-graph-" + uuid.uuid4().hex[:8]
    directory = ROOT / "build" / prefix
    directory.mkdir(parents=True)
    values = yaml.safe_load((ROOT / "charts/platform/values.yaml").read_text())
    vector_checks(directory, values)
    containers = []
    command(["docker", "network", "create", "--label", "observability.test=privacy-graph", prefix])

    def start(name, config, ports=(4318,)):
        out = directory / name
        out.mkdir()
        out.chmod(0o777)
        path = directory / (name + ".yaml")
        path.write_text(yaml.safe_dump(config))
        container = command(
            [
                "docker",
                "run",
                "-d",
                "--network",
                prefix,
                "--network-alias",
                name,
                "--label",
                "observability.test=privacy-graph",
                "--memory",
                "512m",
                "-e",
                f"K8S_POD_UID={name}",
                "-v",
                f"{path}:/config.yaml:ro",
                "-v",
                f"{out}:/out",
                *[arg for port in [13133, *ports] for arg in ["-p", f"127.0.0.1::{port}"]],
                values["images"]["otel"],
                "--config=/config.yaml",
            ]
        )
        containers.append(container)
        addresses = {
            port: command(["docker", "port", container, f"{port}/tcp"]) for port in [13133, *ports]
        }
        try:
            wait(lambda: request(addresses[13133]), name + " readiness", 25)
        except AssertionError:
            print(command(["docker", "logs", container]))
            raise
        return addresses

    def trace_file(config):
        config["receivers"] = {"otlp": config["receivers"]["otlp"]}
        config["exporters"] = {"file": {"path": "/out/export.json", "flush_interval": "100ms"}}
        pipeline = config["service"]["pipelines"]["traces"]
        pipeline["exporters"] = ["file"]
        config["service"]["pipelines"] = {"traces": pipeline}
        return config

    try:
        for keep_name in [False, True]:
            config = rendered(
                {
                    "privacy": {"traces": {"mode": "allowlist", "keepSpanName": keep_name}},
                    "spanMetrics": {"enabled": False},
                },
                directory,
            )["otel-agent"]
            del config["receivers"]["kubeletstats"]
            name = "privacy-" + str(keep_name).lower()
            endpoint = start(name, trace_file(config))[4318]
            safe = span(9001)
            safe["events"] = [
                {
                    "name": "private-event",
                    "timeUnixNano": str(time.time_ns()),
                    "attributes": attrs({"private.event": "private-event-value"}),
                }
            ]
            linked = span(9002)
            linked["links"] = [
                {
                    "traceId": f"{999:032x}",
                    "spanId": f"{999:016x}",
                    "attributes": attrs({"private.link": "private-link-value"}),
                }
            ]
            send(endpoint, "checkout", [safe, linked])
            wait(
                lambda name=name: exports(directory, name, "resourceSpans"),
                "redacted trace delivery",
            )
            output = exports(directory, name, "resourceSpans")
            spans = [s for r in output for scope in r["scopeSpans"] for s in scope["spans"]]
            assert len(spans) == 1 and spans[0]["traceId"] == safe["traceId"], spans
            serialized = json.dumps(output)
            for sensitive in [
                "private-resource",
                "private-user",
                "private-db",
                "private-peer",
                "private-status",
                "private-event",
                "private-link",
                "private-version",
                "private-scope",
                "private=secret",
            ]:
                assert sensitive not in serialized, sensitive
            assert spans[0]["name"] == ("private-original-operation" if keep_name else "[REDACTED]")
            assert any(a["key"] == "http.response.status_code" for a in spans[0]["attributes"])
            assert not spans[0].get("events") and not spans[0].get("links")
        print(
            "Trace privacy: attributes/scope/events/status/tracestate cleared; "
            "linked span rejected."
        )

        configs = rendered(
            {
                "spanMetrics": {"enabled": False},
                "serviceGraph": {
                    "enabled": True,
                    "services": ["checkout", "payments"],
                    "flushIntervalSeconds": 5,
                    "storeTTLSeconds": 2,
                },
            },
            directory,
        )
        prom_config = directory / "prometheus.yaml"
        prom_config.write_text("global:\n  scrape_interval: 5s\nscrape_configs: []\n")
        prom = command(
            [
                "docker",
                "run",
                "-d",
                "--network",
                prefix,
                "--network-alias",
                "prometheus",
                "--label",
                "observability.test=privacy-graph",
                "-p",
                "127.0.0.1::9090",
                "-v",
                f"{prom_config}:/etc/prometheus/prometheus.yml:ro",
                "prom/prometheus:v3.5.0",
                "--config.file=/etc/prometheus/prometheus.yml",
                "--web.enable-remote-write-receiver",
            ]
        )
        containers.append(prom)
        prom_address = command(["docker", "port", prom, "9090/tcp"])
        wait(lambda: request(prom_address, "/-/ready"), "Prometheus readiness")

        def query(expr):
            return json.loads(
                request(prom_address, "/api/v1/query?query=" + urllib.parse.quote(expr))
            )["data"]["result"]

        def total(expr):
            result = query("sum(" + expr + ")")
            return float(result[0]["value"][1]) if result else -1

        metrics = configs["otel-metrics"]
        metrics["exporters"]["prometheusremotewrite"]["endpoint"] = (
            "http://prometheus:9090/api/v1/write"
        )
        start("otel-metrics", metrics, ())
        for i in range(3):
            sampler = copy.deepcopy(configs["otel-traces"])
            sampler["processors"]["tail_sampling"]["decision_wait"] = "2s"
            sampler["processors"]["tail_sampling"]["policies"] = [
                {
                    "name": "drop-all-fixture",
                    "type": "probabilistic",
                    "probabilistic": {"sampling_percentage": 0},
                }
            ]
            del sampler["exporters"]["otlp/tempo"]
            sampler["exporters"]["file"] = {"path": "/out/export.json", "flush_interval": "100ms"}
            sampler["service"]["pipelines"]["traces"]["exporters"] = ["file"]
            sampler["exporters"]["file/original"] = {
                "path": "/out/original.json",
                "flush_interval": "100ms",
            }
            sampler["service"]["pipelines"]["traces/original"] = {
                "receivers": ["otlp"],
                "processors": [],
                "exporters": ["file/original"],
            }
            start("sampler-" + str(i), sampler, ())
        router = configs["otel-router"]
        router["exporters"]["loadbalancing"]["resolver"]["static"]["hostnames"] = [
            f"sampler-{i}:4317" for i in range(3)
        ]
        start("otel-router", router, ())
        agent = configs["otel-agent"]
        del agent["receivers"]["kubeletstats"]
        agent["service"]["pipelines"]["metrics"]["receivers"] = ["otlp"]
        endpoints = [start(f"agent-{i}", copy.deepcopy(agent))[4318] for i in range(2)]
        for n in range(1, 49):
            client = span(n, kind=3, code=2 if n % 2 == 0 else 0)
            server = span(n, parent=client["spanId"])
            # Opposite node agents send the paired halves through trace-ID routing.
            send(endpoints[n % 2], "checkout", [client])
            send(endpoints[1 - n % 2], "payments", [server])
        calls = 'traces_service_graph_request_total{client="checkout",server="payments"}'
        wait(lambda: total(calls) == 48, "48 paired service edges before tail sampling")
        assert total("traces_service_graph_request_failed_total") == 24
        assert total("traces_service_graph_request_server_seconds_count") == 48
        assert abs(total("traces_service_graph_request_server_seconds_sum") - 4.8) < 1e-6
        streams = query(calls)
        assert len({s["metric"]["instance"] for s in streams}) >= 2, streams
        for stream in streams:
            assert stream["metric"]["k8s_cluster_name"] == values["clusterName"]
            assert stream["metric"]["cloud_region"] == values["region"]
            assert not set(stream["metric"]) & {"user_id", "db_name", "peer_service", "trace_id"}
        for n in range(100, 140):
            client = span(n, kind=3)
            send(endpoints[0], "unknown-" + str(n), [client])
            send(endpoints[1], "payments", [span(n, parent=client["spanId"])])
        # Allow unmatched graph entries to expire and the zero-percent sampler to decide.
        time.sleep(8)
        assert total("traces_service_graph_request_total") == 48
        original_ids = {}
        for i in range(3):
            assert exports(directory, "sampler-" + str(i), "resourceSpans") == []
            raw = directory / ("sampler-" + str(i)) / "original.json"
            for line in raw.read_text().splitlines():
                for r in json.loads(line).get("resourceSpans", []):
                    for scope in r["scopeSpans"]:
                        for original in scope["spans"]:
                            assert original["name"] == "private-original-operation"
                            assert "private-user" in json.dumps(original["attributes"])
                            original_ids.setdefault(original["traceId"], set()).add(i)
        assert all(len(destinations) == 1 for destinations in original_ids.values())
        # Execute actual post-export histogram quantile aggregation, including all writers.
        assert query(
            "histogram_quantile(0.95, sum by (le) ("
            "traces_service_graph_request_server_seconds_bucket))"
        )
        (directory / "graph-series.json").write_text(json.dumps(streams, indent=2))
        print(
            "Service graph: 48 paired edges, 24 failures, seconds histograms, independent writers; "
            "unknown names rejected and all original traces tail-dropped."
        )
    finally:
        for container in reversed(containers):
            subprocess.run(["docker", "rm", "-f", container], capture_output=True)
        subprocess.run(["docker", "network", "rm", prefix], capture_output=True)


if __name__ == "__main__":
    main()
