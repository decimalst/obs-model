import copy
import unittest

import jsonschema
import yaml

from scripts.check_shard_changes import check
from scripts.gitops_export import bundle
from scripts.preflight import preflight
from scripts.tuning import validate_tuning
from tests import test_pipeline_controls
from tests.test_installation import DIGEST, Inventory, configuration
from tests.test_manifests import docs, strict


class ConsumerScaling(unittest.TestCase):
    def resources(self, tuning=None):
        return test_pipeline_controls.PipelineControls().resources(tuning or {})

    def test_lag_scaler_has_single_owner_and_matches_durable_consumer(self):
        d = self.resources()
        scaler = d["ScaledObject", "vector-writer"]
        spec = scaler["spec"]
        self.assertNotIn(("HorizontalPodAutoscaler", "vector-writer"), d)
        self.assertEqual(spec["minReplicaCount"], 3)
        self.assertEqual(spec["maxReplicaCount"], 12)
        self.assertEqual(d["KafkaTopic", "logs"]["spec"]["partitions"], 36)
        trigger = spec["triggers"][0]
        self.assertEqual(trigger["type"], "kafka")
        self.assertEqual(trigger["metadata"]["lagThreshold"], "5000")
        self.assertEqual(trigger["metadata"]["allowIdleConsumers"], "false")
        self.assertEqual(trigger["metadata"]["excludePersistentLag"], "false")
        self.assertEqual(spec["fallback"]["behavior"], "currentReplicasIfHigher")
        writer = yaml.safe_load(d["ConfigMap", "vector-writer"]["data"]["vector.yaml"])
        source = writer["sources"]["kafka"]
        self.assertEqual(trigger["metadata"]["consumerGroup"], source["group_id"])
        self.assertEqual(trigger["metadata"]["offsetResetPolicy"], source["auto_offset_reset"])
        self.assertTrue(source["acknowledgements"]["enabled"])
        self.assertTrue(writer["sinks"]["clickhouse"]["acknowledgements"]["enabled"])
        self.assertEqual(
            source["librdkafka_options"]["partition.assignment.strategy"], "cooperative-sticky"
        )
        crd = next(
            d
            for d in docs("operators")
            if d["kind"] == "CustomResourceDefinition"
            and d["metadata"]["name"] == "scaledobjects.keda.sh"
        )
        jsonschema.validate(scaler, strict(crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]))

    def test_tuning_caps_replicas_and_cpu_fallback_has_no_competing_scaler(self):
        d = self.resources(
            {
                "writerAutoscaling": {
                    "maxReplicas": 24,
                    "lagThreshold": 10000,
                    "scaleUpPodsPerMinute": 6,
                }
            }
        )
        spec = d["ScaledObject", "vector-writer"]["spec"]
        self.assertEqual(spec["maxReplicaCount"], 24)
        self.assertEqual(
            spec["advanced"]["horizontalPodAutoscalerConfig"]["behavior"]["scaleUp"]["policies"][0][
                "value"
            ],
            6,
        )
        self.assertEqual(spec["triggers"][0]["metadata"]["lagThreshold"], "10000")
        d = self.resources({"writerAutoscaling": {"maxReplicas": 48, "mode": "cpu"}})
        self.assertNotIn(("ScaledObject", "vector-writer"), d)
        self.assertEqual(d["HorizontalPodAutoscaler", "vector-writer"]["spec"]["maxReplicas"], 36)
        d = self.resources({"autoscaling": {"enabled": False}})
        self.assertNotIn(("ScaledObject", "vector-writer"), d)
        self.assertNotIn(("HorizontalPodAutoscaler", "vector-writer"), d)
        for tuning in [
            {"writerAutoscaling": {"lagThreshold": 0}},
            {"writerAutoscaling": {"mode": "unknown"}},
            {"scaling": {"writerReplicas": 13}},
        ]:
            with self.assertRaises(ValueError):
                validate_tuning(tuning)

    def test_controller_ha_and_cross_namespace_kafka_access(self):
        operators = docs("operators")
        for name in ["keda-operator", "keda-operator-metrics-apiserver", "keda-admission-webhooks"]:
            deployment = next(
                d for d in operators if d["kind"] == "Deployment" and d["metadata"]["name"] == name
            )
            self.assertEqual(deployment["spec"]["replicas"], 2)
            pod = deployment["spec"]["template"]
            affinity = pod["spec"]["affinity"]["podAntiAffinity"][
                "requiredDuringSchedulingIgnoredDuringExecution"
            ][0]
            self.assertEqual(affinity["topologyKey"], "topology.kubernetes.io/zone")
            self.assertEqual(
                affinity["labelSelector"]["matchLabels"]["app"], pod["metadata"]["labels"]["app"]
            )
        peers = self.resources()["Kafka", "telemetry"]["spec"]["kafka"]["listeners"][0][
            "networkPolicyPeers"
        ]
        self.assertIn(
            {
                "namespaceSelector": {
                    "matchLabels": {"kubernetes.io/metadata.name": "observability-system"}
                },
                "podSelector": {"matchLabels": {"app": "keda-operator"}},
            },
            peers,
        )

    def test_existing_adapter_is_never_implicitly_replaced(self):
        p = configuration()
        inventory = Inventory(p)
        # A compatible existing KEDA is reusable; disabling installation omits all its CRDs.
        preflight(p, reader=inventory)
        documents = bundle(p, DIGEST, "gitops/clusters/test")
        self.assertFalse(
            documents["operators/release.yaml"]["spec"]["values"]["components"]["keda"]
        )
        self.assertFalse(
            any(d["spec"]["group"] == "keda.sh" for d in documents["crds/resources.yaml"])
        )
        p = copy.deepcopy(p)
        p["operators"]["keda"] = True
        with self.assertRaisesRegex(ValueError, "external metrics API already exists"):
            preflight(p, reader=Inventory(p))

    def test_unavailable_keda_blocks_lag_mode_but_cpu_mode_needs_no_keda(self):
        p = configuration()
        inventory = Inventory(p)

        def no_keda(args):
            result = inventory(args)
            if args[0] == "kubectl" and "get" in args:
                resource = args[args.index("get") + 1]
                if resource == "apiservices":
                    result["items"] = [
                        a
                        for a in result["items"]
                        if "external.metrics" not in a["metadata"]["name"]
                    ]
                elif resource == "crds":
                    result["items"] = [
                        c for c in result["items"] if not c["metadata"]["name"].endswith("keda.sh")
                    ]
                elif resource == "deployments":
                    result["items"] = [
                        d
                        for d in result["items"]
                        if d["metadata"]["labels"].get("app") != "keda-operator"
                    ]
            return result

        with self.assertRaisesRegex(ValueError, "Available external metrics API"):
            preflight(p, reader=no_keda)
        p["tuning"] = {"writerAutoscaling": {"mode": "cpu"}}
        preflight(p, reader=no_keda)

    def test_partition_counts_cannot_shrink_in_defaults_or_regional_overrides(self):
        old = {"scaling": {"logPartitions": 36}}
        self.assertTrue(check(old, {"scaling": {"logPartitions": 12}}, {}, {}))
        self.assertTrue(check(old, old, {"east": {"scaling": {"logPartitions": 48}}}, {"east": {}}))
        self.assertFalse(
            check(old, old, {"east": {}}, {"east": {"scaling": {"logPartitions": 48}}})
        )
