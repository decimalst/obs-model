import copy
import importlib.util
import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import jsonschema
import yaml

from scripts.regional_values import values as regional_values
from scripts.tuning import validate_tuning

ROOT = Path(__file__).resolve().parents[1]
GATES = ROOT / "charts/platform/files/startup"


def topic(partitions=3, isr="0,1,2"):
    return "\n".join(
        f"Topic: logs Partition: {p} Leader: 0 Replicas: 0,1,2 Isr: {isr} Elr: N/A"
        for p in range(partitions)
    )


class StartupGates(unittest.TestCase):
    def kafka(self, description, expected=3):
        return subprocess.run(
            ["awk", "-v", f"expected={expected}", "-f", str(GATES / "topic-ready.awk")],
            input=description,
            text=True,
            capture_output=True,
            check=False,
        ).returncode

    def test_topic_requires_exact_partitions_leaders_and_real_isr_quorum(self):
        self.assertEqual(self.kafka(topic()), 0)
        self.assertEqual(self.kafka(topic(isr="0,1")), 0)
        for invalid in (
            "",
            "Topic logs does not exist",
            topic(2),
            topic(4),
            topic(isr="0"),
            topic(isr="0,0"),
            topic(isr="0,9"),
            topic().replace("Leader: 0", "Leader: -1"),
            topic().replace("Leader: 0", "Leader: 3"),
            topic().replace("logs", "foreign"),
            topic().replace("Partition: 2", "Partition: 1"),
            topic().replace("Replicas: 0,1,2", "Replicas: 0,1"),
            topic(isr="0,1").replace("Replicas: 0,1,2", "Replicas: 0,1,bad"),
            topic(isr="0,1").replace("Replicas: 0,1,2", "Replicas: 0,1,1"),
        ):
            with self.subTest(invalid=invalid):
                self.assertNotEqual(self.kafka(invalid), 0)

    def test_schema_rejects_missing_or_wrong_object_on_any_expected_replica(self):
        sql = (GATES / "schema-ready.sql").read_text()
        import re

        checks = re.findall(r"countIf\(name = '([^']+)' AND engine = '([^']+)'\)", sql)
        required = {
            "logs": "Distributed",
            "logs_local": "ReplicatedMergeTree",
            "log_events_local": "ReplicatedSummingMergeTree",
            "log_events_mv": "MaterializedView",
            "log_events_shards": "Distributed",
            "log_events": "View",
        }
        self.assertEqual(dict(checks), required)
        self.assertEqual(sql.count("!= {expected:UInt32}"), 6)
        self.assertIn("clusterAllReplicas('logs', system.tables)", sql)
        self.assertIn("skip_unavailable_shards = 0", sql)
        self.assertNotIn(
            "SELECT 1", "\n".join(s for s in sql.splitlines() if not s.startswith("--"))
        )

    def test_gateway_probe_is_bounded_and_suppresses_raw_failures(self):
        spec = importlib.util.spec_from_file_location("wait_gateway", GATES / "wait-gateway.py")
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        env = {"GATE_ATTEMPTS": "2", "GATE_DELAY_SECONDS": "1", "GATE_TIMEOUT_SECONDS": "5"}
        opener = Mock()
        opener.open.side_effect = OSError("PRIVATE-ERROR-BODY")
        with (
            patch.dict(os.environ, env),
            patch.object(probe.urllib.request, "build_opener", return_value=opener),
            patch.object(probe.time, "sleep") as sleep,
            patch("builtins.print") as output,
        ):
            self.assertEqual(probe.main(), 1)
        self.assertEqual(opener.open.call_count, 2)
        self.assertEqual(sleep.call_count, 1)
        self.assertNotIn("PRIVATE-ERROR-BODY", str(output.call_args_list))
        response = Mock(status=200)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener.open.side_effect = None
        opener.open.return_value = response
        with (
            patch.dict(os.environ, env),
            patch.object(probe.urllib.request, "build_opener", return_value=opener),
            patch("builtins.print"),
        ):
            self.assertEqual(probe.main(), 0)

    def test_chart_gates_only_log_path_with_existing_ownership_and_credentials(self):
        result = subprocess.run(
            ["helm", "template", "olly", str(ROOT / "charts/platform"), "-n", "observability"],
            capture_output=True,
            text=True,
            check=True,
        )
        workloads = {
            obj["metadata"]["name"]: obj
            for obj in yaml.safe_load_all(result.stdout)
            if obj and obj["kind"] in {"Rollout", "Deployment", "StatefulSet"}
        }
        for name in ("log-quota", "vector-gateway", "vector-writer"):
            pod = workloads[name]["spec"]["template"]
            self.assertIn("checksum/startup-gates", pod["metadata"]["annotations"])
            self.assertFalse(pod["spec"]["automountServiceAccountToken"])
            self.assertTrue(pod["spec"]["securityContext"]["runAsNonRoot"])
            init = pod["spec"]["initContainers"][0]
            self.assertTrue(init["securityContext"]["readOnlyRootFilesystem"])
            self.assertEqual(init["securityContext"]["capabilities"], {"drop": ["ALL"]})
            env = {entry["name"]: entry for entry in init["env"]}
            if name == "vector-writer":
                self.assertEqual(env["EXPECTED_REPLICAS"]["value"], "6")
                self.assertEqual(
                    env["CLICKHOUSE_PASSWORD"]["valueFrom"]["secretKeyRef"],
                    {"name": "clickhouse-auth", "key": "password"},
                )
            else:
                self.assertNotIn("CLICKHOUSE_PASSWORD", env)
            if name == "vector-gateway":
                self.assertEqual(init["image"], "apache/kafka:4.2.1")
                self.assertEqual(env["EXPECTED_PARTITIONS"]["value"], "36")
        for name in ("otel-router", "otel-metrics", "otel-traces"):
            self.assertNotIn("initContainers", workloads[name]["spec"]["template"]["spec"])
        vector = (ROOT / "charts/platform/templates/vector-config.yaml").read_text()
        self.assertIn("group_id: clickhouse-writer-v1", vector)
        self.assertEqual(vector.count("acknowledgements:\n          enabled: true"), 3)
        self.assertNotIn(
            "--wait ",
            (ROOT / "scripts/deploy.sh")
            .read_text()
            .split("helm upgrade --install olly")[1]
            .split("kubectl")[0],
        )
        self.assertIn(
            '"disableWait": name == "platform"', (ROOT / "scripts/gitops_export.py").read_text()
        )

    def test_schema_rejects_unbounded_or_malformed_gate_tuning(self):
        values = yaml.safe_load((ROOT / "charts/platform/values.yaml").read_text())
        schema = json.loads((ROOT / "charts/platform/values.schema.json").read_text())
        jsonschema.validate(values, schema)
        for field, value in (
            ("attempts", 0),
            ("attempts", 361),
            ("delaySeconds", 0),
            ("timeoutSeconds", 61),
            ("timeoutSeconds", "20"),
        ):
            modified = copy.deepcopy(values)
            modified["startupGates"][field] = value
            with (
                self.subTest(field=field, value=value),
                self.assertRaises(jsonschema.ValidationError),
            ):
                jsonschema.validate(modified, schema)

    def test_compiler_preserves_nondefault_gate_controls(self):
        tuning = {"startupGates": {"attempts": 7, "delaySeconds": 2, "timeoutSeconds": 9}}
        platform = {
            "region": "us-west-2",
            "cluster_name": "olly-us-west-2",
            "availability_zones": ["us-west-2a", "us-west-2b", "us-west-2c"],
            "quota_redis_host": "quota.us-west-2",
            "secret_source": "existing",
            "role_arns": {
                name: "arn:aws:iam::123456789012:role/" + name for name in ("mimir", "tempo")
            },
            "vpc_cidr": "10.41.0.0/16",
            "buckets": {
                name: "west-" + name
                for name in ("mimir-blocks", "mimir-ruler", "mimir-alertmanager", "tempo")
            },
            "tuning": tuning,
        }
        generated = regional_values(platform, "registry/quota@sha256:" + "a" * 64)
        self.assertEqual(generated["startupGates"], tuning["startupGates"])
        result = subprocess.run(
            [
                "helm",
                "template",
                "olly",
                str(ROOT / "charts/platform"),
                "-n",
                "observability",
                "-f",
                "-",
            ],
            input=yaml.safe_dump(generated),
            capture_output=True,
            text=True,
            check=True,
        )
        for obj in yaml.safe_load_all(result.stdout):
            if not obj or obj["kind"] not in {"Rollout", "Deployment"}:
                continue
            if obj["metadata"]["name"] not in {"log-quota", "vector-gateway", "vector-writer"}:
                continue
            env = {
                entry["name"]: entry.get("value")
                for entry in obj["spec"]["template"]["spec"]["initContainers"][0]["env"]
            }
            self.assertEqual(
                {
                    name: env[name]
                    for name in ("GATE_ATTEMPTS", "GATE_DELAY_SECONDS", "GATE_TIMEOUT_SECONDS")
                },
                {"GATE_ATTEMPTS": "7", "GATE_DELAY_SECONDS": "2", "GATE_TIMEOUT_SECONDS": "9"},
            )
        with self.assertRaisesRegex(ValueError, "Unsupported tuning field"):
            validate_tuning({"startupGates": {"disable": True}})
        with self.assertRaisesRegex(ValueError, "Invalid tuning"):
            validate_tuning({"startupGates": {"attempts": 0}})
