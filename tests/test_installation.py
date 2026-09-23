import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from scripts.bootstrap_crds import packaged_crds
from scripts.install import main as install
from scripts.platform_config import OPERATORS, load_platform, validate_platform
from scripts.preflight import (
    CRDS,
    LABELS,
    preflight,
    ready_nodes,
    validate_generated_inputs,
    validate_storage_class,
)
from scripts.regional_values import operator_values, values
from scripts.seed_secrets import main as seed_secrets

ROOT = Path(__file__).resolve().parents[1]
DIGEST = "registry/quota@sha256:" + "a" * 64


def configuration():
    p = load_platform(ROOT / "examples/existing-platform.yaml")
    p["operators"] = {name: False for name in OPERATORS}
    return p


def nodes(p):
    return [
        {
            "metadata": {
                "labels": {
                    "topology.kubernetes.io/zone": az,
                    "kubernetes.io/arch": "amd64",
                    "kubernetes.io/os": "linux",
                }
            },
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        }
        for az in p["availability_zones"]
        for _ in range(2)
    ]


class Inventory:
    def __init__(self, p):
        self.p, self.calls = p, []
        self.endpoint = "https://correct.eks.amazonaws.com"
        self.bucket_region = p["region"]
        self.node_list = nodes(p)
        self.role_mismatch = False
        self.metrics_available = True

    def __call__(self, args):
        self.calls.append(args)
        p = self.p
        if args[0] == "aws":
            op = args[2]
            if op == "describe-cluster":
                return {
                    "cluster": {
                        "status": "ACTIVE",
                        "version": "1.35",
                        "arn": "arn:aws:eks:us-east-1:123456789012:cluster/my-existing-eks",
                        "endpoint": "https://correct.eks.amazonaws.com",
                        "resourcesVpcConfig": {"vpcId": "vpc-example"},
                    }
                }
            if op == "describe-vpcs":
                return {"Vpcs": [{"CidrBlock": p["vpc_cidr"]}]}
            if op == "get-bucket-location":
                return {"LocationConstraint": self.bucket_region}
            if op == "list-pod-identity-associations":
                return {
                    "associations": [
                        {"namespace": "observability", "serviceAccount": s, "associationId": s}
                        for s in ("mimir", "tempo")
                    ]
                }
            if op == "describe-pod-identity-association":
                service = args[args.index("--association-id") + 1]
                return {
                    "association": {
                        "roleArn": "wrong" if self.role_mismatch else p["role_arns"][service]
                    }
                }
        if args[:3] == ["kubectl", "config", "view"]:
            return {"clusters": [{"cluster": {"server": self.endpoint}}]}
        if args[:2] == ["helm", "list"]:
            return []
        if args[0] == "kubectl" and "get" in args:
            resource = args[args.index("get") + 1]
            if resource == "nodes":
                return {"items": self.node_list}
            if resource == "csidriver":
                return {}
            if resource == "daemonsets":
                return {
                    "items": [
                        {
                            "metadata": {"name": "eks-pod-identity-agent"},
                            "status": {"desiredNumberScheduled": 6, "numberReady": 6},
                        },
                        {
                            "metadata": {"name": "aws-node"},
                            "status": {"desiredNumberScheduled": 6, "numberReady": 6},
                            "spec": {
                                "template": {
                                    "spec": {
                                        "containers": [
                                            {
                                                "name": "aws-eks-nodeagent",
                                                "args": ["--enable-network-policy=true"],
                                            }
                                        ]
                                    }
                                }
                            },
                        },
                    ]
                }
            if resource == "storageclasses":
                return {
                    "items": [
                        {
                            "metadata": {"name": p["storage_class"]},
                            "provisioner": "ebs.csi.aws.com",
                            "parameters": {"encrypted": "true"},
                            "volumeBindingMode": "WaitForFirstConsumer",
                            "reclaimPolicy": "Retain",
                        }
                    ]
                }
            if resource == "crds":
                return {
                    "items": [
                        {
                            "metadata": {"name": name},
                            "spec": {"versions": [{"name": version, "served": True}]},
                        }
                        for name, version in CRDS.values()
                    ]
                }
            if resource == "deployments":
                return {
                    "items": [
                        {
                            "metadata": {
                                "name": name,
                                "namespace": "existing-controllers",
                                "labels": {key: value},
                            },
                            "status": {"availableReplicas": 2},
                            "spec": {
                                "template": {
                                    "spec": {
                                        "containers": [
                                            {
                                                "image": "altinity/clickhouse-operator:0.27.3"
                                                if name == "altinity-clickhouse-operator"
                                                else name + ":1"
                                            }
                                        ]
                                    }
                                }
                            },
                        }
                        for name, (key, value) in LABELS.items()
                    ]
                }
            if resource == "apiservices":
                return {
                    "items": [
                        {
                            "metadata": {"name": "v1beta1.external.metrics.k8s.io"},
                            "status": {"conditions": [{"type": "Available", "status": "True"}]},
                        },
                        {
                            "metadata": {"name": "v1beta1.metrics.k8s.io"},
                            "status": {
                                "conditions": [
                                    {
                                        "type": "Available",
                                        "status": "True" if self.metrics_available else "False",
                                    }
                                ]
                            },
                        },
                    ]
                }
            if resource == "secret":
                self_assert = args[-1]
                assert (
                    "go-template=" in self_assert
                    and ".data" in self_assert
                    and "$v}" not in self_assert
                )
                return {
                    "keys": {
                        k: True
                        for k in [
                            "GF_DATABASE_HOST",
                            "GF_DATABASE_PASSWORD",
                            "password",
                            "admin-user",
                            "admin-password",
                        ]
                    }
                }
        raise AssertionError(f"Unexpected command: {args}")


