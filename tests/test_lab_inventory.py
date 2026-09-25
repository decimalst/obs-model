import copy
import io
import json
import os
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import lab_inventory as lab

ACCOUNT = "123456789012"
REGION = "us-east-2"
NAME = "olly-lab-test"
RUN = "lab-test-001"
EXPIRY = "2026-09-26T12:00:00Z"
ARGS = {"account": ACCOUNT, "region": REGION, "name": NAME, "run_id": RUN}
SECRET = "SYNTHETIC-SECRET-NEVER-PROJECT"


def resource(kind, name, values):
    return {
        "address": f"module.lab.{kind}.{name}",
        "mode": "managed",
        "type": kind,
        "values": {"tags_all": {"LabRun": RUN, "ExpiresAt": EXPIRY, "Extra": SECRET}, **values},
    }


def fixture():
    bucket = f"{NAME}-{ACCOUNT}-{REGION}-tempo"
    resources = [
        resource("aws_vpc", "lab", {"id": "vpc-12345678"}),
        resource(
            "aws_subnet",
            "lab",
            {"id": "subnet-12345678", "vpc_id": "vpc-12345678", "availability_zone": REGION + "a"},
        ),
        resource(
            "aws_eks_cluster",
            "lab",
            {"id": NAME, "name": NAME, "arn": f"arn:aws:eks:{REGION}:{ACCOUNT}:cluster/{NAME}"},
        ),
        resource(
            "aws_s3_bucket",
            "lab",
            {"id": bucket, "bucket": bucket, "arn": "arn:aws:s3:::" + bucket},
        ),
        resource(
            "aws_db_instance",
            "lab",
            {
                "id": "db-fixture",
                "identifier": NAME + "-grafana",
                "arn": f"arn:aws:rds:{REGION}:{ACCOUNT}:db:{NAME}-grafana",
                "final_snapshot_identifier": NAME + "-grafana-final",
                "password": SECRET,
            },
        ),
        resource(
            "aws_iam_role",
            "lab",
            {
                "id": NAME + "-tempo-123",
                "name": NAME + "-tempo-123",
                "arn": f"arn:aws:iam::{ACCOUNT}:role/{NAME}-tempo-123",
                "assume_role_policy": SECRET,
            },
        ),
        resource(
            "aws_secretsmanager_secret_version", "lab", {"id": SECRET, "secret_string": SECRET}
        ),
    ]
    state = {
        "values": {
            "outputs": {"password": SECRET},
            "root_module": {"child_modules": [{"resources": resources}]},
        }
    }
    pvs = {
        "kind": "List",
        "items": [
            {
                "kind": "PersistentVolume",
                "metadata": {
                    "name": "pvc-lab-data",
                    "uid": "11111111-1111-1111-1111-111111111111",
                    "annotations": {"private": SECRET},
                    "labels": {"topology.kubernetes.io/region": REGION},
                },
                "spec": {
                    "storageClassName": "gp3-encrypted",
                    "claimRef": {
                        "namespace": "observability",
                        "name": "data-telemetry-quorum-0",
                        "uid": "22222222-2222-2222-2222-222222222222",
                    },
                    "csi": {
                        "driver": "ebs.csi.aws.com",
                        "volumeHandle": "vol-12345678",
                        "volumeAttributes": {"private": SECRET},
                    },
                    "nodeAffinity": {
                        "required": {
                            "nodeSelectorTerms": [
                                {
                                    "matchExpressions": [
                                        {
                                            "key": "topology.ebs.csi.aws.com/zone",
                                            "operator": "In",
                                            "values": [REGION + "a"],
                                        }
                                    ]
                                }
                            ]
                        }
                    },
                },
            }
        ],
    }
    return state, pvs, resources


