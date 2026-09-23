"""Bounded regional canary and alert configuration reconciler. Standard library only."""

import base64
import json
import os
import re
import signal
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward an authorization header or secret to a redirect.


def http(url, payload=None, headers=None, method=None):
    headers = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=payload, headers=headers, method=method)
    with urllib.request.build_opener(NoRedirect).open(req, timeout=5) as response:
        body = response.read(4 * 1024 * 1024 + 1)
        if len(body) > 4 * 1024 * 1024:
            raise ValueError("response limit")
        return response.status, body


def attrs(values):
    return [{"key": k, "value": {"stringValue": str(v)}} for k, v in values.items()]


def payloads(writer, namespace, marker, trace_id, now):
    resource = {
        "attributes": attrs(
            {
                "service.name": "olly-canary",
                "service.namespace": namespace,
                "service.instance.id": writer,
                "k8s.pod.uid": writer,
                "k8s.namespace.name": namespace,
            }
        )
    }
    metrics = {
        "resourceMetrics": [
            {
                "resource": resource,
                "scopeMetrics": [
                    {
                        "metrics": [
                            {
                                "name": "olly_canary_marker",
                                "gauge": {
                                    "dataPoints": [
                                        {
                                            "asDouble": marker,
                                            "timeUnixNano": str(now),
                                            "attributes": attrs({"probe_writer": writer}),
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                ],
            }
        ]
    }
    traces = {
        "resourceSpans": [
            {
                "resource": resource,
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": trace_id,
                                "spanId": trace_id[:16],
                                "name": "synthetic support probe",
                                "kind": 2,
                                "startTimeUnixNano": str(now),
                                "endTimeUnixNano": str(now + 1000000),
                                "status": {"code": 2},
                                "attributes": attrs({"olly.synthetic": "true"}),
                            }
                        ]
                    }
                ],
            }
        ]
    }
    return metrics, traces


def metric_present(body, marker):
    obj = json.loads(body)
    return obj.get("status") == "success" and any(
        float(item["value"][1]) == marker for item in obj.get("data", {}).get("result", [])
    )


def trace_present(body, trace_id):
    obj = json.loads(body)
    # Tempo JSON uses protobuf JSON (base64 bytes); tolerate OTLP JSON hexadecimal too.
    expected = {trace_id.lower(), base64.b64encode(bytes.fromhex(trace_id)).decode()}
    for batch in obj.get("batches", obj.get("resourceSpans", [])):
        for scope in batch.get("scopeSpans", batch.get("instrumentationLibrarySpans", [])):
            if any(span.get("traceId") in expected for span in scope.get("spans", [])):
                return True
    return False


def alert_config(settings, secret_dir):
    def receiver(name, key):
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", key):
            raise ValueError("invalid receiver key")
        url = (Path(secret_dir) / key).read_text().strip()
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise ValueError("receiver requires HTTPS without userinfo")
        return {
            "name": name,
            "webhook_configs": [
                {
                    "url": url,
                    "send_resolved": True,
                    "max_alerts": 50,
                    "http_config": {"follow_redirects": False},
                }
            ],
        }

    receivers = [receiver("default", settings["defaultReceiverKey"])]
    routes = []
    for owner, key in sorted(settings["owners"].items()):
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,61}[a-z0-9]", owner):
            raise ValueError("invalid owner")
        name = "owner-" + owner
        receivers.append(receiver(name, key))
        routes.append({"receiver": name, "matchers": [f'owner="{owner}"']})
    return {
        "global": {"resolve_timeout": "5m"},
        "route": {
            "receiver": "default",
            "group_by": ["alertname", "cloud_region", "k8s_cluster_name", "owner", "service_name"],
            "group_wait": "30s",
            "group_interval": "5m",
            "repeat_interval": "4h",
            "routes": routes,
        },
        "receivers": receivers,
    }


class Agent:
    def __init__(self, config, writer, transport=http, emit=print):
        self.config, self.writer, self.http, self.emit = config, writer, transport, emit
        self.lock = threading.Lock()
        self.status = {}
        self.last_success = {}
        self.last_finished = 0
        self.stop = threading.Event()

    def record(self, signal_name, ok):
        with self.lock:
            self.status[signal_name] = int(ok)
            if ok:
                self.last_success[signal_name] = time.time()

    def check_canary(self):
        c = self.config
        now = time.time_ns()
        marker = now // 1000000
        trace_id = uuid.uuid4().hex
        metrics, traces = payloads(self.writer, c["namespace"], marker, trace_id, now)
        self.emit(
            json.dumps(
                {"level": "INFO", "message": "olly-canary:" + trace_id, "trace_id": trace_id}
            ),
            flush=True,
        )
        accepted = {"logs": True}
        for kind, payload in [("metrics", metrics), ("traces", traces)]:
            try:
                _, body = self.http(c["otlp"] + "/v1/" + kind, json.dumps(payload).encode())
                partial = json.loads(body or b"{}").get("partialSuccess", {})
                accepted[kind] = not any(
                    int(partial.get(k, 0)) for k in ["rejectedSpans", "rejectedDataPoints"]
                )
            except (OSError, ValueError, KeyError, TypeError):
                accepted[kind] = False
        password = Path(c["clickhousePasswordFile"]).read_text().strip()
        auth = {
            "Authorization": "Basic " + base64.b64encode(("canary:" + password).encode()).decode()
        }
        query = "olly_canary_marker{probe_writer=" + json.dumps(self.writer) + "}"
        sql = (
            "SELECT count() FROM observability.logs WHERE timestamp > now() - INTERVAL 15 MINUTE "
            "AND cluster_name={cluster:String} AND namespace={namespace:String} "
            "AND service_name='olly-canary' AND message={marker:String} FORMAT TabSeparated"
        )
        log_url = (
            c["clickhouse"]
            + "/?"
            + urllib.parse.urlencode(
                {
                    "query": sql,
                    "param_cluster": c["cluster"],
                    "param_namespace": c["namespace"],
                    "param_marker": "olly-canary:" + trace_id,
                }
            )
        )
        requests = {
            "logs": (log_url, auth, lambda b: int(b.strip()) > 0),
            "metrics": (
                c["mimir"] + "/prometheus/api/v1/query?" + urllib.parse.urlencode({"query": query}),
                {"X-Scope-OrgID": "platform"},
                lambda b: metric_present(b, marker),
            ),
            "traces": (
                c["tempo"] + "/api/traces/" + trace_id,
                {"Accept": "application/json"},
                lambda b: trace_present(b, trace_id),
            ),
        }
        pending = {k for k, ok in accepted.items() if ok}
        found = set()
        deadline = time.monotonic() + c["canary"]["deadlineSeconds"]
        while pending and time.monotonic() < deadline and not self.stop.is_set():
            for kind in list(pending):
                url, headers, predicate = requests[kind]
                try:
                    _, body = self.http(url, headers=headers)
                    if predicate(body):
                        pending.remove(kind)
                        found.add(kind)
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            if pending:
                self.stop.wait(c["canary"]["pollSeconds"])
        for kind in requests:
            self.record(kind, kind in found)

    def reconcile_alerts(self):
        settings = json.loads(Path(self.config["notificationFile"]).read_text())
        if not settings["enabled"]:
            return
        if not settings["manageTenantConfig"]:
            raise ValueError("tenant ownership required")
        config = alert_config(settings, "/receivers")
        # Never print this payload: receiver URLs may contain credentials.
        envelope = json.dumps(
            {"alertmanager_config": json.dumps(config), "template_files": {}}
        ).encode()
        code, _ = self.http(
            self.config["alertmanager"] + "/api/v1/alerts", envelope, {"X-Scope-OrgID": "platform"}
        )
        self.record("alert_config", code == 201)

    def run(self):
        while not self.stop.is_set():
            start = time.monotonic()
            if self.config["canary"]["enabled"]:
                try:
                    self.check_canary()
                except (OSError, ValueError, KeyError, TypeError):
                    for kind in ["logs", "metrics", "traces"]:
                        self.record(kind, False)
            if self.config["notificationsEnabled"]:
                try:
                    self.reconcile_alerts()
                except (OSError, ValueError, KeyError, TypeError):
                    self.record("alert_config", False)
            with self.lock:
                self.last_finished = time.time()
            self.stop.wait(max(1, self.config["intervalSeconds"] - (time.monotonic() - start)))

    def metrics(self):
        with self.lock:
            lines = [
                f"olly_support_completed_timestamp_seconds {self.last_finished}",
                f"olly_support_interval_seconds {self.config['intervalSeconds']}",
            ]
            for kind, ok in sorted(self.status.items()):
                labels = '{signal="' + kind + '"}'
                lines.extend(
                    [
                        f"olly_support_check_success{labels} {ok}",
                        f"olly_support_last_success_timestamp_seconds{labels} "
                        f"{self.last_success.get(kind, 0)}",
                    ]
                )
        return "\n".join(lines) + "\n"

    def healthy(self):
        expected = ["logs", "metrics", "traces"] if self.config["canary"]["enabled"] else []
        if self.config["notificationsEnabled"]:
            expected.append("alert_config")
        with self.lock:
            return (
                bool(expected)
                and (0 <= time.time() - self.last_finished < 2 * self.config["intervalSeconds"])
                and all(self.status.get(k) == 1 for k in expected)
            )


def main():
    config = json.loads(Path("/config/settings.json").read_text())
    agent = Agent(config, os.environ["POD_UID"])

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/metrics":
                data, code = agent.metrics(), 200
            elif self.path == "/health":
                code = 200 if agent.healthy() else 503
                data = "healthy\n" if code == 200 else "degraded\n"
            elif self.path == "/ready":
                data, code = (
                    "ready\n",
                    200,
                )  # Dependency failure must not remove the diagnostic endpoint.
            else:
                data, code = "not found\n", 404
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.end_headers()
            self.wfile.write(data.encode())

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("0.0.0.0", 8000), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=agent.run, daemon=True)
    worker.start()

    def shutdown(*_):
        agent.stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    server.serve_forever()
    worker.join(timeout=20)


if __name__ == "__main__":
    main()
