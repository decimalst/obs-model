import copy
import tempfile
import unittest
from pathlib import Path

import yaml

from scripts.cell_plan import ROOT, grafana_database_identity, plan


class CellPlanning(unittest.TestCase):
    def test_aws_database_identity_is_derived_and_cannot_be_relabelled(self):
        p = {"secret_source": "aws", "grafana_db_host": "Shared.EXAMPLE.internal."}
        self.assertEqual(grafana_database_identity(p, {}), "shared.example.internal/grafana")
        self.assertEqual(
            grafana_database_identity(
                p, {"grafanaDatabaseIdentity": "SHARED.example.internal/grafana"}
            ),
            "shared.example.internal/grafana",
        )
        for label in ["label-a/grafana", "label-b/grafana", "shared.example.internal/other"]:
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, "contradicts"):
                grafana_database_identity(p, {"grafanaDatabaseIdentity": label})

    def test_shared_budget_scope_and_deploy_time_overrides(self):
        p = yaml.safe_load((ROOT / "examples/existing-platform.yaml").read_text())
        p["tuning"] = {"collectors": {"traceSamplingPercent": 25}}
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "platform.yaml").write_text(yaml.safe_dump(p))
            result = plan(
                {
                    "version": 1,
                    "mode": "shared",
                    "cells": [
                        {"id": "cell-a", "platform": "platform.yaml", "services": ["shop/checkout"]}
                    ],
                },
                tmp,
            )
        self.assertFalse(result["deployment_performed"])
        self.assertFalse(result["isolation_verified"])
        self.assertEqual(
            result["cells"][0]["budgets"]["tail_sampling_per_pod"]["baseline_percent"], 25
        )

    def test_isolated_cells_reject_shared_backends_and_duplicate_assignments(self):
        p = yaml.safe_load((ROOT / "examples/existing-platform.yaml").read_text())
        p["cluster_arn"] = "arn:aws:eks:us-east-1:123456789012:cluster/team-a"
        p["cluster_name"] = "team-a"
        q = copy.deepcopy(p)
        q["cluster_arn"] = p["cluster_arn"].replace("team-a", "team-b")
        q["cluster_name"] = "team-b"
        q["buckets"] = {k: v + "-b" for k, v in q["buckets"].items()}
        q["role_arns"] = {k: v + "-b" for k, v in q["role_arns"].items()}
        q["quota_redis_host"] = "independent.cache.amazonaws.com"
        config = {
            "version": 1,
            "mode": "isolated-clusters",
            "cells": [
                {
                    "id": "team-a",
                    "platform": "a.yaml",
                    "services": ["shop/checkout"],
                    "grafanaDatabaseIdentity": "db-a/grafana",
                },
                {
                    "id": "team-b",
                    "platform": "b.yaml",
                    "services": ["shop/catalog"],
                    "grafanaDatabaseIdentity": "db-b/grafana",
                },
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.yaml").write_text(yaml.safe_dump(p))
            (Path(tmp) / "b.yaml").write_text(yaml.safe_dump(q))
            self.assertEqual(len(plan(config, tmp)["cells"]), 2)
            for key in ["quota_redis_host", "buckets", "role_arns", "cluster_arn"]:
                bad = {**q, key: p[key]}
                (Path(tmp) / "b.yaml").write_text(yaml.safe_dump(bad))
                with self.assertRaises(ValueError):
                    plan(config, tmp)
            (Path(tmp) / "b.yaml").write_text(yaml.safe_dump(q))
            config["cells"][1]["services"] = ["shop/checkout"]
            with self.assertRaises(ValueError):
                plan(config, tmp)
            config["cells"][1]["services"] = ["shop/catalog"]
            for candidate in (p, q):
                candidate.update(
                    secret_source="aws",
                    grafana_db_host="shared.example.internal",
                    grafana_db_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:db",
                    quota_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:quota",
                )
            (Path(tmp) / "a.yaml").write_text(yaml.safe_dump(p))
            (Path(tmp) / "b.yaml").write_text(yaml.safe_dump(q))
            with self.assertRaisesRegex(ValueError, "contradicts"):
                plan(config, tmp)
            for cell in config["cells"]:
                del cell["grafanaDatabaseIdentity"]
            with self.assertRaisesRegex(ValueError, "grafana_database is shared"):
                plan(config, tmp)
            q["grafana_db_host"] = "independent.example.internal"
            (Path(tmp) / "b.yaml").write_text(yaml.safe_dump(q))
            self.assertEqual(len(plan(config, tmp)["cells"]), 2)
