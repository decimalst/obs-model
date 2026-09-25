"""Exercise the deployment entry point with disposable, non-network clients."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from scripts.bootstrap_crds import packaged_crds

ROOT = Path(__file__).resolve().parents[1]


class DeployBootstrapTests(unittest.TestCase):
    def deploy(
        self, *, existing=False, cert_manager=True, otel=True, fail_list=False, fail_bootstrap=False
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for child in ("scripts", "build", "bin"):
                (root / child).mkdir()
            shutil.copyfile(ROOT / "scripts/deploy.sh", root / "scripts/deploy.sh")
            (root / "build/platform.yaml").write_text(
                yaml.safe_dump({"images": {"quota": "example/quota@sha256:" + "a" * 64}})
            )
            (root / "build/platform.operators.yaml").write_text(
                yaml.safe_dump(
                    {
                        "components": {
                            "cert-manager": cert_manager,
                            "opentelemetry-operator": otel,
                        }
                    }
                )
            )
            (root / "build/platform.json").write_text("{}")
            helper = """import json, os, pathlib, sys
with open(os.environ['TEST_CALLS'], 'a') as log:
    log.write(json.dumps([pathlib.Path(sys.argv[0]).name, *sys.argv[1:]]) + '\\n')
"""
            for script in (
                "preflight.py",
                "seed_secrets.py",
                "smoke_test.py",
                "helm_dependencies.py",
            ):
                (root / "scripts" / script).write_text(helper)
            (root / "scripts/bootstrap_crds.py").write_text(
                helper + "pathlib.Path('build/bootstrap-crds.yaml').write_text('')\n"
            )
            (root / "scripts/schema.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
            (root / "scripts/schema.sh").chmod(0o700)
            clients = (
                "#!"
                + sys.executable
                + "\n"
                + helper
                + """
name = pathlib.Path(sys.argv[0]).name
if name == 'helm' and 'components.opentelemetry-operator=false' in sys.argv:
    if os.environ['TEST_FAIL_BOOTSTRAP'] == 'yes':
        sys.exit(1)
if name == 'helm' and sys.argv[1] == 'list':
    if os.environ['TEST_FAIL_LIST'] == 'yes':
        sys.exit(1)
    releases = [{'name': 'operators', 'namespace': 'observability-system'}]
    print(json.dumps(releases if os.environ['TEST_EXISTING'] == 'yes' else []))
if name == 'kubectl' and sys.argv[1:3] == ['create', 'namespace']:
    print('apiVersion: v1\\nkind: Namespace\\nmetadata: {name: observability}')
if name == 'kubectl' and sys.argv[1:4] == ['apply', '-f', '-']:
    sys.stdin.read()
"""
            )
            for name in ("helm", "kubectl"):
                (root / "bin" / name).write_text(clients)
                (root / "bin" / name).chmod(0o700)
            log = root / "calls.jsonl"
            environment = {
                **os.environ,
                "PATH": str(root / "bin") + os.pathsep + os.environ["PATH"],
                "PYTHON": sys.executable,
                "TEST_CALLS": str(log),
                "TEST_EXISTING": "yes" if existing else "no",
                "TEST_FAIL_LIST": "yes" if fail_list else "no",
                "TEST_FAIL_BOOTSTRAP": "yes" if fail_bootstrap else "no",
            }
            result = subprocess.run(
                ["bash", "scripts/deploy.sh", "build/platform.yaml", "build/platform.json"],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=20,
            )
            return result, [json.loads(line) for line in log.read_text().splitlines()]

    def test_fresh_install_creates_watched_namespace_and_waits_before_otel(self):
        result, calls = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        upgrades = [call for call in calls if call[:2] == ["helm", "upgrade"]]
        self.assertEqual([call[3] for call in upgrades], ["operators", "operators", "olly"])
        namespace = next(call for call in calls if call[:3] == ["kubectl", "create", "namespace"])
        self.assertEqual(namespace[3], "observability")
        self.assertLess(calls.index(namespace), calls.index(upgrades[0]))
        self.assertIn("components.opentelemetry-operator=false", upgrades[0])
        self.assertIn("--wait", upgrades[0])
        self.assertIn("--skip-crds", upgrades[0])
        self.assertNotIn("--set", upgrades[1])
        seed = next(call for call in calls if call[0] == "seed_secrets.py")
        self.assertLess(calls.index(upgrades[1]), calls.index(seed))
        self.assertLess(calls.index(seed), calls.index(upgrades[2]))

    def test_upgrades_and_reused_controllers_never_disable_owned_otel(self):
        for options in ({"existing": True}, {"cert_manager": False}, {"otel": False}):
            with self.subTest(**options):
                result, calls = self.deploy(**options)
                self.assertEqual(result.returncode, 0, result.stderr)
                upgrades = [call for call in calls if call[:2] == ["helm", "upgrade"]]
                self.assertEqual([call[3] for call in upgrades], ["operators", "olly"])
                self.assertNotIn("--set", upgrades[0])

    def test_release_lookup_failure_cannot_be_mistaken_for_first_install(self):
        result, calls = self.deploy(fail_list=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(call[:2] == ["helm", "upgrade"] for call in calls))
        self.assertFalse(any(call[0] == "kubectl" for call in calls))

    def test_failed_controller_readiness_stops_before_otel_or_secret_seeding(self):
        result, calls = self.deploy(fail_bootstrap=True)
        self.assertNotEqual(result.returncode, 0)
        upgrades = [call for call in calls if call[:2] == ["helm", "upgrade"]]
        self.assertEqual(len(upgrades), 1)
        self.assertIn("components.opentelemetry-operator=false", upgrades[0])
        self.assertFalse(any(call[0] == "seed_secrets.py" for call in calls))

    def test_cert_manager_crds_remain_helm_owned_and_staged_render_has_no_users(self):
        dependencies = yaml.safe_load((ROOT / "charts/operators/Chart.yaml").read_text())[
            "dependencies"
        ]
        enabled = {dependency["name"]: False for dependency in dependencies}
        enabled["cert-manager"] = True
        self.assertEqual(packaged_crds(enabled), [])
        renders = {}
        for phase in ("false", "true"):
            result = subprocess.run(
                [
                    "helm",
                    "template",
                    "operators",
                    str(ROOT / "charts/operators"),
                    "-n",
                    "observability-system",
                    "--set",
                    "components.opentelemetry-operator=" + phase,
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            renders[phase] = [item for item in yaml.safe_load_all(result.stdout) if item]
        for objects in renders.values():
            crds = {
                item["metadata"]["name"]
                for item in objects
                if item["kind"] == "CustomResourceDefinition"
            }
            self.assertIn("certificates.cert-manager.io", crds)
            self.assertIn("issuers.cert-manager.io", crds)
        cert_users = lambda objects: {  # noqa: E731
            item["kind"] for item in objects if item["apiVersion"].startswith("cert-manager.io/")
        }
        self.assertEqual(cert_users(renders["false"]), set())
        self.assertEqual(cert_users(renders["true"]), {"Certificate", "Issuer"})
        watched_bindings = [
            item
            for item in renders["false"]
            if item["kind"] == "RoleBinding"
            and item["metadata"].get("namespace") == "observability"
        ]
        self.assertTrue(watched_bindings)


if __name__ == "__main__":
    unittest.main()
