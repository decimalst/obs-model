import copy
import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import jsonschema
import yaml

from scripts.build_site import FILES, build
from scripts.platform_config import load_platform
from scripts.service_catalog import (
    ROOT,
    compiled,
    dashboard,
    load_catalog,
    publish,
    scaffold,
    slo_rules,
    validate_service,
)
from scripts.tuning import validate_tuning


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.service = json.loads((ROOT / "examples/service.json").read_text())

    def test_schema_failure_cases(self):
        for update in [
            {"password": "secret"},
            {"repository": "javascript:bad"},
            {"name": "../bad"},
            {"slo": {"enabled": True, "target": 100, "source": "application"}},
            {"profiling": "true"},
        ]:
            with self.subTest(update=update), self.assertRaises(jsonschema.ValidationError):
                validate_service({**self.service, **update})
        with self.assertRaises(ValueError):
            validate_service({**self.service, "dependencies": ["applications/checkout"]})

    def test_duplicate_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ["one", "two"]:
                (Path(tmp) / f"{name}.json").write_text(json.dumps(self.service))
            with self.assertRaises(ValueError):
                load_catalog(tmp)

    def test_escape_content_and_slo_scope(self):
        s = copy.deepcopy(self.service)
        s["description"] = "<script>alert(1)</script>"
        text = json.dumps(dashboard([s]))
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertEqual(slo_rules(s), [])
        s["slo"]["enabled"] = True
        rules = slo_rules(s)
        self.assertEqual(len(rules), 6)
        self.assertEqual(
            [r["labels"]["severity"] for r in rules if "alert" in r], ["critical", "warning"]
        )
        self.assertIn("service_requests_total", rules[0]["expr"])
        s["slo"]["source"] = "observed-spans"
        self.assertIn('otel_metric_overflow!="true"', slo_rules(s)[0]["expr"])
        self.assertIn("SPAN_KIND_SERVER", slo_rules(s)[0]["expr"])

    def test_compiled_files_are_current(self):
        for name, content in compiled(load_catalog(ROOT / "catalog/services")).items():
            self.assertEqual((ROOT / name).read_text(), content)

    def test_scaffold_never_overwrites_and_publish_requires_exact_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "checkout"
            scaffold(self.service, dest)
            self.assertIn(
                "observability/service: checkout",
                (dest / "chart/templates/deployment.yaml").read_text(),
            )
            self.assertIn("go.opentelemetry.io/otel v1.40.0", (dest / "go.mod").read_text())
            self.assertTrue((dest / "go.sum").is_file())
            command = [
                "helm",
                "template",
                "checkout",
                str(dest / "chart"),
                "-n",
                "applications",
                "--set",
                "image=example/service@sha256:" + "a" * 64,
            ]
            result = subprocess.check_output(command, text=True)
            documents = list(yaml.safe_load_all(result))
            self.assertEqual(documents[0]["kind"], "PodDisruptionBudget")
            with self.assertRaises(subprocess.CalledProcessError):
                subprocess.run(command + ["--set", "replicas=1"], check=True, capture_output=True)
            with self.assertRaises(subprocess.CalledProcessError):
                subprocess.run(command + ["-n", "wrong"], check=True, capture_output=True)

            with self.assertRaises(ValueError):
                scaffold(self.service, dest)
            with (
                patch("scripts.service_catalog.subprocess.run") as run,
                self.assertRaises(ValueError),
            ):
                publish(dest, "example/checkout", "wrong")
            run.assert_not_called()

    def test_public_site_is_an_explicit_allowlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            build(tmp)
            self.assertEqual({p.name for p in Path(tmp).iterdir()}, set(FILES) | {".nojekyll"})
            with self.assertRaises(ValueError):
                build(tmp)

    def test_studio_contracts_if_generated(self):
        path = ROOT / "build/studio/existing.json"
        if not path.exists():
            self.skipTest("Run make studio-test for compiler cross-validation")
        p = load_platform(path)
        self.assertTrue(p["profiling"]["enabled"])
        validate_tuning(json.loads((ROOT / "build/studio/new-tuning.json").read_text()))
        with zipfile.ZipFile(ROOT / "build/studio/bundle.zip") as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(
                json.loads(archive.read("platform.json")), json.loads(path.read_text())
            )
        with zipfile.ZipFile(ROOT / "build/studio/unicode.zip") as archive:
            self.assertEqual(archive.read("README.md").decode(), "Hello — κόσμος\n")

    def test_slo_fixture_matches_compiler(self):
        s = copy.deepcopy(self.service)
        s["slo"]["enabled"] = True
        expected = {"groups": [{"name": "service-catalog-slos", "rules": slo_rules(s)}]}
        self.assertEqual(
            yaml.safe_load((ROOT / "tests/rules/catalog.fixture.yaml").read_text()), expected
        )
