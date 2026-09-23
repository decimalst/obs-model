import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from scripts.security_config import compile_profile, load_profile, validate_profile

ROOT = Path(__file__).resolve().parents[1]


class SecurityTests(unittest.TestCase):
    def profile(self):
        return load_profile(ROOT / "examples/security-oidc-ingress.yaml")

    def test_all_examples_compile_and_preserve_external_ownership(self):
        for path in sorted((ROOT / "examples").glob("security-*.yaml")):
            with self.subTest(path=path.name):
                result = compile_profile(load_profile(path))
                self.assertEqual(set(result["values"]), {"grafana"})
                self.assertNotIn("Secret", {o["kind"] for o in result["externalPolicyExamples"]})
                if result["prerequisites"]["externalPolicyRequired"]:
                    self.assertIn(
                        "unchanged",
                        result["prerequisites"]["coverage"]["otlpArrowVectorKafkaClickHousePeers"],
                    )
                    self.assertTrue(result["externalPolicyExamples"])
                else:
                    self.assertFalse(result["externalPolicyExamples"])

    def test_rejects_secret_material_unsafe_endpoints_and_privilege_expressions(self):
        for section, key, value in [
            ("authentication", "clientSecret", "NEVER_ECHO_THIS_VALUE"),
            ("authentication", "authURL", "https://user:NEVER_ECHO_THIS_VALUE@example.com"),
            ("authentication", "tokenURL", "http://example.com/token"),
            (
                "authentication",
                "userInfoURL",
                "https://example.com/user?token=NEVER_ECHO_THIS_VALUE",
            ),
            ("authentication", "groupsClaim", "groups[*] || 'Admin'"),
            ("authentication", "groupsClaim", "${SECRET}"),
            ("authentication", "secretName", "../escape"),
            ("authentication", "roles", {"GrafanaAdmin": ["admins"]}),
            ("authentication", "roles", {"Admin": ["all' || 'Admin"]}),
            ("authentication", "roles", {"Admin": ["duplicate"], "Viewer": ["duplicate"]}),
            ("authentication", "roles", {}),
            ("authentication", "scopes", ["email"]),
            ("access", "url", "https://grafana.example.com/subpath"),
            ("access", "url", "https://grafana.example.com\n[auth]\ndisable_login_form=false"),
            ("access", "url", "https://grafana.example.com:8443"),
        ]:
            p = self.profile()
            p[section][key] = value
            with self.subTest(section=section, key=key, value=value):
                with self.assertRaises(ValueError) as error:
                    validate_profile(p)
                self.assertNotIn("NEVER_ECHO_THIS_VALUE", str(error.exception))

    def test_local_mode_is_loopback_only_and_oauth_has_no_default_role(self):
        local = load_profile(ROOT / "examples/security-local.yaml")
        self.assertFalse(
            compile_profile(local)["values"]["grafana"]["grafana.ini"]["security"]["cookie_secure"]
        )
        for url in ("http://0.0.0.0:3000", "http://10.0.0.1:3000", "https://localhost:3000"):
            p = copy.deepcopy(local)
            p["access"]["url"] = url
            with self.assertRaises(ValueError):
                validate_profile(p)
        result = compile_profile(self.profile())
        ini = result["values"]["grafana"]["grafana.ini"]
        oauth = ini["auth.generic_oauth"]
        self.assertTrue(oauth["role_attribute_path"].endswith(" || ''"))
        self.assertTrue(oauth["role_attribute_strict"])
        self.assertFalse(oauth["allow_assign_grafana_admin"])
        self.assertFalse(ini["auth.basic"]["enabled"])
        self.assertTrue(ini["auth"]["disable_login_form"])
        self.assertTrue(oauth["use_pkce"])
        self.assertEqual(
            result["prerequisites"]["callbackURL"],
            "https://grafana.example.com/login/generic_oauth",
        )

    def test_mesh_policy_is_scoped_and_requires_exact_identities(self):
        for provider in ("istio", "linkerd"):
            p = load_profile(ROOT / f"examples/security-{provider}.yaml")
            result = compile_profile(p)
            policies = result["externalPolicyExamples"]
            self.assertTrue(all(o["metadata"]["namespace"] == "observability" for o in policies))
            if provider == "istio":
                self.assertEqual(policies[0]["spec"]["mtls"]["mode"], "STRICT")
                self.assertEqual(
                    policies[1]["spec"]["rules"][0]["to"][0]["operation"]["ports"], ["3000"]
                )
            else:
                self.assertEqual(policies[0]["spec"]["port"], 3000)
            for identities in ([], ["*"], ["cluster.local/ns/*/sa/*"]):
                bad = copy.deepcopy(p)
                bad["internalTransport"]["grafanaClientIdentities"] = identities
                with self.assertRaises(ValueError):
                    validate_profile(bad)

    def test_pinned_helm_renders_tls_secret_references_and_no_embedded_credentials(self):
        p = self.profile()
        p["authentication"]["caConfigMap"] = "private-oidc-ca"
        result = compile_profile(p)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "values.yaml"
            path.write_text(yaml.safe_dump(result["values"]))
            objects = list(
                yaml.safe_load_all(
                    subprocess.check_output(
                        [
                            "helm",
                            "template",
                            "olly",
                            str(ROOT / "charts/platform"),
                            "-n",
                            "observability",
                            "-f",
                            str(path),
                        ],
                        text=True,
                    )
                )
            )
        objects = {(o["kind"], o["metadata"]["name"]): o for o in objects if o}
        ingress = objects["Ingress", "grafana"]["spec"]
        self.assertEqual(ingress["tls"][0]["secretName"], "grafana-tls")
        self.assertEqual(ingress["ingressClassName"], "shared-ingress")
        grafana = objects["Deployment", "grafana"]["spec"]["template"]["spec"]
        container = next(c for c in grafana["containers"] if c["name"] == "grafana")
        env = {e["name"]: e for e in container["env"]}
        self.assertEqual(
            env["GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET"]["valueFrom"]["secretKeyRef"],
            {"name": "grafana-oauth", "key": "client-secret"},
        )
        self.assertIn("CLICKHOUSE_PASSWORD", env)  # Overlay must preserve datasource credentials.
        self.assertTrue(
            any(m["mountPath"] == "/etc/grafana/oidc-ca.pem" for m in container["volumeMounts"])
        )
        ini = objects["ConfigMap", "grafana"]["data"]["grafana.ini"]
        self.assertIn("client_secret = $__env{GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET}", ini)
        self.assertIn("role_attribute_strict = true", ini)
        self.assertIn("grafana-db", json.dumps(grafana))
        policy = objects["NetworkPolicy", "grafana"]["spec"]
        self.assertIn(
            {
                "namespaceSelector": {
                    "matchLabels": {"kubernetes.io/metadata.name": "ingress-system"}
                }
            },
            policy["ingress"][0]["from"],
        )


if __name__ == "__main__":
    unittest.main()