class Installation(unittest.TestCase):
    def test_stale_overlays_cannot_change_region_or_controller_ownership(self):
        p = configuration()
        manifest, controllers = values(p, DIGEST), operator_values(p)
        validate_generated_inputs(p, manifest, controllers)
        for mutate in (
            lambda m, c: m.update(region="us-west-2"),
            lambda m, c: c["components"].update({"cert-manager": True}),
            lambda m, c: m.update(createStorageClass=True),
        ):
            candidate, operators = copy.deepcopy(manifest), copy.deepcopy(controllers)
            mutate(candidate, operators)
            with self.assertRaisesRegex(ValueError, "do not match"):
                validate_generated_inputs(p, candidate, operators)

    def test_secret_values_and_unknown_options_rejected_in_profile(self):
        for change in (
            {"password": "must-not-persist"},
            {"operators": {"typo": False}},
            {"secret_source": "unknown"},
        ):
            with self.assertRaises(ValueError):
                validate_platform({**configuration(), **change})

    def test_disabling_owned_controller_is_not_an_ownership_transfer(self):
        p = configuration()
        inventory = Inventory(p)

        def reader(args):
            if args[:2] == ["helm", "list"]:
                return [{"name": "operators", "namespace": "observability-system"}]
            if args[:3] == ["helm", "get", "values"]:
                return {"components": {**p["operators"], "cert-manager": True}}
            return inventory(args)

        with self.assertRaisesRegex(ValueError, "cannot transfer ownership"):
            preflight(p, reader)

    @patch("scripts.seed_secrets.command")
    @patch("scripts.seed_secrets.subprocess.run")
    def test_existing_secrets_are_never_seeded_or_rotated(self, run, command):
        with patch(
            "sys.argv",
            ["seed-secrets", "--terraform-output", str(ROOT / "examples/existing-platform.yaml")],
        ):
            seed_secrets()
        run.assert_not_called()
        command.assert_not_called()

    def test_software_only_preflight_is_read_only(self):
        p = configuration()
        inventory = Inventory(p)
        preflight(p, inventory)
        self.assertIn("existing-controllers", p["operator_namespaces"])
        for command in inventory.calls:
            self.assertFalse(
                {
                    "apply",
                    "patch",
                    "create",
                    "delete",
                    "upgrade",
                    "install",
                    "update-kubeconfig",
                    "get-secret-value",
                }
                & set(command)
            )

    def test_support_preflight_requires_owner_opt_in_and_secret_keys_only(self):
        for missing in ["namespace", "canary", "receiver", None]:
            with self.subTest(missing=missing):
                p = configuration()
                p["tuning"] = {
                    "support": {"canary": {"enabled": True, "secretName": "custom-canary"}},
                    "notifications": {
                        "enabled": True,
                        "manageTenantConfig": True,
                        "owners": {"payments": "payments-url"},
                    },
                }
                inventory, calls = Inventory(p), []

                def reader(args, calls=calls, missing=missing, inventory=inventory):
                    calls.append(args)
                    if "get" in args:
                        resource = args[args.index("get") + 1]
                        if resource == "namespace":
                            return {
                                "metadata": {
                                    "labels": {}
                                    if missing == "namespace"
                                    else {"observability/collect": "true"}
                                }
                            }
                        if resource == "secret" and "custom-canary" in args:
                            return {"keys": {} if missing == "canary" else {"password": True}}
                        if resource == "secret" and "alert-receivers" in args:
                            return {
                                "keys": {"default-url": True}
                                if missing == "receiver"
                                else {"default-url": True, "payments-url": True}
                            }
                    return inventory(args)

                if missing:
                    with self.assertRaises(ValueError):
                        preflight(p, reader)
                else:
                    preflight(p, reader)
                for command in calls:
                    self.assertFalse(
                        {"apply", "patch", "create", "get-secret-value"} & set(command)
                    )
                    if "secret" in command:
                        self.assertIn('printf "%q: true," $k', command[-1])

    def test_wrong_cluster_cross_region_bucket_and_role_fail_closed(self):
        for attr, value, expected in [
            ("endpoint", "https://wrong.eks.amazonaws.com", "different API"),
            ("bucket_region", "us-west-2", "outside"),
            ("role_mismatch", True, "role differs"),
            ("metrics_available", False, "metrics-server"),
        ]:
            with self.subTest(attr=attr):
                p = configuration()
                inventory = Inventory(p)
                setattr(inventory, attr, value)
                with self.assertRaisesRegex(ValueError, expected):
                    preflight(p, inventory)

    def test_existing_controller_and_storage_ownership_not_adopted(self):
        for component in ("cert-manager", "argo-rollouts", "strimzi-kafka-operator"):
            p = configuration()
            p["operators"][component] = True
            with self.assertRaisesRegex(ValueError, "already exist"):
                preflight(p, Inventory(p))
        p = configuration()
        p["create_storage_class"] = True
        with self.assertRaisesRegex(ValueError, "outside this release"):
            preflight(p, Inventory(p))

    def test_zone_and_storage_safety(self):
        p = configuration()
        with self.assertRaises(ValueError):
            ready_nodes(nodes(p)[:-1], p["availability_zones"])
        with self.assertRaises(ValueError):
            validate_storage_class({"provisioner": "ebs.csi.aws.com", "reclaimPolicy": "Delete"})
        for zones in ([], ["us-east-1a"] * 3, ["us-east-1a", "us-east-1b", "us-west-2c"]):
            with self.assertRaises(ValueError):
                validate_platform({**p, "availability_zones": zones})

    def test_reused_controllers_emit_no_resources_or_crd_writes(self):
        p = configuration()
        self.assertEqual(packaged_crds(p["operators"]), [])
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "operators.yaml"
            config.write_text(yaml.safe_dump(operator_values(p)))
            output = subprocess.run(
                [
                    "helm",
                    "template",
                    "operators",
                    str(ROOT / "charts/operators"),
                    "--include-crds",
                    "-f",
                    str(config),
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertEqual([d for d in yaml.safe_load_all(output) if d], [])

    def test_actual_azs_and_storage_class_reach_every_pvc(self):
        p = configuration()
        generated = values(p, DIGEST)
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "platform.yaml"
            config.write_text(yaml.safe_dump(generated))
            output = subprocess.run(
                [
                    "helm",
                    "template",
                    "olly",
                    str(ROOT / "charts/platform"),
                    "-n",
                    "observability",
                    "-f",
                    str(config),
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            resources = [d for d in yaml.safe_load_all(output) if d]
            self.assertNotIn("us-east-1a", output)
            self.assertFalse(any(d["kind"] == "StorageClass" for d in resources))
            quota = next(
                d
                for d in resources
                if d["kind"] == "Rollout" and d["metadata"]["name"] == "log-quota"
            )
            self.assertEqual(
                quota["spec"]["template"]["spec"]["nodeSelector"]["kubernetes.io/arch"], "amd64"
            )
            for d in resources:
                if d["kind"] == "StatefulSet":
                    for claim in d["spec"].get("volumeClaimTemplates", []):
                        self.assertEqual(claim["spec"].get("storageClassName"), p["storage_class"])

    @patch("scripts.install.subprocess.run")
    @patch("scripts.install.preflight", side_effect=ValueError("wrong cluster"))
    def test_failed_preflight_never_invokes_deployment(self, check, run):
        with patch(
            "sys.argv",
            [
                "install",
                "--config",
                str(ROOT / "examples/existing-platform.yaml"),
                "--quota-image",
                DIGEST,
            ],
        ):
            with self.assertRaisesRegex(ValueError, "wrong cluster"):
                install()
        run.assert_not_called()

    @patch("scripts.install.subprocess.run")
    @patch("scripts.install.preflight")
    def test_check_only_renders_without_deployment(self, check, run):
        with tempfile.TemporaryDirectory() as temp:
            with patch(
                "sys.argv",
                [
                    "install",
                    "--config",
                    str(ROOT / "examples/existing-platform.yaml"),
                    "--quota-image",
                    DIGEST,
                    "--check-only",
                    "--output-dir",
                    temp,
                ],
            ):
                install()
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][:2], ["helm", "template"])
            self.assertTrue((Path(temp) / "platform.operators.yaml").exists())

    def test_terraform_wrapper_and_raw_profile_are_equivalent(self):
        p = configuration()
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "terraform.json"
            config.write_text(json.dumps({"platform": {"value": p}}))
            self.assertEqual(load_platform(config), validate_platform(copy.deepcopy(p)))

    @patch("scripts.install.subprocess.run")
    @patch("scripts.install.preflight")
    def test_offline_render_does_not_contact_cluster_or_aws(self, check, run):
        with tempfile.TemporaryDirectory() as temp:
            with patch(
                "sys.argv",
                [
                    "install",
                    "--config",
                    str(ROOT / "examples/existing-platform.yaml"),
                    "--quota-image",
                    DIGEST,
                    "--render-only",
                    "--output-dir",
                    temp,
                ],
            ):
                install()
            check.assert_not_called()
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][:2], ["helm", "template"])
            self.assertTrue((Path(temp) / "platform.json").exists())


if __name__ == "__main__":
    unittest.main()
