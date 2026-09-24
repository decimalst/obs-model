"""Cross-check browser exports against the actual installer and offline planners."""

import copy
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from scripts.build_site import FILES, build
from scripts.cell_plan import plan as cell_plan
from scripts.platform_config import validate_platform
from scripts.qualification import configuration
from scripts.qualification import plan as qualification_plan
from scripts.regional_values import values
from scripts.security_config import validate_profile
from scripts.tuning import DEFAULTS, validate_tuning

ROOT = Path(__file__).resolve().parents[1]
NODE = os.environ.get("NODE", str(ROOT / ".cache/tools/node/bin/node"))
if not Path(NODE).is_file():
    NODE = "node"


def compiler(changes=None):
    program = """
      import {defaults, compileRequest} from './site/model.mjs';
      import {readFileSync} from 'node:fs';
      const r = {...structuredClone(defaults),
        zones:['us-east-1a','us-east-1b','us-east-1c'],
        adminRole:'arn:aws:iam::123456789012:role/Admin',
        ...JSON.parse(readFileSync(0,'utf8'))};
      process.stdout.write(JSON.stringify(compileRequest(r)));
    """
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", program],
        cwd=ROOT,
        input=json.dumps(changes or {}),
        text=True,
        capture_output=True,
    )
    if result.returncode:
        raise ValueError("Studio compiler rejected test input: " + result.stderr)
    return json.loads(result.stdout)


def existing():
    p = yaml.safe_load((ROOT / "examples/existing-platform.yaml").read_text())
    p["cluster_name"] = "existing-eks"
    p["cluster_arn"] = "arn:aws:eks:us-east-1:123456789012:cluster/existing-eks"
    return dict(
        mode="existing",
        name=p["cluster_name"],
        region=p["region"],
        vpcCIDR=p["vpc_cidr"],
        zones=p["availability_zones"],
        existing=p,
    )


class AdvancedStudio(unittest.TestCase):
    def test_default_privacy_and_graph_policy_match_backend(self):
        files = compiler()
        tuning = validate_tuning(json.loads(files["tuning.json"]))
        for field in ("privacy", "serviceGraph"):
            self.assertEqual(tuning[field], DEFAULTS[field])
        self.assertNotIn("security-profile.json", files)
        self.assertNotIn("qualification.json", files)
        self.assertIn("--render-only", files["README.md"])

    def test_all_security_profiles_compile_to_installer_contracts(self):
        for path in ROOT.glob("examples/security-*.yaml"):
            with self.subTest(profile=path.name):
                profile = yaml.safe_load(path.read_text())
                files = compiler({**existing(), "securityProfile": profile})
                self.assertEqual(json.loads(files["security-profile.json"]), profile)
                validate_profile(profile)
                p = validate_platform(json.loads(files["platform.json"]))
                self.assertEqual(p["security_profile"], profile)
                generated = values(p, "example/quota@sha256:" + "a" * 64)
                self.assertIn("GF_SECURITY_SECRET_KEY", generated["grafana"]["envValueFrom"])
                self.assertIn("--security-profile", files["README.md"])

    def test_privacy_graph_export_matches_deploy_time_validation(self):
        policy = yaml.safe_load((ROOT / "examples/tuning-privacy-graphs.yaml").read_text())
        for mode in ("redact", "drop"):
            with self.subTest(mode=mode):
                changes = copy.deepcopy(policy)
                changes["privacy"]["logs"]["mode"] = mode
                # Examples can be partial; studio emits complete editable settings.
                from scripts.tuning import merge

                changes = {key: merge(DEFAULTS[key], val) for key, val in changes.items()}
                files = compiler(changes)
                tuning = validate_tuning(json.loads(files["tuning.json"]))
                self.assertEqual(tuning["privacy"], changes["privacy"])
                self.assertEqual(tuning["serviceGraph"], changes["serviceGraph"])

    def test_qualification_exports_remain_pending_and_use_selected_environment(self):
        for path in [
            ROOT / "operations/qualification.example.yaml",
            ROOT / "examples/recovery-existing-external.yaml",
            ROOT / "examples/recovery-cross-region.yaml",
        ]:
            with self.subTest(profile=path.name), tempfile.TemporaryDirectory() as tmp:
                q = yaml.safe_load(path.read_text())
                if not q["recovery"]["crossRegion"]["enabled"]:
                    q["recovery"]["destination"]["region"] = "us-east-1"
                files = compiler({**existing(), "qualification": q})
                output = Path(tmp) / "qualification.json"
                output.write_text(files["qualification.json"])
                config = configuration(output)
                self.assertEqual(config["environment"]["mode"], "software-only")
                self.assertEqual(config["environment"]["cluster"], "existing-eks")
                self.assertEqual(config["features"]["profiling"], False)
                self.assertEqual(config["features"]["serviceGraphs"], False)
                self.assertEqual(config["deploymentArtifacts"], q["deploymentArtifacts"])
                batch = qualification_plan(config)
                self.assertEqual(len(batch["gates"]), 26)
                self.assertEqual(batch["execution"], "deferred-to-operator")
                self.assertNotIn("receipts", files)

    def test_cell_exports_feed_offline_planner_without_claiming_isolation(self):
        for mode in ("shared", "isolated-clusters"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                files = compiler(
                    {
                        **existing(),
                        "isolation": {
                            "enabled": True,
                            "mode": mode,
                            "id": "checkout-east",
                            "services": ["shop/checkout"],
                            "grafanaDatabaseIdentity": "db.example.internal/grafana",
                            "additionalCells": [],
                        },
                    }
                )
                (Path(tmp) / "platform.json").write_text(files["platform.json"])
                config = json.loads(files["cells.json"])
                report = cell_plan(config, tmp)
                self.assertFalse(report["isolation_verified"])
                self.assertFalse(report["deployment_performed"])

    def test_new_cell_budget_uses_reviewed_contract_instead_of_raw_terraform_output(self):
        files = compiler(
            {
                "logEventsPerSecond": 7654,
                "traceBaselinePercent": 27,
                "isolation": {
                    "enabled": True,
                    "mode": "shared",
                    "id": "new-region",
                    "services": ["shop/checkout"],
                    "grafanaDatabaseIdentity": "",
                    "additionalCells": [],
                },
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            raw = existing()["existing"]
            (Path(tmp) / "platform.json").write_text(json.dumps(raw))
            reviewed = {**raw, "tuning": json.loads(files["tuning.json"])}
            (Path(tmp) / "platform.reviewed.json").write_text(json.dumps(reviewed))
            report = cell_plan(json.loads(files["cells.json"]), tmp)
        budgets = report["cells"][0]["budgets"]
        self.assertEqual(budgets["logs_per_namespace_service"]["events_per_second"], 7654)
        self.assertEqual(budgets["tail_sampling_per_pod"]["baseline_percent"], 27)
        self.assertIn("platform.reviewed.json", files["OPERATIONS.md"])

    def test_static_publish_contains_complete_module_graph_and_no_private_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            build(tmp)
            self.assertEqual({p.name for p in Path(tmp).iterdir()}, set(FILES) | {".nojekyll"})
            for path in Path(tmp).glob("*.mjs"):
                for imported in re.findall(r'from\s+[\'"]\./([^\'"]+)[\'"]', path.read_text()):
                    self.assertTrue((Path(tmp) / imported).is_file(), imported)
            self.assertFalse((Path(tmp) / "deployment-request.json").exists())


if __name__ == "__main__":
    unittest.main()
