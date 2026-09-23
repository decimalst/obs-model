import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import jsonschema
import yaml

from scripts.gitops_export import bundle, write_bundle
from scripts.platform_config import load_platform

ROOT = Path(__file__).resolve().parents[1]


class GitOps(unittest.TestCase):
    def platform(self):
        return load_platform(ROOT / "examples/existing-platform.yaml")

    def test_flux_schemas_revision_sync_and_drift_ownership(self):
        files = bundle(self.platform(), "example/quota@sha256:" + "a" * 64, "gitops/clusters/test")
        schemas = {
            name: json.loads((ROOT / f"tests/schemas/flux-{name}.json").read_text())
            for name in ["helmrelease", "kustomization"]
        }
        for path, doc in files.items():
            if path.endswith("release.yaml"):
                jsonschema.validate(doc, schemas["helmrelease"])
                spec = doc["spec"]
                self.assertEqual(spec["chart"]["spec"]["reconcileStrategy"], "Revision")
                self.assertEqual(spec["driftDetection"]["mode"], "enabled")
                self.assertEqual(spec["install"]["crds"], "Skip")
                self.assertFalse(spec["install"]["createNamespace"])
                self.assertEqual(spec["storageNamespace"], spec["targetNamespace"])
        for doc in files["sync.yaml"]:
            jsonschema.validate(doc, schemas["kustomization"])
            self.assertFalse(doc["spec"]["prune"])
        spec = files["platform/release.yaml"]["spec"]
        self.assertEqual(spec["releaseName"], "olly")
        self.assertTrue(spec["upgrade"]["disableWait"])
        self.assertTrue(spec["values"]["rollouts"]["autoPromotionEnabled"])
        ignores = spec["driftDetection"]["ignore"]
        self.assertEqual(ignores[0]["paths"], ["/spec/replicas"])
        self.assertEqual(ignores[1]["paths"], ["/spec/selector/rollouts-pod-template-hash"])
        self.assertTrue(all("target" in ignore for ignore in ignores))

    def test_existing_controllers_emit_no_crds_and_kustomize_builds(self):
        p = self.platform()
        from scripts.platform_config import OPERATORS

        p["operators"] = {name: False for name in OPERATORS}
        files = bundle(p, "example/quota@sha256:" + "a" * 64, "gitops/clusters/test", False)
        self.assertEqual(files["crds/resources.yaml"], [])
        self.assertFalse(
            files["platform/release.yaml"]["spec"]["values"]["rollouts"]["autoPromotionEnabled"]
        )
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            write_bundle(files, directory)
            for path in ["crds", "operators", "platform"]:
                result = subprocess.run(
                    ["kubectl", "kustomize", str(directory / path)], capture_output=True, text=True
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_dashboards_queries_and_rules_are_mounted_from_git(self):
        docs = {
            (d["kind"], d["metadata"]["name"]): d
            for d in yaml.safe_load_all((ROOT / "build/platform.yaml").read_text())
            if d
        }
        dashboards = docs["ConfigMap", "olly-dashboards"]["data"]
        seen = set()
        for content in dashboards.values():
            dashboard = json.loads(content)
            self.assertNotIn(dashboard["uid"], seen)
            seen.add(dashboard["uid"])
            self.assertTrue(dashboard["panels"])
            for panel in dashboard["panels"]:
                if panel["type"] == "text":
                    self.assertIn(panel["options"]["mode"], ["html", "markdown"])
                else:
                    self.assertIn(panel["datasource"]["uid"], ["mimir", "tempo", "clickhouse"])
        self.assertIn("saved-queries", seen)
        rules = docs["ConfigMap", "olly-rules"]["data"]["platform.yaml"]
        self.assertEqual(
            yaml.safe_load(rules),
            yaml.safe_load((ROOT / "charts/platform/files/rules/platform.yaml").read_text()),
        )
        ruler = docs["Deployment", "mimir-ruler"]["spec"]["template"]["spec"]
        self.assertTrue(
            any(v.get("configMap", {}).get("name") == "olly-rules" for v in ruler["volumes"])
        )
        grafana = docs["Deployment", "grafana"]["spec"]["template"]["spec"]
        self.assertTrue(
            any(v.get("configMap", {}).get("name") == "olly-dashboards" for v in grafana["volumes"])
        )
        cfg = yaml.safe_load(docs["ConfigMap", "mimir-config"]["data"]["mimir.yaml"])
        self.assertFalse(cfg["ruler"]["enable_api"])
        self.assertEqual(cfg["ruler_storage"]["backend"], "local")

    def test_checked_in_regional_releases_render_and_validate(self):
        # Future regional PRs exercise this automatically; no AWS access required.
        for path in (ROOT / "gitops/clusters").glob("*/**/release.yaml"):
            with self.subTest(path=path):
                release = yaml.safe_load(path.read_text())
                schema = json.loads((ROOT / "tests/schemas/flux-helmrelease.json").read_text())
                jsonschema.validate(release, schema)
                spec = release["spec"]
                chart = (ROOT / spec["chart"]["spec"]["chart"]).resolve()
                self.assertTrue(chart.is_relative_to(ROOT / "charts"))
                with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml") as handle:
                    yaml.safe_dump(spec.get("values", {}), handle)
                    handle.flush()
                    result = subprocess.run(
                        [
                            "helm",
                            "template",
                            spec["releaseName"],
                            str(chart),
                            "-n",
                            spec["targetNamespace"],
                            "-f",
                            handle.name,
                        ],
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
