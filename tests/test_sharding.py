import subprocess
import tempfile
import unittest
from pathlib import Path

import jsonschema
import yaml

from scripts.check_shard_changes import check
from scripts.tuning import validate_tuning
from tests.test_manifests import docs, strict

ROOT = Path(__file__).resolve().parents[1]


class Sharding(unittest.TestCase):
    def resources(self, shards=2):
        result = subprocess.run(
            [
                "helm",
                "template",
                "olly",
                str(ROOT / "charts/platform"),
                "-n",
                "observability",
                "--set",
                f"clickhouse.shards={shards}",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return {
            (d["kind"], d["metadata"]["name"]): d for d in yaml.safe_load_all(result.stdout) if d
        }

    def test_shards_are_replicated_and_az_affinity_is_scoped_to_each_shard(self):
        crd = next(
            c
            for c in docs("operators")
            if c["kind"] == "CustomResourceDefinition"
            and c["spec"]["names"]["kind"] == "ClickHouseInstallation"
        )
        schema = strict(
            next(v for v in crd["spec"]["versions"] if v["name"] == "v1")["schema"][
                "openAPIV3Schema"
            ]
        )
        for shards in [2, 3, 8]:
            d = self.resources(shards)
            chi = d["ClickHouseInstallation", "observability"]
            jsonschema.validate(chi, schema)
            cluster = chi["spec"]["configuration"]["clusters"][0]
            actual = cluster["layout"]["shards"]
            self.assertEqual([s["name"] for s in actual], [str(i) for i in range(shards)])
            self.assertTrue(
                all(
                    s["replicasCount"] == 3 and s["internalReplication"] == "Enabled"
                    for s in actual
                )
            )
            self.assertEqual(cluster["schemaPolicy"], {"replica": "All", "shard": "All"})
            self.assertIn("valueFrom", cluster["secret"])
            pod = chi["spec"]["templates"]["podTemplates"][0]
            self.assertEqual(
                pod["podDistribution"],
                [{"type": "ShardAntiAffinity", "topologyKey": "topology.kubernetes.io/zone"}],
            )
            self.assertNotIn("affinity", pod["spec"])

    def test_acknowledgements_and_distributed_schema(self):
        d = self.resources()
        profiles = d["ClickHouseInstallation", "observability"]["spec"]["configuration"]["profiles"]
        self.assertEqual(profiles["default/distributed_foreground_insert"], 1)
        self.assertEqual(profiles["default/insert_quorum"], 2)
        self.assertEqual(profiles["readonly/skip_unavailable_shards"], 0)
        sql = d["ConfigMap", "clickhouse-schema"]["data"]["schema.sql"]
        self.assertIn("cityHash64(cluster_name, service_key, event_id)", sql)
        self.assertIn("FROM observability.logs_local GROUP BY cluster_name", sql)
        self.assertIn("FROM observability.log_events_shards GROUP BY", sql)
        self.assertIn("Legacy log schema", sql)
        job = d["Job", "clickhouse-schema"]
        self.assertEqual(
            job["metadata"]["annotations"]["helm.sh/hook"], "post-install,post-upgrade"
        )
        self.assertIn("count() != 6", job["spec"]["template"]["spec"]["containers"][0]["args"][0])
        gateway = yaml.safe_load(d["ConfigMap", "vector-gateway"]["data"]["vector.yaml"])
        self.assertEqual(gateway["sinks"]["kafka"]["key_field"], "event_id")

    def test_unsafe_topology_and_shrink_are_rejected(self):
        for candidate in [{"clickhouse": {"shards": 1}}, {"clickhouse": {"shards": 33}}]:
            with self.assertRaises(ValueError):
                validate_tuning(candidate)
        self.assertTrue(check({"clickhouse": {"shards": 3}}, {"clickhouse": {"shards": 2}}, {}, {}))
        self.assertTrue(
            check(
                {},
                {},
                {"region": {"clickhouse": {"shards": 4}}},
                {"region": {"clickhouse": {"shards": 3}}},
            )
        )
        self.assertTrue(check({}, {}, {"region": {}}, {}))
        self.assertFalse(check({}, {"clickhouse": {"shards": 2}}, {"region": {}}, {"region": {}}))

    def test_expansion_requires_a_preceding_write_pause(self):
        old = {"clickhouse": {"shards": 2, "pauseWrites": False}}
        new = {"clickhouse": {"shards": 3, "pauseWrites": True}}
        self.assertTrue(check(old, new, {}, {}))
        old["clickhouse"]["pauseWrites"] = True
        self.assertFalse(check(old, new, {}, {}))
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml") as handle:
            yaml.safe_dump({"clickhouse": {"pauseWrites": True}}, handle)
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
                check=True,
                capture_output=True,
                text=True,
            )
        objects = {
            (d["kind"], d["metadata"]["name"]): d for d in yaml.safe_load_all(result.stdout) if d
        }
        self.assertEqual(objects["Deployment", "vector-writer"]["spec"]["replicas"], 0)
        self.assertNotIn(("HorizontalPodAutoscaler", "vector-writer"), objects)
        self.assertEqual(
            objects["ScaledObject", "vector-writer"]["metadata"]["annotations"][
                "autoscaling.keda.sh/paused-replicas"
            ],
            "0",
        )
        self.assertIn(("HorizontalPodAutoscaler", "vector-gateway"), objects)
