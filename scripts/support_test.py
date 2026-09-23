#!/usr/bin/env python3
"""Native local support qualification. Reduced storage topology, no AWS or external pages."""

import base64
import importlib.util
import json
import subprocess
import urllib.error
import urllib.parse
import uuid
import xml.etree.ElementTree as ET

import yaml
from collector_test import command, request, wait
from render import ROOT, render

spec = importlib.util.spec_from_file_location(
    "support_agent", ROOT / "charts/platform/files/support/agent.py"
)
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


def main():
    render()
    name = "olly-support-" + uuid.uuid4().hex[:8]
    directory = ROOT / "build" / name
    directory.mkdir()
    directory.chmod(0o777)
    containers = []
    metrics_files = ROOT / "build/support-metrics"
    metrics_files.mkdir(exist_ok=True)
    enabled = yaml.safe_load_all(
        command(
            [
                "helm",
                "template",
                "olly",
                "charts/platform",
                "-n",
                "observability",
                "--set",
                "support.canary.enabled=true",
            ]
        )
    )
    ch_config = next(o for o in enabled if o and o["kind"] == "ClickHouseInstallation")["spec"][
        "configuration"
    ]
    users_xml = ET.Element("clickhouse")
    profile = ET.SubElement(ET.SubElement(users_xml, "profiles"), "canary")
    for key, value in ch_config["profiles"].items():
        if key.startswith("canary/"):
            ET.SubElement(profile, key.split("/")[1]).text = str(value)
    user = ET.SubElement(ET.SubElement(users_xml, "users"), "canary")
    ET.SubElement(user, "password").text = "validation"
    ET.SubElement(user, "profile").text = ch_config["users"]["canary/profile"]
    ET.SubElement(ET.SubElement(user, "networks"), "ip").text = "::/0"
    (directory / "canary.xml").write_bytes(ET.tostring(users_xml))
    command(["docker", "network", "create", "--label", "observability.test=support", name])

    def start(role, image, args=(), ports=(), extra=()):
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
                "observability.test=support",
                "-v",
                f"{directory}:/fixture:ro",
                *[a for port in ports for a in ["-p", f"127.0.0.1::{port}"]],
                *extra,
                image,
                *args,
            ]
        )
        containers.append(container)
        return {p: command(["docker", "port", container, f"{p}/tcp"]) for p in ports}

    try:
        (directory / "prometheus.yaml").write_text(
            "global:\n  scrape_interval: 5s\nscrape_configs: []\n"
        )
        prom = start(
            "prometheus",
            "prom/prometheus:v3.5.0",
            ["--config.file=/fixture/prometheus.yaml", "--web.enable-remote-write-receiver"],
            [9090],
        )
        wait(lambda: request(prom[9090], "/-/ready"), "Prometheus")
        (directory / "tempo.yaml").write_text(
            yaml.safe_dump(
                {
                    "server": {"http_listen_port": 3200},
                    "distributor": {
                        "receivers": {"otlp": {"protocols": {"http": {"endpoint": "0.0.0.0:4318"}}}}
                    },
                    "ingester": {"trace_idle_period": "1s", "max_block_duration": "5m"},
                    "storage": {
                        "trace": {
                            "backend": "local",
                            "local": {"path": "/tmp/blocks"},
                            "wal": {"path": "/tmp/wal"},
                        }
                    },
                    "compactor": {"compaction": {"block_retention": "1h"}},
                }
            )
        )
        tempo = start(
            "tempo", "grafana/tempo:2.9.0", ["-config.file=/fixture/tempo.yaml"], [3200, 4318]
        )
        wait(lambda: request(tempo[3200], "/ready"), "Tempo", 60)
        (directory / "clickhouse.xml").write_text(
            "<clickhouse><prometheus><endpoint>/metrics</endpoint><port>9363</port><metrics>true</metrics><events>true</events><asynchronous_metrics>true</asynchronous_metrics></prometheus></clickhouse>"
        )
        ch = start(
            "clickhouse",
            "clickhouse/clickhouse-server:25.8.33.6",
            ports=[8123, 9363],
            extra=[
                "-e",
                "CLICKHOUSE_USER=reader",
                "-e",
                "CLICKHOUSE_PASSWORD=validation",
                "-v",
                f"{directory}/clickhouse.xml:/etc/clickhouse-server/config.d/prometheus.xml:ro",
                "-v",
                f"{directory}/canary.xml:/etc/clickhouse-server/users.d/canary.xml:ro",
            ],
        )
        auth = {"Authorization": "Basic cmVhZGVyOnZhbGlkYXRpb24="}

        def sql(query):
            return agent.http(
                "http://" + ch[8123] + "/", query.encode(), {**auth, "Content-Type": "text/plain"}
            )

        wait(lambda: sql("SELECT 1"), "ClickHouse")
        sql("CREATE DATABASE observability")
        sql(
            "CREATE TABLE observability.logs (cluster_name String,event_id UUID,"
            "timestamp DateTime64(3),"
            "service_key String,service_name String,namespace String,level String,"
            "message String,trace_id String) "
            "ENGINE=MergeTree ORDER BY (cluster_name,namespace,service_name,timestamp)"
        )
        canary_auth = {"Authorization": "Basic " + base64.b64encode(b"canary:validation").decode()}
        _, limits = agent.http(
            "http://"
            + ch[8123]
            + "/?"
            + urllib.parse.urlencode({"query": "SELECT getSetting('max_rows_to_read')"}),
            headers=canary_auth,
        )
        assert limits.strip() == b"100000", limits
        for forbidden in [
            "INSERT INTO observability.logs (message) VALUES ('forbidden')",
            "SET max_rows_to_read=0",
        ]:
            try:
                agent.http("http://" + ch[8123] + "/", forbidden.encode(), canary_auth)
            except urllib.error.HTTPError as exc:
                assert b"READONLY" in exc.read(), "Canary did not reject the write as read-only"
            else:
                raise AssertionError("Canary account can write or remove scan bounds")
        objects = {
            (o["kind"], o["metadata"]["name"]): o
            for o in yaml.safe_load_all((ROOT / "build/platform.yaml").read_text())
            if o
        }
        support_config = objects["ConfigMap", "olly-support"]["data"]
        for key in ["agent.py", "settings.json", "notifications.json"]:
            (directory / key).write_text(support_config[key])
        support_container = objects["Deployment", "olly-support"]["spec"]["template"]["spec"][
            "containers"
        ][0]
        support = start(
            "support",
            support_container["image"],
            support_container["command"],
            [8000],
            [
                "--user",
                "10001:10001",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "-e",
                "POD_UID=fixture",
                "-v",
                f"{directory}:/config:ro",
            ],
        )
        wait(lambda: request(support[8000], "/ready"), "support readiness")
        try:
            request(support[8000], "/health")
        except urllib.error.HTTPError as exc:
            assert exc.code == 503
        else:
            raise AssertionError("Disabled checks must not report healthy signal delivery")
        assert "olly_support_completed_timestamp_seconds" in request(support[8000], "/metrics")
        print(
            "Pinned support runtime starts without credentials; canary account rejects writes "
            "and scan-limit overrides."
        )
        vector = yaml.safe_load(objects["ConfigMap", "vector-agent"]["data"]["vector.yaml"])
        vector["data_dir"] = "/tmp/vector"
        vector["sources"]["kubernetes"] = {
            "type": "http_server",
            "address": "0.0.0.0:8080",
            "decoding": {"codec": "json"},
        }
        vector["sinks"]["quota"] = {
            "type": "clickhouse",
            "inputs": ["cleanup"],
            "endpoint": "http://clickhouse:8123",
            "database": "observability",
            "table": "logs",
            "skip_unknown_fields": True,
            "date_time_best_effort": True,
            "auth": {"strategy": "basic", "user": "reader", "password": "validation"},
            "batch": {"timeout_secs": 1},
        }
        (directory / "vector.yaml").write_text(yaml.safe_dump(vector))
        vect = start(
            "vector",
            "timberio/vector:0.54.0-debian",
            ["--config", "/fixture/vector.yaml"],
            [8080, 8686],
        )
        wait(lambda: request(vect[8686], "/health"), "Vector")
        for role in ["metrics", "agent"]:
            c = yaml.safe_load(objects["ConfigMap", "otel-" + role]["data"]["collector.yaml"])
            if role == "metrics":
                c["exporters"]["prometheusremotewrite"]["endpoint"] = (
                    "http://prometheus:9090/api/v1/write"
                )
            else:
                del c["receivers"]["kubeletstats"]
                c["service"]["pipelines"]["metrics"]["receivers"] = ["otlp"]
                c["exporters"]["otelarrow/metrics"]["endpoint"] = "metrics:4317"
                del c["exporters"]["otelarrow/traces"]
                c["exporters"]["otlphttp/test"] = {"endpoint": "http://tempo:4318"}
                c["service"]["pipelines"]["traces"]["exporters"] = ["otlphttp/test"]
            (directory / (role + ".yaml")).write_text(yaml.safe_dump(c))
            ports = start(
                role,
                "otel/opentelemetry-collector-contrib:0.148.0",
                ["--config=/fixture/" + role + ".yaml"],
                [4318, 8888, 13133],
                ["-e", "K8S_POD_UID=local-agent"],
            )
            wait(lambda ports=ports: request(ports[13133]), role)
            if role == "agent":
                otlp = ports
            else:
                gateway = ports
        (directory / "password").write_text("validation")
        config = json.loads(objects["ConfigMap", "olly-support"]["data"]["settings.json"])
        config.update(
            otlp="http://" + otlp[4318],
            mimir="http://" + prom[9090],
            tempo="http://" + tempo[3200],
            clickhouse="http://" + ch[8123],
            clickhousePasswordFile=str(directory / "password"),
        )
        config["canary"] = {"enabled": True, "deadlineSeconds": 45, "pollSeconds": 0.5}

        # Prometheus substitutes for the Mimir query endpoint (prefix differs).
        def transport(url, *args, **kwargs):
            return agent.http(url.replace("/prometheus/api/", "/api/"), *args, **kwargs)

        def emit(line, **_):
            request(
                vect[8080],
                "/",
                {
                    "message": line,
                    "kubernetes": {
                        "pod_namespace": "observability",
                        "pod_labels": {"observability/service": "olly-canary"},
                    },
                },
            )

        probe = agent.Agent(config, "native-probe", transport=transport, emit=emit)
        probe.check_canary()
        assert probe.status == {"logs": 1, "metrics": 1, "traces": 1}, probe.status
        print(
            "Canary finds exact log, metric marker and trace through native Vector, "
            "Arrow collectors, ClickHouse, Prometheus and Tempo."
        )
        for component, address in [
            ("clickhouse", ch[9363]),
            ("otel", gateway[8888]),
            ("tempo", tempo[3200]),
        ]:
            (metrics_files / (component + ".prom")).write_text(request(address, "/metrics"))
        # Mimir's actual Alertmanager configuration API and notification delivery, isolated locally.
        (directory / "probe.prom").write_text(
            probe.metrics()
            + "support_safe_metric 1\n"
            + 'support_forbidden_metric{user_id="test"} 1\n'
            + 'support_forbidden_semicolon{user_id=";"} 1\n'
        )
        (directory / "webhook.py").write_text("""import json
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
received=[]
class Handler(BaseHTTPRequestHandler):
 def do_POST(self):
  received.append({"path":self.path,"payload":json.loads(self.rfile.read(int(self.headers['Content-Length'])))})
  self.send_response(200);self.end_headers()
 def do_GET(self):
  self.send_response(200);self.end_headers()
  data=(Path('/fixture/probe.prom').read_bytes() if self.path=='/metrics'
        else json.dumps(received).encode())
  self.wfile.write(data)
 def log_message(self,*a):pass
HTTPServer(('0.0.0.0',8000),Handler).serve_forever()
""")
        webhook = start(
            "webhook", "python:3.14.7-slim", ["python", "-B", "/fixture/webhook.py"], [8000]
        )
        wait(lambda: request(webhook[8000]), "webhook")
        (directory / "mimir.yaml").write_text(
            yaml.safe_dump(
                {
                    "multitenancy_enabled": True,
                    "server": {"http_listen_port": 8080},
                    "alertmanager": {
                        "data_dir": "/tmp/alertmanager",
                        "enable_api": True,
                        "external_url": "http://localhost:8080/alertmanager",
                        "sharding_ring": {
                            "kvstore": {"store": "inmemory"},
                            "replication_factor": 1,
                            "instance_addr": "127.0.0.1",
                        },
                    },
                    "alertmanager_storage": {
                        "backend": "filesystem",
                        "filesystem": {"dir": "/tmp/alerts"},
                    },
                }
            )
        )
        mimir = start(
            "mimir",
            "grafana/mimir:3.2.0",
            ["-target=alertmanager", "-config.file=/fixture/mimir.yaml"],
            [8080],
        )
        wait(lambda: request(mimir[8080], "/ready"), "Mimir Alertmanager", 60)
        (directory / "default-url").write_text("https://example.com/default")
        (directory / "payments-url").write_text("https://example.com/payments")
        notification_config = agent.alert_config(
            {"defaultReceiverKey": "default-url", "owners": {"payments": "payments-url"}}, directory
        )
        # Only the test swaps the validated HTTPS receivers for disposable local HTTP sinks.
        for r in notification_config["receivers"]:
            r["webhook_configs"][0]["url"] = "http://webhook:8000/" + r["name"]
        notification_config["route"].update(
            group_wait="0s", group_interval="1s", repeat_interval="1h"
        )
        base = "http://" + mimir[8080]
        headers = {"X-Scope-OrgID": "platform"}
        status, _ = agent.http(
            base + "/api/v1/alerts",
            json.dumps(
                {"alertmanager_config": json.dumps(notification_config), "template_files": {}}
            ).encode(),
            headers,
        )
        assert status == 201

        def loaded():
            return agent.http(base + "/alertmanager/api/v2/status", headers=headers)[0] == 200

        wait(loaded, "tenant configuration active", 60)
        body = [
            {
                "labels": {"alertname": "LocalSupportFixture", "owner": owner},
                "annotations": {"summary": "local-only validation"},
            }
            for owner in ["payments", "unassigned"]
        ]
        agent.http(base + "/alertmanager/api/v2/alerts", json.dumps(body).encode(), headers)

        def delivered():
            events = json.loads(request(webhook[8000]))
            return {e["path"] for e in events} >= {"/default", "/owner-payments"}

        wait(delivered, "owner and default delivery", 60)
        (metrics_files / "mimir.prom").write_text(request(mimir[8080], "/metrics"))
        scrape = objects["OpenTelemetryCollector", "otel-scrape"]["spec"]["config"]
        targets = {
            "mimir": ("mimir:8080", "8080"),
            "tempo": ("tempo:3200", "3200"),
            "clickhouse": ("clickhouse:9363", "9363"),
            "annotated-pods": ("metrics:8888", "8888"),
        }
        jobs = []
        for job in scrape["receivers"]["prometheus"]["config"]["scrape_configs"]:
            if job["job_name"] not in targets:
                continue
            address, port = targets[job["job_name"]]
            del job["kubernetes_sd_configs"]
            job["scrape_interval"] = "1s"
            job["scrape_timeout"] = "1s"
            job["static_configs"] = [
                {
                    "targets": [address],
                    "labels": {
                        "__meta_kubernetes_pod_phase": "Running",
                        "__meta_kubernetes_pod_container_port_number": port,
                        "__meta_kubernetes_pod_annotation_prometheus_io_scrape": "true",
                        "__meta_kubernetes_pod_annotation_prometheus_io_port": port,
                        "__meta_kubernetes_pod_label_app_kubernetes_io_name": job["job_name"],
                        "__meta_kubernetes_pod_label_clickhouse_altinity_com_chi": "observability",
                    },
                }
            ]
            if job["job_name"] == "annotated-pods":
                labels = dict(job["static_configs"][0]["labels"])
                labels.update(
                    __meta_kubernetes_pod_container_port_number="8000",
                    __meta_kubernetes_pod_annotation_prometheus_io_port="8000",
                )
                job["static_configs"].append({"targets": ["webhook:8000"], "labels": labels})
            jobs.append(job)
        scrape["receivers"]["prometheus"]["config"]["scrape_configs"] = jobs
        scrape["exporters"]["otelarrow"]["endpoint"] = "metrics:4317"
        scrape["exporters"]["file/debug"] = {"path": "/out/scrape.json", "flush_interval": "100ms"}
        scrape["service"]["pipelines"]["metrics"]["exporters"].append("file/debug")
        (directory / "scrape.yaml").write_text(yaml.safe_dump(scrape))
        start(
            "scrape",
            "otel/opentelemetry-collector-contrib:0.148.0",
            ["--config=/fixture/scrape.yaml"],
            extra=["-v", f"{directory}:/out"],
        )

        def scraped():
            data = json.loads(
                request(
                    prom[9090],
                    "/api/v1/query?"
                    + urllib.parse.urlencode(
                        {
                            "query": 'up{job=~"mimir|tempo|clickhouse|annotated-pods|'
                            'otelcol-contrib"}'
                        }
                    ),
                )
            )
            return len([r for r in data["data"]["result"] if r["value"][1] == "1"]) == 5

        wait(scraped, "production backend scrape jobs through Arrow and remote write", 30)
        wait(
            lambda: any(
                "InsertedRows" in n
                for n in json.loads(request(prom[9090], "/api/v1/label/__name__/values"))["data"]
            ),
            "backend samples, not just up",
            30,
        )
        series = json.loads(request(prom[9090], "/api/v1/label/__name__/values"))["data"]
        assert "support_safe_metric" in series
        assert "support_forbidden_metric" not in series
        assert "support_forbidden_semicolon" not in series
        required = {
            "ClickHouseProfileEvents_InsertedRows_total",
            "otelcol_exporter_queue_size",
            "tempo_distributor_spans_received_total",
        }
        assert required <= set(series), sorted(required - set(series))
        (metrics_files / "remote-names.json").write_text(json.dumps(series, indent=2) + "\n")
        dashboard = json.loads(
            (ROOT / "charts/platform/files/dashboards/platform-support.json").read_text()
        )
        for panel in dashboard["panels"]:
            expression = (
                panel["targets"][0]["expr"].replace("$region", ".*").replace("$cluster", ".*")
            )
            path = "/api/v1/query?" + urllib.parse.urlencode({"query": expression})
            if "kafka_" in expression:
                assert json.loads(request(prom[9090], path))["status"] == "success"
            else:
                wait(
                    lambda path=path: bool(json.loads(request(prom[9090], path))["data"]["result"]),
                    "dashboard query: " + panel["title"],
                    15,
                )
        print(
            "Support dashboard queries execute against stored native metrics; "
            "Kafka values are tested separately."
        )
        print(
            "Mimir stores the generated tenant config and delivers owner/default fixture alerts "
            "to a local receiver."
        )
    except Exception:
        # Do not print container logs: alert configuration may contain receiver credentials.
        print(
            "Support test failed; isolated fixture paths retained under build/. "
            "No external notifications sent."
        )
        raise
    finally:
        for c in reversed(containers):
            subprocess.run(["docker", "rm", "-f", c], check=False, capture_output=True)
        subprocess.run(["docker", "network", "rm", name], check=False, capture_output=True)


if __name__ == "__main__":
    main()
