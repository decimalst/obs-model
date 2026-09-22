#!/usr/bin/env python3
"""Real pre-tail RED generation -> Arrow -> gateway -> Prometheus remote write.

Prometheus substitutes for Mimir to verify exporter names, labels and PromQL.
Retained trace sinks are files; no EKS, S3 or throughput claim is made.
"""

import copy
import json
import subprocess
import time
import urllib.parse
import uuid

import yaml
from collector_test import attrs, command, exports, request, wait
from render import ROOT, render


def main():
    render()
    name = "olly-red-" + uuid.uuid4().hex[:8]
    directory = ROOT / "build" / name
    directory.mkdir()
    values = yaml.safe_load((ROOT / "charts/platform/values.yaml").read_text())
    configs = {
        role: yaml.safe_load((ROOT / f"build/configs/otel-{role}/collector.yaml").read_text())
        for role in ["agent", "metrics", "router", "traces"]
    }
    containers = []
    command(["docker", "network", "create", "--label", "observability.test=red", name])

    def start(role, config, ports=()):
        output = directory / role
        output.mkdir()
        output.chmod(0o777)
        path = directory / (role + ".yaml")
        path.write_text(yaml.safe_dump(config))
        container = command(
            [
                "docker",
                "run",
                "-d",
                "--network",
                name,
                "--network-alias",
                role,
                "--label",
                "observability.test=red",
                "--memory",
                "512m",
                "-e",
                f"K8S_POD_UID={role}",
                "-v",
                f"{path}:/config.yaml:ro",
                "-v",
                f"{output}:/out",
                *[arg for port in [13133, *ports] for arg in ["-p", f"127.0.0.1::{port}"]],
                values["images"]["otel"],
                "--config=/config.yaml",
            ]
        )
        containers.append(container)
        endpoints = {
            port: command(["docker", "port", container, f"{port}/tcp"]) for port in [13133, *ports]
        }
        wait(lambda: request(endpoints[13133]), role + " readiness", 30)
        return endpoints

    def capture(config, pipeline, only=False):
        if only:
            config["exporters"] = {}
            config["service"]["pipelines"][pipeline]["exporters"] = []
        config["exporters"]["file"] = {"path": "/out/export.json", "flush_interval": "100ms"}
        config["service"]["pipelines"][pipeline]["exporters"].append("file")
        return config

    def send(address, spans, resource=None):
        request(
            address,
            "/v1/traces",
            {
                "resourceSpans": [
                    {
                        "resource": {
                            "attributes": attrs(
                                resource
                                or {
                                    "service.name": "checkout",
                                    "service.namespace": "shop",
                                    "service.instance.id": "application-not-the-metric-writer",
                                    "user.id": "unbounded-resource",
                                }
                            )
                        },
                        "scopeSpans": [{"scope": {"name": "red-fixture"}, "spans": spans}],
                    }
                ]
            },
        )

    def span(number, kind=2, code=0, http_code=200, duration_ms=100, route="/orders/{id}"):
        now = time.time_ns()
        return {
            "traceId": f"{number:032x}",
            "spanId": f"{number:016x}",
            "name": f"GET /orders/private-id-{number}",
            "kind": kind,
            "startTimeUnixNano": str(now),
            "endTimeUnixNano": str(now + duration_ms * 1_000_000),
            "status": {"code": code},
            "attributes": attrs(
                {
                    "url.full": "https://private.example/account",
                    "user.id": str(number),
                    "http.route": route,
                }
            )
            + [{"key": "http.response.status_code", "value": {"intValue": str(http_code)}}],
        }

    try:
        prometheus_config = directory / "prometheus.yaml"
        prometheus_config.write_text("global:\n  scrape_interval: 5s\nscrape_configs: []\n")
        prom = command(
            [
                "docker",
                "run",
                "-d",
                "--network",
                name,
                "--network-alias",
                "prometheus",
                "--label",
                "observability.test=red",
                "-p",
                "127.0.0.1::9090",
                "-v",
                f"{prometheus_config}:/etc/prometheus/prometheus.yml:ro",
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

        gateway = capture(configs["metrics"], "metrics")
        gateway["exporters"]["prometheusremotewrite"]["endpoint"] = (
            "http://prometheus:9090/api/v1/write"
        )
        metrics_endpoint = start("otel-metrics", gateway, (8888,))
        for i in range(3):
            sampler = copy.deepcopy(configs["traces"])
            # Deliberately store no healthy/slow traces, while still deriving RED from all inputs.
            composite = sampler["processors"]["tail_sampling"]["policies"][0]["composite"]
            composite["composite_sub_policy"][1]["latency"]["threshold_ms"] = 60000
            composite["composite_sub_policy"][2]["probabilistic"]["sampling_percentage"] = 0
            start(f"sampler-{i}", capture(sampler, "traces", True))
        router = capture(configs["router"], "traces")
        router["exporters"]["loadbalancing"]["resolver"]["static"]["hostnames"] = [
            f"sampler-{i}:4317" for i in range(3)
        ]
        start("otel-router", router)
        agent = configs["agent"]
        del agent["receivers"]["kubeletstats"]
        agent["service"]["pipelines"]["metrics"]["receivers"] = ["otlp"]
        # Use the minimum supported interval to bound test time.
        agent["connectors"]["spanmetrics/red"]["metrics_flush_interval"] = "5s"
        entries = [start(f"agent-{i}", copy.deepcopy(agent), (4318,))[4318] for i in range(2)]
        for i, address in enumerate(entries):
            n = i * 10
            send(
                address,
                [
                    span(n + 1, duration_ms=10),
                    span(n + 2, duration_ms=100),
                    span(n + 3, http_code=503, duration_ms=200),
                    span(n + 4, code=2, duration_ms=1000),
                    span(n + 5, kind=3, code=2),
                    span(n + 6, kind=1, code=2),
                ],
            )
            send(address, [span(n + 7)], {"service.namespace": "missing-name"})
        calls = 'platform_red_calls_total{service_name="checkout"}'
        wait(lambda: total(calls) == 8, "8 server calls from both agents")
        assert (
            total(
                'platform_red_calls_total{service_name="checkout",status_code="STATUS_CODE_ERROR"}'
            )
            == 4
        )
        assert total('platform_red_duration_seconds_count{service_name="checkout"}') == 8
        assert (
            abs(total('platform_red_duration_seconds_sum{service_name="checkout"}') - 2.62) < 1e-6
        )
        streams = query(calls)
        assert {s["metric"]["instance"] for s in streams} == {"agent-0", "agent-1"}, streams
        for stream in streams:
            labels = stream["metric"]
            assert labels["span_kind"] == "SPAN_KIND_SERVER", labels
            assert labels["k8s_cluster_name"] == values["clusterName"], labels
            assert labels["service_namespace"] == "shop", labels
            assert not set(labels) & {
                "span_name",
                "trace_id",
                "span_id",
                "url_full",
                "user_id",
                "http_route",
            }
        received = [
            span
            for r in exports(directory, "otel-router", "resourceSpans")
            for scope in r["scopeSpans"]
            for span in scope["spans"]
        ]
        original_503 = next(s for s in received if int(s["traceId"], 16) == 3)
        assert original_503.get("status", {}).get("code", 0) == 0, original_503
        assert "private-id" in original_503["name"]

        def retained():
            return [
                s
                for i in range(3)
                for r in exports(directory, f"sampler-{i}", "resourceSpans")
                for scope in r["scopeSpans"]
                for s in scope["spans"]
            ]

        wait(lambda: len(retained()) == 6, "tail sampling retains only explicit error traces", 75)
        assert {int(s["traceId"], 16) for s in retained()} == {4, 5, 6, 14, 15, 16}
        # Optional route dimension must overflow safely instead of creating unbounded series.
        routed = subprocess.run(
            [
                "helm",
                "template",
                "olly",
                str(ROOT / "charts/platform"),
                "--set",
                "spanMetrics.includeHttpRoute=true",
                "--set",
                "spanMetrics.aggregationCardinalityLimit=2",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        red_agent = yaml.safe_load(
            next(
                d
                for d in yaml.safe_load_all(routed.stdout)
                if d and d["kind"] == "ConfigMap" and d["metadata"]["name"] == "otel-agent"
            )["data"]["collector.yaml"]
        )
        del red_agent["receivers"]["kubeletstats"]
        red_agent["service"]["pipelines"]["metrics"]["receivers"] = ["otlp"]
        red_agent["connectors"]["spanmetrics/red"]["metrics_flush_interval"] = "5s"
        overflow = start("agent-overflow", red_agent, (4318,))[4318]
        send(
            overflow,
            [span(100 + i, route=f"/unsafe/route-{i}") for i in range(10)],
            {"service.name": "overflow", "service.namespace": "shop"},
        )
        wait(
            lambda: total('platform_red_calls_total{otel_metric_overflow="true"}') >= 8,
            "overflow metrics visible",
        )
        assert len(query('platform_red_calls_total{service_name="overflow"}')) <= 2
        assert "otelcol_otelarrow" in request(metrics_endpoint[8888], "/metrics")
        print(
            "PASS: pre-tail RED over Arrow/remote write; 8 server calls, 4 errors, "
            "8 histogram observations (2.62 seconds); two distinct metric writers; "
            "client/internal/missing-service exclusions; original traces unchanged; "
            "0% healthy trace retention; route cardinality overflow visible."
        )
    except Exception:
        for container in containers:
            output = subprocess.run(
                ["docker", "logs", "--tail", "12", container], capture_output=True, text=True
            )
            print(output.stdout + output.stderr)
        raise
    finally:
        for container in reversed(containers):
            subprocess.run(["docker", "rm", "-fv", container], capture_output=True, timeout=30)
        subprocess.run(["docker", "network", "rm", name], capture_output=True, timeout=30)


if __name__ == "__main__":
    main()
