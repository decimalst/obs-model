import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.gitops_export import bundle
from scripts.install import main as install
from scripts.platform_config import load_platform
from scripts.preflight import security_prerequisites, validate_generated_inputs
from scripts.regional_values import operator_values, values
from scripts.security_config import load_profile

ROOT = Path(__file__).resolve().parents[1]
DIGEST = "example/quota@sha256:" + "a" * 64


class SetupOptions(unittest.TestCase):
    def test_offline_installer_persists_profile_and_renders_without_remote_calls(self):
        run = subprocess.run
        with tempfile.TemporaryDirectory() as tmp:
            args = [
                "install",
                "--config",
                str(ROOT / "examples/existing-platform.yaml"),
                "--security-profile",
                str(ROOT / "examples/security-oidc-ingress.yaml"),
                "--quota-image",
                DIGEST,
                "--render-only",
                "--output-dir",
                tmp,
            ]
            with (
                patch("sys.argv", args),
                patch("scripts.install.preflight") as preflight,
                patch("scripts.install.subprocess.run", wraps=run) as command,
            ):
                install()
            preflight.assert_not_called()
            self.assertEqual(command.call_count, 1)
            self.assertEqual(command.call_args.args[0][:2], ["helm", "template"])
            p = load_platform(Path(tmp) / "platform.json")
            self.assertEqual(p["security_profile"]["authentication"]["mode"], "oauth")
            prerequisites = json.loads((Path(tmp) / "security-prerequisites.json").read_text())
            self.assertEqual(
                prerequisites["callbackURL"], "https://grafana.example.com/login/generic_oauth"
            )
            files = bundle(p, DIGEST, "gitops/clusters/test")
            value = files["platform/release.yaml"]["spec"]["values"]["grafana"]
            self.assertTrue(value["grafana.ini"]["auth.generic_oauth"]["role_attribute_strict"])
            self.assertFalse(any("external-policy" in name for name in files))

    def test_security_preflight_only_projects_keys_and_never_mutates(self):
        profile = load_profile(ROOT / "examples/security-istio.yaml")
        calls = []

        def reader(args):
            calls.append(args)
            return {
                "keys": {
                    k: True
                    for k in ["tls.crt", "tls.key", "client-id", "client-secret", "secret-key"]
                }
            }

        security_prerequisites(profile, reader)
        self.assertTrue(all("get" in c for c in calls))
        self.assertFalse(any("apply" in c or "patch" in c for c in calls))
        secret_calls = [c for c in calls if "secret" in c]
        self.assertTrue(secret_calls)
        self.assertTrue(all('printf "%q: true," $k' in c[-1] for c in secret_calls))
        with self.assertRaisesRegex(ValueError, "missing required keys"):
            security_prerequisites(profile, lambda args: {"keys": {}})

    def test_selected_security_profile_rejects_alternate_grafana_override_channels(self):
        platform = load_platform(ROOT / "examples/existing-platform.yaml")
        platform["security_profile"] = load_profile(ROOT / "examples/security-oidc-ingress.yaml")
        generated = values(platform, DIGEST)
        controllers = operator_values(platform)
        validate_generated_inputs(platform, generated, controllers)
        overrides = {
            "env": {"GF_AUTH_ANONYMOUS_ENABLED": "true"},
            "envValueFrom": {
                "GF_AUTH_ANONYMOUS_ENABLED": {
                    "configMapKeyRef": {"name": "unreviewed", "key": "anonymous"}
                }
            },
            "envFromSecret": "unreviewed",
            "envFromSecrets": [{"name": "unreviewed"}],
            "envFromConfigMaps": [{"name": "unreviewed"}],
            "args": ["--config=/unreviewed/grafana.ini"],
            "command": ["/bin/sh", "-c", "grafana server"],
            "extraConfigmapMounts": [{"name": "unreviewed", "mountPath": "/etc/grafana"}],
            "extraSecretMounts": [{"name": "unreviewed", "mountPath": "/etc/grafana"}],
        }
        for key, value in overrides.items():
            with self.subTest(channel=key):
                altered = copy.deepcopy(generated)
                if key == "envValueFrom":
                    altered["grafana"][key].update(value)
                else:
                    altered["grafana"][key] = value
                with self.assertRaisesRegex(ValueError, "selected security profile"):
                    validate_generated_inputs(platform, altered, controllers)
        # Every supported compiler output remains accepted, including mesh metadata.
        for example in ROOT.glob("examples/security-*.yaml"):
            with self.subTest(profile=example.name):
                platform["security_profile"] = load_profile(example)
                validate_generated_inputs(platform, values(platform, DIGEST), controllers)

    def test_without_security_profile_preserves_existing_overlay_validation(self):
        platform = load_platform(ROOT / "examples/existing-platform.yaml")
        generated = values(platform, DIGEST)
        generated["grafana"]["env"] = {"GF_LOG_LEVEL": "warn"}
        validate_generated_inputs(platform, generated, operator_values(platform))
