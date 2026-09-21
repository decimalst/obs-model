import copy
import json
import subprocess
import unittest
from pathlib import Path

import jsonschema
import yaml

ROOT = Path(__file__).resolve().parents[1]


def docs(name):
    return [x for x in yaml.safe_load_all((ROOT / "build" / f"{name}.yaml").read_text()) if x]


def strict(schema):
    if isinstance(schema, list):
        return [strict(v) for v in schema]
    if not isinstance(schema, dict):
        return schema
    s = {k: strict(v) for k, v in schema.items()}
    if "properties" in s and not s.get("x-kubernetes-preserve-unknown-fields"):
        s.setdefault("additionalProperties", False)
    return s


class Manifests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.resources = docs("platform")
        cls.by_id = {(d["kind"], d["metadata"]["name"]): d for d in cls.resources}

    def test_custom_resources_against_pinned_crds(self):
        schemas = {}
        for crd in docs("operators") + docs("flink-operator") + self.resources:
            if crd["kind"] == "CustomResourceDefinition":
                for version in crd["spec"]["versions"]:
                    if version.get("served"):
                        key = (
                            crd["spec"]["group"] + "/" + version["name"],
                            crd["spec"]["names"]["kind"],
                        )
                        schemas[key] = strict(version["schema"]["openAPIV3Schema"])
        checked = []
        for d in self.resources:
            key = d["apiVersion"], d["kind"]
            if key in schemas:
                with self.subTest(resource=key, name=d["metadata"]["name"]):
                    jsonschema.Draft7Validator(schemas[key]).validate(d)
                    checked.append(d["kind"])
        for required in [
            "Kafka",
            "KafkaNodePool",
            "KafkaTopic",
            "ClickHouseInstallation",
            "Rollout",
            "OpenTelemetryCollector",
        ]:
            self.assertIn(required, checked)

    def test_routing_and_upgrade_contracts(self):
        for name in ["log-quota", "vector-gateway", "otel-metrics", "otel-router"]:
            d = self.by_id["Rollout", name]
            strategy = d["spec"]["strategy"]["blueGreen"]
            self.assertFalse(strategy["autoPromotionEnabled"])
            self.assertIn("prePromotionAnalysis", strategy)
            self.assertIn(("Service", strategy["previewService"]), self.by_id)
        sampler = self.by_id["StatefulSet", "otel-traces"]
        self.assertEqual(sampler["spec"]["updateStrategy"]["type"], "OnDelete")
        self.assertEqual(sampler["spec"]["replicas"], 3)
        router = yaml.safe_load(self.by_id["ConfigMap", "otel-router"]["data"]["collector.yaml"])
        exporter = router["exporters"]["loadbalancing"]
        self.assertEqual(exporter["routing_key"], "traceID")
        self.assertEqual(len(exporter["resolver"]["static"]["hostnames"]), 3)
        self.assertNotIn(("HorizontalPodAutoscaler", "otel-traces"), self.by_id)

    def test_no_error_bypass_and_no_log_otel_pipeline(self):
        agent = yaml.safe_load(self.by_id["ConfigMap", "vector-agent"]["data"]["vector.yaml"])
        self.assertNotIn("exclude", agent["transforms"]["node_budget"])
        source = agent["sources"]["kubernetes"]
        self.assertFalse(source["oldest_first"])
        self.assertEqual(source["max_read_bytes"], 2048)
        self.assertEqual(source["max_merged_line_bytes"], 32768)
        self.assertIn("log-quota", agent["sinks"]["quota"]["uri"])
        for d in self.resources:
            if d["kind"] == "ConfigMap" and "collector.yaml" in d.get("data", {}):
                self.assertNotIn(
                    "logs", yaml.safe_load(d["data"]["collector.yaml"])["service"]["pipelines"]
                )

    def test_durable_replicated_regional_storage(self):
        k = self.by_id["Kafka", "telemetry"]["spec"]["kafka"]
        self.assertEqual(k["config"]["min.insync.replicas"], 2)
        self.assertFalse(k["config"]["auto.create.topics.enable"])
        self.assertEqual(self.by_id["KafkaNodePool", "quorum"]["spec"]["replicas"], 3)
        self.assertEqual(self.by_id["StorageClass", "gp3-encrypted"]["reclaimPolicy"], "Retain")
        self.assertFalse(
            any(
                d["kind"] == "Service" and d["spec"].get("type") == "LoadBalancer"
                for d in self.resources
            )
        )
        for d in self.resources:
            if d["kind"] == "StatefulSet" and d["metadata"]["name"].startswith(
                ("mimir-ingester-", "tempo-ingester-", "mimir-store-gateway-")
            ):
                self.assertIn(
                    d["spec"]["template"]["spec"]["nodeSelector"]["topology.kubernetes.io/zone"],
                    ["us-east-1a", "us-east-1b", "us-east-1c"],
                )

    def test_duplicate_names_and_service_target_ports(self):
        ids = [
            (
                d["apiVersion"],
                d["kind"],
                d["metadata"].get("namespace", "observability"),
                d["metadata"]["name"],
            )
            for d in self.resources
        ]
        self.assertEqual(len(ids), len(set(ids)))
        for name in ["log-quota", "vector-gateway", "otel-metrics", "otel-router", "otel-traces"]:
            pod = self.by_id["StatefulSet" if name == "otel-traces" else "Rollout", name]["spec"][
                "template"
            ]["spec"]
            ports = {p["name"] for c in pod["containers"] for p in c["ports"]}
            service = self.by_id["Service", name]
            self.assertTrue(all(p["targetPort"] in ports for p in service["spec"]["ports"]))

    def test_values_schema_rejects_unsafe_values(self):
        values = yaml.safe_load((ROOT / "charts/platform/values.yaml").read_text())
        schema = json.loads((ROOT / "charts/platform/values.schema.json").read_text())
        jsonschema.validate(values, schema)
        for section, key, value in [
            ("quota", "replicas", 1),
            ("quota", "eventsPerSecond", 0),
            ("collectors", "traceSamplingPercent", 101),
            ("collectors", "replicas", 4),
            ("logs", "maxDebugHours", 25),
        ]:
            candidate = copy.deepcopy(values)
            candidate[section][key] = value
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(candidate, schema)

    def test_rolling_fallback_renders(self):
        result = subprocess.run(
            [
                "helm",
                "template",
                "olly",
                str(ROOT / "charts/platform"),
                "-n",
                "observability",
                "--set",
                "rollouts.enabled=false",
            ],
            text=True,
            capture_output=True,
            check=True,
        )
        resources = list(yaml.safe_load_all(result.stdout))
        self.assertFalse(any(d and d["kind"] == "Rollout" for d in resources))

    def test_optional_flink_rejects_missing_artifact_and_validates_complete_spec(self):
        args = [
            "helm",
            "template",
            "olly",
            str(ROOT / "charts/platform"),
            "-n",
            "observability",
            "--set",
            "flink.enabled=true",
        ]
        bad = subprocess.run(args, text=True, capture_output=True)
        self.assertNotEqual(bad.returncode, 0)
        good = subprocess.run(
            args
            + [
                "--set",
                "flink.image=example/job:1.0.0",
                "--set",
                "flink.jarURI=local:///opt/flink/job.jar",
                "--set",
                "flink.checkpointBucket=example-regional-checkpoints",
            ],
            text=True,
            capture_output=True,
            check=True,
        )
        deployment = next(
            d for d in yaml.safe_load_all(good.stdout) if d and d["kind"] == "FlinkDeployment"
        )
        crd = next(
            d
            for d in docs("flink-operator")
            if d["kind"] == "CustomResourceDefinition"
            and d["spec"]["names"]["kind"] == "FlinkDeployment"
        )
        schema = next(
            v["schema"]["openAPIV3Schema"]
            for v in crd["spec"]["versions"]
            if v["name"] == "v1beta1"
        )
        jsonschema.Draft7Validator(strict(schema)).validate({"spec": deployment["spec"]})
        self.assertEqual(deployment["spec"]["job"]["upgradeMode"], "savepoint")

    def test_debug_controller_artifact_matches_source(self):
        self.assertEqual(
            (ROOT / "scripts/debug_grants.py").read_bytes(),
            (ROOT / "charts/debug-grants/debug_grants.py").read_bytes(),
        )

    def test_aws_credentials_and_webhook_remain_reachable(self):
        egress = self.by_id["NetworkPolicy", "regional-platform"]["spec"]["egress"]
        self.assertTrue(
            any(
                {"ipBlock": {"cidr": "169.254.170.23/32"}} in rule.get("to", [])
                and {"protocol": "TCP", "port": 80} in rule.get("ports", [])
                for rule in egress
            )
        )
        webhook = self.by_id["NetworkPolicy", "mimir-rollout-webhook"]["spec"]
        deployment = self.by_id["Deployment", "olly-rollout-operator"]["spec"]
        labels = deployment["template"]["metadata"]["labels"]
        for key, value in webhook["podSelector"]["matchLabels"].items():
            self.assertEqual(labels[key], value)
        self.assertEqual(webhook["ingress"][0]["ports"], [{"protocol": "TCP", "port": 8443}])

    def test_target_allocator_has_redundant_az_configuration(self):
        allocator = self.by_id["OpenTelemetryCollector", "otel-scrape"]["spec"]["targetAllocator"]
        self.assertEqual(allocator["replicas"], 2)
        self.assertEqual(allocator["podDisruptionBudget"]["minAvailable"], 1)
        constraints = allocator["affinity"]["podAntiAffinity"][
            "requiredDuringSchedulingIgnoredDuringExecution"
        ]
        self.assertEqual(constraints[0]["topologyKey"], "topology.kubernetes.io/zone")


if __name__ == "__main__":
    unittest.main()
