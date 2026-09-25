"""Verify supplemental operator API access without adopting its generated policy."""

import ipaddress
import subprocess
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


class OperatorNetwork(unittest.TestCase):
    def render(self, *settings):
        command = [
            "helm",
            "template",
            "operators",
            str(ROOT / "charts/operators"),
            "-n",
            "observability-system",
        ]
        for setting in settings:
            command.extend(["--set", setting])
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
        return [d for d in yaml.safe_load_all(result.stdout) if d]

    def policy(self, objects):
        policies = [d for d in objects if d["kind"] == "NetworkPolicy"]
        self.assertEqual(len(policies), 1)
        return policies[0]

    def test_policy_is_additive_and_selects_only_this_release_operator(self):
        objects = self.render()
        policy = self.policy(objects)
        deployment = next(
            d
            for d in objects
            if d["kind"] == "Deployment"
            and d["metadata"]["name"] == "operators-opentelemetry-operator"
        )
        selector = policy["spec"]["podSelector"]["matchLabels"]
        self.assertEqual(
            selector,
            {
                **deployment["spec"]["selector"]["matchLabels"],
                "app.kubernetes.io/instance": "operators",
            },
        )
        self.assertTrue(
            selector.items() <= deployment["spec"]["template"]["metadata"]["labels"].items()
        )
        self.assertEqual(policy["metadata"]["namespace"], deployment["metadata"]["namespace"])
        self.assertNotEqual(policy["metadata"]["name"], "opentelemetry-operator")
        self.assertNotIn("ownerReferences", policy["metadata"])
        self.assertEqual(policy["metadata"]["labels"]["app.kubernetes.io/managed-by"], "Helm")
        self.assertEqual(policy["spec"]["policyTypes"], ["Egress"])
        self.assertNotIn("ingress", policy["spec"])

    def test_https_covers_service_dnat_and_endpoint_rotation_with_scoped_dns(self):
        policy = self.policy(self.render())
        https, dns = policy["spec"]["egress"]
        self.assertEqual(https["ports"], [{"protocol": "TCP", "port": 443}])
        self.assertEqual(len(https["to"]), 1)
        network = ipaddress.ip_network(https["to"][0]["ipBlock"]["cidr"])
        # Egress policy may run before or after Service DNAT. Endpoint IPs also
        # change independently of Helm, so pinning current /32s is insufficient.
        for address in ("172.20.0.1", "10.72.22.18", "10.72.49.187", "10.72.55.22"):
            self.assertIn(ipaddress.ip_address(address), network)
        self.assertEqual(
            dns["to"],
            [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                    }
                }
            ],
        )
        self.assertEqual(
            dns["ports"], [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]
        )

    def test_reused_operator_receives_no_policy(self):
        objects = self.render("components.opentelemetry-operator=false")
        self.assertFalse(any(d["kind"] == "NetworkPolicy" for d in objects))

    def test_supported_name_and_namespace_overrides_preserve_selector(self):
        objects = self.render(
            "opentelemetry-operator.nameOverride=regional-otel",
            "opentelemetry-operator.namespaceOverride=regional-controllers",
        )
        policy = self.policy(objects)
        self.assertEqual(policy["metadata"]["namespace"], "regional-controllers")
        self.assertEqual(
            policy["spec"]["podSelector"]["matchLabels"]["app.kubernetes.io/name"], "regional-otel"
        )


if __name__ == "__main__":
    unittest.main()
