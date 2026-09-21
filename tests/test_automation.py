import unittest
from unittest.mock import patch

import yaml

from scripts.bootstrap_crds import AltinityLoader, UniqueLoader
from scripts.regional_values import values
from scripts.seed_secrets import apply_secret


class Automation(unittest.TestCase):
    def test_yaml_merge_normalization_never_changes_schema_types(self):
        parsed = yaml.load(
            "base: &base {type: string, description: inherited}\nvalue: {<<: *base, description: specific}",
            Loader=AltinityLoader,
        )
        self.assertEqual(parsed["value"], {"type": "string", "description": "specific"})
        with self.assertRaises(ValueError):
            yaml.load("type: string\ntype: object", Loader=AltinityLoader)
        with self.assertRaises(ValueError):
            yaml.load("description: one\ndescription: two", Loader=UniqueLoader)

    def test_regional_values_do_not_mix_egress_coordinates(self):
        platform = {
            "region": "us-west-2",
            "cluster_name": "olly-us-west-2",
            "availability_zones": ["us-west-2a", "us-west-2b", "us-west-2c"],
            "quota_redis_host": "quota.us-west-2",
            "vpc_cidr": "10.41.0.0/16",
            "buckets": {
                k: "west-" + k
                for k in ["mimir-blocks", "mimir-ruler", "mimir-alertmanager", "tempo"]
            },
        }
        generated = values(platform, "registry/quota@sha256:" + "a" * 64)
        text = yaml.safe_dump(generated)
        self.assertNotIn("us-east-1", text)
        self.assertIn("s3.us-west-2.amazonaws.com", text)
        self.assertEqual(generated["networkPolicy"]["vpcCIDR"], "10.41.0.0/16")
        with self.assertRaises(ValueError):
            values(platform, "registry/quota:latest")

    @patch("scripts.seed_secrets.command")
    @patch("scripts.seed_secrets.subprocess.run")
    def test_existing_generated_secrets_are_not_rotated(self, run, command):
        run.return_value.stdout = "secret/clickhouse-auth\n"
        apply_secret("clickhouse-auth", {"password": "new"}, preserve=True)
        command.assert_not_called()
        self.assertTrue(run.call_args.kwargs["check"])


if __name__ == "__main__":
    unittest.main()
