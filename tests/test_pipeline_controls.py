import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from scripts.regional_values import values
from scripts.tuning import DEFAULTS, load_tuning, merge, validate_tuning

ROOT = Path(__file__).resolve().parents[1]


class PipelineControls(unittest.TestCase):
    def render(self, tuning=None):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml") as handle:
            yaml.safe_dump(tuning or {}, handle)
            handle.flush()
            result = subprocess.run(
                [
                    "helm",
                    "template",
                    "olly",
                    str(ROOT / "charts/platform"),
                    "-n",
                    "observability",
                    "-f",
                    handle.name,
                ],
                capture_output=True,
                text=True,
            )
        return result

    def resources(self, tuning=None):
        result = self.render(tuning)
        self.assertEqual(result.returncode, 0, result.stderr)
        return {
            (d["kind"], d["metadata"]["name"]): d for d in yaml.safe_load_all(result.stdout) if d
        }

    def config(self, docs, role):
        return yaml.safe_load(docs["ConfigMap", f"otel-{role}"]["data"]["collector.yaml"])

    def test_arrow_links_preserve_trace_affinity_and_backend_protocols(self):
        for transport in ["arrow", "otlp"]:
            with self.subTest(transport=transport):
                d = self.resources({"collectors": {"interconnect": transport}})
                name = "otelarrow" if transport == "arrow" else "otlp"
                agent = self.config(d, "agent")
                self.assertIn("otlp", agent["receivers"])
                for signal in ["metrics", "traces"]:
                    exp = agent["exporters"][f"{name}/{signal}"]
                    if transport == "arrow":
                        self.assertTrue(exp["arrow"]["disable_downgrade"])
                    self.assertEqual(
                        agent["service"]["pipelines"][signal]["exporters"], [f"{name}/{signal}"]
                    )
                router = self.config(d, "router")
                self.assertIn(name, router["receivers"])
                self.assertEqual(router["exporters"]["loadbalancing"]["routing_key"], "traceID")
                self.assertEqual(list(router["exporters"]["loadbalancing"]["protocol"]), ["otlp"])
                self.assertIn("otlp/tempo", self.config(d, "traces")["exporters"])
                self.assertIn("prometheusremotewrite", self.config(d, "metrics")["exporters"])
                scrape = d["OpenTelemetryCollector", "otel-scrape"]["spec"]["config"]
                self.assertEqual(scrape["service"]["pipelines"]["metrics"]["exporters"], [name])

    def test_client_namespaces_cannot_bypass_processing(self):
        d = self.resources()
        policy = d["NetworkPolicy", "regional-platform"]["spec"]
        for rule in policy["ingress"]:
            for peer in rule["from"]:
                self.assertNotIn(
                    "observability/collect",
                    peer.get("namespaceSelector", {}).get("matchLabels", {}),
                )
        entry = d["NetworkPolicy", "otel-client-ingress"]["spec"]
        self.assertEqual(entry["podSelector"]["matchLabels"], {"app": "otel-agent"})
        labels = d["DaemonSet", "otel-agent"]["spec"]["template"]["metadata"]["labels"]
        self.assertEqual(labels["app"], "otel-agent")

    def test_cardinality_defenses_match_at_all_three_boundaries(self):
        d = self.resources()
        limits = yaml.safe_load(d["ConfigMap", "mimir-config"]["data"]["mimir.yaml"])["limits"]
        scrape = d["OpenTelemetryCollector", "otel-scrape"]["spec"]["config"]["receivers"][
            "prometheus"
        ]["config"]["scrape_configs"][0]
        self.assertEqual(scrape["metric_relabel_configs"], limits["metric_relabel_configs"])
        gateway = self.config(d, "metrics")
        conditions = gateway["processors"]["filter/cardinality"]["metric_conditions"]
        for rule in limits["metric_relabel_configs"]:
            self.assertEqual(rule["action"], "drop")
            self.assertEqual(rule["separator"], ";")
            self.assertEqual(len(rule["source_labels"]), 1)
            for label in rule["source_labels"]:
                self.assertTrue(any(f'"{label}"' in condition for condition in conditions))
        exporter = gateway["exporters"]["prometheusremotewrite"]
        self.assertFalse(exporter["target_info"]["enabled"])
        self.assertFalse(exporter["resource_to_telemetry_conversion"]["enabled"])
        self.assertLess(
            limits["max_global_series_per_metric"], limits["max_global_series_per_user"]
        )

    def test_tuning_reaches_native_configs_and_changes_rollout_checksum(self):
        base = self.resources()
        tuning = merge(
            load_tuning(ROOT / "examples/tuning.yaml"),
            {
                "quota": {"eventsPerSecond": 2000},
                "collectors": {
                    "arrowQueueItems": 7777,
                    "traceSamplingPercent": 35,
                    "scrapeSampleLimit": 4321,
                },
                "mimir": {
                    "mimir": {
                        "structuredConfig": {"limits": {"max_global_series_per_metric": 12345}}
                    }
                },
                "tempo": {"overrides": {"defaults": {"ingestion": {"rate_limit_bytes": 7000000}}}},
            },
        )
        validate_tuning(tuning)
        d = self.resources(tuning)
        self.assertEqual(
            self.config(d, "agent")["exporters"]["otelarrow/metrics"]["sending_queue"][
                "queue_size"
            ],
            7777,
        )
        limits = yaml.safe_load(d["ConfigMap", "mimir-config"]["data"]["mimir.yaml"])["limits"]
        self.assertEqual(limits["max_global_series_per_metric"], 12345)
        tempo = yaml.safe_load(d["ConfigMap", "tempo-config"]["data"]["tempo.yaml"])
        self.assertEqual(tempo["overrides"]["defaults"]["ingestion"]["rate_limit_bytes"], 7000000)
        for name in ["otel-metrics", "otel-router", "log-quota"]:
            self.assertNotEqual(
                base["Rollout", name]["spec"]["template"]["metadata"]["annotations"],
                d["Rollout", name]["spec"]["template"]["metadata"]["annotations"],
            )

    def test_bad_tuning_rejected_by_installer_and_chart(self):
        for candidate in [
            {"collectors": {"interconnect": "automatic"}},
            {"collectors": {"batchSize": 1025}},
            {"collectors": {"scrapeTimeout": "31s"}},
            {"collectors": {"traceErrorAllocation": 51}},
            {"quota": {"debugEventsPerSecond": 1001}},
            {"logs": {"agentBufferBytes": 1}},
            {"scaling": {"metricsPartitions": 2}},
            {"scaling": {"writerReplicas": 13}},
            {"scaling": {"extraKafkaBrokers": 2}},
            {"resources": {"agents": {"requests": {"memory": "1Gi"}}}},
            {"resources": {"writers": {"requests": {"cpu": "3000m"}}}},
            {
                "mimir": {
                    "mimir": {"structuredConfig": {"limits": {"max_global_series_per_metric": 0}}}
                }
            },
        ]:
            with self.subTest(candidate=candidate):
                with self.assertRaises(ValueError):
                    validate_tuning(candidate)
                self.assertNotEqual(self.render(candidate).returncode, 0)
        for candidate in [
            {"quota": {"redisTLS": False}},
            {"images": {"otel": "custom"}},
            {"collectors": {"queueSzie": 3}},
            {"networkPolicy": {"enabled": False}},
        ]:
            with self.assertRaises(ValueError):
                validate_tuning(candidate)

    def test_generation_persists_only_allowed_overrides(self):
        from tests.test_installation import configuration

        p = configuration()
        p["tuning"] = {"collectors": {"traceSamplingPercent": 42}}
        actual = values(p, "example/quota@sha256:" + "a" * 64)
        self.assertEqual(actual["collectors"]["traceSamplingPercent"], 42)
        self.assertEqual(actual["region"], p["region"])
        self.assertEqual(DEFAULTS["collectors"]["traceSamplingPercent"], 10)

    def test_expanded_capacity_preserves_quorum_and_consumer_limits(self):
        from tests.test_manifests import docs, strict

        d = self.resources(
            {
                "scaling": {"extraKafkaBrokers": 3, "logPartitions": 6, "metricsPartitions": 2},
                "mimir": {"ingester": {"replicas": 6}},
            }
        )
        self.assertEqual(d["KafkaNodePool", "quorum"]["spec"]["replicas"], 3)
        brokers = d["KafkaNodePool", "data"]
        self.assertEqual(brokers["spec"]["roles"], ["broker"])
        schema = next(
            c
            for c in docs("operators")
            if c["kind"] == "CustomResourceDefinition"
            and c["spec"]["names"]["kind"] == "KafkaNodePool"
        )
        import jsonschema

        version = next(v for v in schema["spec"]["versions"] if v["name"] == "v1")
        jsonschema.validate(brokers, strict(version["schema"]["openAPIV3Schema"]))
        hpa = d["ScaledObject", "vector-writer"]["spec"]
        self.assertEqual(hpa["maxReplicaCount"], 6)
        self.assertEqual(hpa["scaleTargetRef"]["kind"], "Deployment")
        self.assertEqual(d["KafkaTopic", "mimir-ingest"]["spec"]["partitions"], 2)

    def test_quota_environment_is_decimal_even_for_large_deploy_time_budgets(self):
        for tuning in [{}, {"quota": {"bytesPerSecond": 10000000, "byteBurst": 20000000}}]:
            d = self.resources(tuning)
            container = d["Rollout", "log-quota"]["spec"]["template"]["spec"]["containers"][0]
            quota = merge(DEFAULTS["quota"], tuning.get("quota", {}))
            field_map = {
                "EVENTS_PER_SECOND": "eventsPerSecond",
                "EVENT_BURST": "eventBurst",
                "BYTES_PER_SECOND": "bytesPerSecond",
                "BYTE_BURST": "byteBurst",
                "DEBUG_EVENTS_PER_SECOND": "debugEventsPerSecond",
                "DEBUG_EVENT_BURST": "debugEventBurst",
                "DEBUG_BYTES_PER_SECOND": "debugBytesPerSecond",
                "DEBUG_BYTE_BURST": "debugByteBurst",
            }
            env = {e["name"]: e.get("value") for e in container["env"]}
            for name, key in field_map.items():
                self.assertRegex(env[name], r"^[0-9]+$")
                self.assertEqual(int(env[name]), quota[key])

    def test_resource_overrides_reach_agents_gateways_and_stateful_components(self):
        tuning = {
            "resources": {
                "agents": {"limits": {"memory": "1Gi"}},
                "gateways": {"requests": {"cpu": "500m"}},
                "clickhouse": {"limits": {"memory": "8Gi"}},
                "keeper": {"requests": {"cpu": "250m"}},
            }
        }
        validate_tuning(tuning)
        d = self.resources(tuning)
        agent = d["DaemonSet", "otel-agent"]["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(agent["resources"]["limits"]["memory"], "1Gi")
        gateway = d["Rollout", "vector-gateway"]["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(gateway["resources"]["requests"]["cpu"], "500m")
        ch = d["ClickHouseInstallation", "observability"]["spec"]["templates"]["podTemplates"][0][
            "spec"
        ]["containers"][0]
        self.assertEqual(ch["resources"]["limits"]["memory"], "8Gi")