class LabInventoryTests(unittest.TestCase):
    def test_current_lab_root_resource_types_have_explicit_projection_policy(self):
        root = Path(__file__).resolve().parents[1]
        directories = [
            root / "terraform/live/lab",
            root / "terraform/modules/region",
            root / "terraform/modules/platform-services",
        ]
        resource_types = {
            kind
            for directory in directories
            for source in directory.glob("*.tf")
            for kind in re.findall(r'^resource\s+"([a-z0-9_]+)"', source.read_text(), re.MULTILINE)
        }
        self.assertIn("aws_s3_bucket_ownership_controls", resource_types)
        self.assertTrue(resource_types)
        self.assertEqual(resource_types - set(lab.PRIMARY) - lab.RELATIONSHIPS, set())

    def test_projects_only_approved_identities_and_never_executes(self):
        state, pvs, _ = fixture()
        before = copy.deepcopy((state, pvs))
        with (
            patch("subprocess.run", side_effect=AssertionError("No execution")),
            patch("os.system", side_effect=AssertionError("No execution")),
            patch("socket.socket", side_effect=AssertionError("No network")),
        ):
            result = lab.inventory(state, pvs, **ARGS)
        self.assertEqual((state, pvs), before)
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertFalse(result["deletionAuthorized"])
        self.assertFalse(result["persistentVolumes"][0]["sourceClusterVerified"])
        self.assertEqual(result["persistentVolumes"][0]["volumeId"], "vol-12345678")
        self.assertEqual(len(result["lab"]["relationships"]), 1)
        state["values"]["root_module"]["child_modules"][0]["resources"].reverse()
        self.assertEqual(result, lab.inventory(state, pvs, **ARGS))

    def test_rejects_foreign_state_identity_tags_and_resources(self):
        for change in (
            lambda r: r[2]["values"].update(
                arn=f"arn:aws:eks:{REGION}:999999999999:cluster/{NAME}"
            ),
            lambda r: r[2]["values"].update(name="other-cluster"),
            lambda r: r[1]["values"].update(vpc_id="vpc-ffffffff"),
            lambda r: r[1]["values"].update(availability_zone="us-west-2a"),
            lambda r: r[0]["values"]["tags_all"].update(LabRun="another-run"),
            lambda r: r[0]["values"]["tags_all"].update(ExpiresAt="2026-09-27T12:00:00Z"),
            lambda r: r[0]["values"]["tags_all"].update(ExpiresAt="2026-02-31T12:00:00Z"),
            lambda r: r[0]["values"].pop("tags_all"),
            lambda r: r[5]["values"].update(name="unrelated-admin"),
            lambda r: r[3]["values"].update(bucket="unrelated-bucket"),
            lambda r: r[4]["values"].update(final_snapshot_identifier="existing-production-backup"),
            lambda r: r.append(
                resource("aws_unknown_future_resource", "unknown", {"id": "unknown"})
            ),
            lambda r: r.append(copy.deepcopy(r[0])),
            lambda r: r.append(
                {**copy.deepcopy(r[3]), "address": "module.lab.aws_s3_bucket.duplicate"}
            ),
        ):
            state, pvs, resources = fixture()
            change(resources)
            with self.subTest(change=change), self.assertRaises(ValueError):
                lab.inventory(state, pvs, **ARGS)
        state, pvs, _ = fixture()
        state["planned_values"] = {}
        with self.assertRaises(ValueError):
            lab.inventory(state, pvs, **ARGS)

    def test_rejects_unscoped_foreign_or_duplicate_volumes(self):
        for change in (
            lambda p: p["spec"]["claimRef"].update(namespace="production"),
            lambda p: p["spec"]["claimRef"].pop("uid"),
            lambda p: p["metadata"].pop("uid"),
            lambda p: p["metadata"]["labels"].update(
                {"topology.kubernetes.io/region": "us-west-2"}
            ),
            lambda p: p["spec"].update(storageClassName="production"),
            lambda p: p["spec"]["csi"].update(driver="another-csi"),
            lambda p: p["spec"]["csi"].update(volumeHandle="aws://us-west-2a/vol-12345678"),
            lambda p: p["spec"].pop("nodeAffinity"),
            lambda p: p["spec"]["nodeAffinity"]["required"]["nodeSelectorTerms"].append({}),
            lambda p: p["spec"]["nodeAffinity"]["required"]["nodeSelectorTerms"][0][
                "matchExpressions"
            ][0].update(values=["us-west-2a"]),
        ):
            state, pvs, _ = fixture()
            change(pvs["items"][0])
            with self.subTest(change=change), self.assertRaises(ValueError):
                lab.inventory(state, pvs, **ARGS)
        state, pvs, _ = fixture()
        pvs["items"].append(copy.deepcopy(pvs["items"][0]))
        with self.assertRaises(ValueError):
            lab.inventory(state, pvs, **ARGS)
        state, pvs, _ = fixture()
        pvs["clusterArn"] = "arn:aws:eks:us-east-2:999999999999:cluster/foreign"
        with self.assertRaises(ValueError):
            lab.inventory(state, pvs, **ARGS)

    def test_private_exclusive_output_and_secret_safe_errors(self):
        state, pvs, _ = fixture()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            state_file, pv_file, output = base / "state.json", base / "pvs.json", base / "review"
            state_file.write_text(json.dumps(state))
            pv_file.write_text(json.dumps(pvs))
            args = [
                "--state",
                str(state_file),
                "--pvs",
                str(pv_file),
                "--account",
                ACCOUNT,
                "--region",
                REGION,
                "--name",
                NAME,
                "--run-id",
                RUN,
                "--output",
                str(output),
            ]
            with redirect_stdout(io.StringIO()):
                self.assertEqual(lab.main(args), 0)
            self.assertEqual(os.stat(output).st_mode & 0o777, 0o700)
            self.assertEqual(os.stat(output / "inventory.json").st_mode & 0o777, 0o600)
            saved = (output / "inventory.json").read_bytes()
            with redirect_stderr(io.StringIO()) as error:
                self.assertEqual(lab.main(args), 1)
            self.assertEqual((output / "inventory.json").read_bytes(), saved)
            state_file.write_text("invalid " + SECRET)
            with redirect_stderr(error):
                self.assertEqual(lab.main(args), 1)
            self.assertNotIn(SECRET, error.getvalue())
            link = base / "link.json"
            link.symlink_to(pv_file)
            with self.assertRaises(ValueError):
                lab.read_json(link)
            with patch.object(lab, "MAX_BYTES", 1), self.assertRaises(ValueError):
                lab.read_json(pv_file)
            fifo = base / "not-regular"
            os.mkfifo(fifo)
            with self.assertRaises(ValueError):
                lab.read_json(fifo)

    def test_empty_pv_snapshot_is_supported_but_does_not_prove_cleanup(self):
        state, _, _ = fixture()
        result = lab.inventory(state, {"kind": "List", "items": []}, **ARGS)
        self.assertEqual(result["persistentVolumes"], [])
        self.assertFalse(result["deletionAuthorized"])
        self.assertIn("cannot prove cluster provenance", " ".join(result["cleanupReview"]))
