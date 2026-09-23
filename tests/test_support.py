import base64
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from scripts.support_bundle import collect, summarize
from scripts.tuning import validate_tuning

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "support_agent", ROOT / "charts/platform/files/support/agent.py"
)
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


class SupportTests(unittest.TestCase):
    def test_stale_metrics_and_wrong_trace_do_not_pass(self):
        body = {"status": "success", "data": {"result": [{"value": [1, "123"]}]}}
        self.assertFalse(agent.metric_present(json.dumps(body), 124))
        self.assertTrue(agent.metric_present(json.dumps(body), 123))
        trace = "a" * 32
        for identifier in [trace, base64.b64encode(bytes.fromhex(trace)).decode()]:
            body = {"batches": [{"scopeSpans": [{"spans": [{"traceId": identifier}]}]}]}
            self.assertTrue(agent.trace_present(json.dumps(body), trace))
            self.assertFalse(agent.trace_present(json.dumps(body), "b" * 32))
        self.assertFalse(agent.trace_present(b"{}", trace))

    def test_canary_partial_ingestion_cannot_be_reported_as_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            password = Path(tmp) / "password"
            password.write_text("test")
            config = dict(
                namespace="observability",
                cluster="east",
                otlp="http://otlp",
                mimir="http://mimir",
                tempo="http://tempo",
                clickhouse="http://clickhouse",
                clickhousePasswordFile=str(password),
                intervalSeconds=120,
                canary={"deadlineSeconds": 0.01, "pollSeconds": 0.001},
            )
            calls, outputs = [], []

            def transport(url, payload=None, headers=None):
                calls.append(url)
                if url.endswith("/v1/metrics"):
                    return 200, b'{"partialSuccess":{"rejectedDataPoints":"1"}}'
                if url.endswith("/v1/traces"):
                    return 200, b'{"partialSuccess":{"rejectedSpans":"1"}}'
                if url.startswith("http://clickhouse"):
                    self.assertEqual(headers["Authorization"], "Basic Y2FuYXJ5OnRlc3Q=")
                    self.assertNotIn("max_rows_to_read", url)
                    return 200, b"1\n"
                raise AssertionError("Rejected signals must not pass because old data exists")

            probe = agent.Agent(
                config, "writer-a", transport=transport, emit=lambda s, **kw: outputs.append(s)
            )
            probe.check_canary()
            self.assertEqual(probe.status, {"logs": 1, "metrics": 0, "traces": 0})
            self.assertEqual(len(outputs), 1)
            self.assertIn("olly-canary:", outputs[0])
            self.assertNotIn("test", probe.metrics())

    def test_alert_routes_default_owner_and_secret_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = {"defaultReceiverKey": "default-url", "owners": {"payments": "payments-url"}}
            for name in ["default-url", "payments-url"]:
                (Path(tmp) / name).write_text("https://example.com/" + name)
            config = agent.alert_config(settings, tmp)
            self.assertEqual(config["route"]["routes"][0]["matchers"], ['owner="payments"'])
            reserved = agent.alert_config(
                {"defaultReceiverKey": "default-url", "owners": {"default": "payments-url"}},
                tmp,
            )
            self.assertEqual(
                [r["name"] for r in reserved["receivers"]], ["default", "owner-default"]
            )
            self.assertFalse(
                config["receivers"][0]["webhook_configs"][0]["http_config"]["follow_redirects"]
            )
            for bad in [
                "http://example.com",
                "https://user:pass@example.com",
                "file:///etc/passwd",
                "",
            ]:
                (Path(tmp) / "default-url").write_text(bad)
                with self.assertRaises(ValueError):
                    agent.alert_config(settings, tmp)
            with self.assertRaises(ValueError):
                agent.alert_config({"defaultReceiverKey": "../secret", "owners": {}}, tmp)

    def test_support_bundle_excludes_sensitive_fields_and_never_mutates(self):
        obj = {
            "kind": "Pod",
            "metadata": {"name": "safe-name", "annotations": {"token": "SECRET"}},
            "spec": {"containers": [{"env": [{"value": "SECRET"}]}]},
            "status": {
                "phase": "Running",
                "conditions": [{"type": "Ready", "status": "False", "message": "SECRET"}],
            },
        }
        self.assertNotIn("SECRET", json.dumps(summarize("pods", [obj])))
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, json.dumps({"items": [obj]}))

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "bundle"
            collect("chosen-context", "observability", output, run)
            self.assertTrue(all("get" in c and "--context" in c for c in calls))
            self.assertNotIn("SECRET", "".join(f.read_text() for f in output.iterdir()))
            with self.assertRaises(FileExistsError):
                collect("chosen-context", "observability", output, run)

    def test_support_ownership_and_timing_fail_closed(self):
        for override in [
            {"notifications": {"enabled": True}},
            {"support": {"intervalSeconds": 60}},
            {"notifications": {"owners": {'bad"owner': "url"}}},
            {"notifications": {"secretName": "../bad"}},
        ]:
            with self.assertRaises(ValueError):
                validate_tuning(override)
        validate_tuning({"notifications": {"enabled": True, "manageTenantConfig": True}})

    def test_native_endpoints_and_readiness_do_not_mask_failed_checks(self):
        objects = list(
            yaml.safe_load_all(
                subprocess.check_output(
                    ["helm", "template", "olly", "charts/platform", "-n", "observability"],
                    text=True,
                )
            )
        )
        objects = {(o["kind"], o["metadata"]["name"]): o for o in objects if o}
        config = json.loads(objects["ConfigMap", "olly-support"]["data"]["settings.json"])
        self.assertEqual(config["alertmanager"], "http://mimir-alertmanager-headless:8080")
        probe = agent.Agent(config, "writer")
        self.assertFalse(probe.healthy())
        with patch.object(agent.time, "time", return_value=1000):
            probe.last_finished = 1000
            self.assertFalse(probe.healthy())
            probe.config["canary"]["enabled"] = True
            self.assertFalse(probe.healthy())
            probe.status = {"logs": 1, "metrics": 1, "traces": 1}
            self.assertTrue(probe.healthy())
            probe.last_finished = 759
            self.assertFalse(probe.healthy())
            probe.last_finished = 1001
            self.assertFalse(probe.healthy())
        spec = objects["Deployment", "olly-support"]["spec"]["template"]["spec"]
        self.assertFalse(spec["automountServiceAccountToken"])
        self.assertEqual(spec["containers"][0]["readinessProbe"]["httpGet"]["path"], "/ready")
        self.assertEqual([v["name"] for v in spec["volumes"]], ["config"])
        self.assertFalse(
            any(
                o["kind"] == "Secret" and o["metadata"]["name"] == "alert-receivers"
                for o in objects.values()
            )
        )

    def test_canary_uses_a_dedicated_existing_secret_and_server_enforced_limits(self):
        objects = yaml.safe_load_all(
            subprocess.check_output(
                [
                    "helm",
                    "template",
                    "olly",
                    "charts/platform",
                    "-n",
                    "observability",
                    "--set",
                    "support.canary.enabled=true",
                    "--set",
                    "support.canary.secretName=existing-canary",
                ],
                text=True,
            )
        )
        objects = {(o["kind"], o["metadata"]["name"]): o for o in objects if o}
        config = objects["ClickHouseInstallation", "observability"]["spec"]["configuration"]
        self.assertEqual(config["profiles"]["canary/readonly"], 1)
        self.assertEqual(config["profiles"]["canary/max_rows_to_read"], 100000)
        self.assertEqual(config["profiles"]["canary/skip_unavailable_shards"], 0)
        self.assertEqual(
            config["users"]["canary/password"]["valueFrom"]["secretKeyRef"],
            {"name": "existing-canary", "key": "password"},
        )
        spec = objects["Deployment", "olly-support"]["spec"]["template"]["spec"]
        self.assertEqual(
            next(v for v in spec["volumes"] if v["name"] == "clickhouse")["secret"]["secretName"],
            "existing-canary",
        )
        self.assertNotIn(("Secret", "existing-canary"), objects)
