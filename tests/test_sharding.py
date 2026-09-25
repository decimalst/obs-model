import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

import jsonschema
import yaml

from scripts.check_shard_changes import check
from scripts.clickhouse_fixture import ClickHouseCluster
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
                all(s["replicasCount"] == 3 and s["internalReplication"] == "true" for s in actual)
            )
            self.assertEqual(cluster["schemaPolicy"], {"replica": "All", "shard": "All"})
            self.assertIn("valueFrom", cluster["secret"])
            pod = chi["spec"]["templates"]["podTemplates"][0]
            self.assertEqual(
                pod["podDistribution"],
                [{"type": "ShardAntiAffinity", "topologyKey": "topology.kubernetes.io/zone"}],
            )
            self.assertNotIn("affinity", pod["spec"])

    def test_runtime_fixture_preserves_operator_replication_tokens(self):
        # Pinned Altinity formats this field verbatim; a fixture that hardcodes
        # true hides CRD-valid but ClickHouse-invalid values such as Enabled.
        fleet = object.__new__(ClickHouseCluster)
        fleet.replicas, fleet.shards = 3, 2
        fleet.chi = self.resources()["ClickHouseInstallation", "observability"]
        xml = ET.fromstring(fleet.xml(0, 0))
        self.assertEqual(
            [node.text for node in xml.findall("remote_servers/logs/shard/internal_replication")],
            ["true", "true"],
        )
        fleet.chi["spec"]["configuration"]["clusters"][0]["layout"]["shards"][0][
            "internalReplication"
        ] = "Enabled"
        invalid_xml = ET.fromstring(fleet.xml(0, 0))
        self.assertEqual(
            invalid_xml.find("remote_servers/logs/shard/internal_replication").text,
            "Enabled",
        )

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

    def test_fixture_quorum_override_survives_fresh_growth_renders(self):
        rendered = {
            count: yaml.safe_dump(self.resources(count)["ClickHouseInstallation", "observability"])
            for count in [2, 3]
        }
        for quorum, replicas, expected in [(None, 3, 2), (1, 1, 1)]:
            with self.subTest(quorum=quorum), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "charts/platform").mkdir(parents=True)
                (root / "charts/platform/values.yaml").write_text(
                    "images:\n  clickhouse: fixture-image\n"
                )
                (root / "build/configs/clickhouse-schema").mkdir(parents=True)
                (root / "build/configs/clickhouse-schema/schema.sql").write_text("SELECT 1")
                (root / "build/platform.yaml").write_text(rendered[2])
                with (
                    patch("scripts.clickhouse_fixture.ROOT", root),
                    patch("scripts.clickhouse_fixture.command", side_effect=rendered.values()),
                    patch("scripts.clickhouse_fixture.wait"),
                    patch.object(ClickHouseCluster, "run", return_value="fixture-container"),
                    patch.object(ClickHouseCluster, "sql"),
                ):
                    cluster = ClickHouseCluster(replicas=replicas, insert_quorum=quorum)
                    self.assertEqual(
                        cluster.chi["spec"]["configuration"]["profiles"]["default/insert_quorum"],
                        expected,
                    )
                    for count in [2, 3]:
                        cluster.grow(count)
                        self.assertEqual(
                            cluster.chi["spec"]["configuration"]["profiles"][
                                "default/insert_quorum"
                            ],
                            expected,
                        )
                        for profile in cluster.directory.glob("ch-*/profiles.xml"):
                            self.assertEqual(
                                ET.parse(profile).findtext("profiles/default/insert_quorum"),
                                str(expected),
                            )

    def test_fixture_quorum_override_rejects_impossible_values(self):
        for value in [0, -1, 4, True, "1", 1.5]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                ClickHouseCluster(replicas=3, insert_quorum=value)

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
