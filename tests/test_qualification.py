import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import qualification as q

ROOT = Path(__file__).resolve().parents[1]
BASE = "2000-01-01T00:00:00Z"


class QualificationTests(unittest.TestCase):
    def setUp(self):
        self.config = q.configuration(ROOT / "operations/qualification.example.yaml")
        self.config["sourceRevision"] = "a" * 40
        self.config["deploymentArtifacts"] = {
            "platformContractSHA256": "b" * 64,
            "renderedValuesSHA256": "c" * 64,
            "terraformPlanSHA256": "d" * 64,
        }
        self.config["recovery"].update(rpoMinutes=15, rtoMinutes=60)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / "run"
        with patch.object(q, "now_string", return_value=BASE):
            self.planned = q.initialize(self.config, self.directory)
        self.counter = 0

    def receipt(self, gate, status="passed"):
        self.counter += 1
        stamp = f"2000-01-01T00:{self.counter // 60:02}:{self.counter % 60:02}Z"
        spec = next(g for g in self.planned["gates"] if g["id"] == gate)
        return {
            "version": 1,
            "runId": self.config["runId"],
            "configDigest": q.digest(self.config),
            "gate": gate,
            "status": status,
            "startedAt": stamp,
            "completedAt": stamp,
            "operator": "test-operator",
            "reason": "Synthetic unit fixture; no deployment",
            "checks": dict.fromkeys(spec["checks"], status == "passed"),
            "measurements": dict.fromkeys(spec.get("measurements", []), 1),
            "evidenceRefs": ["SYNTHETIC-UNIT-FIXTURE"],
        }

    def complete_through(self, gate):
        for item in self.planned["gates"]:
            if item["enabled"]:
                q.record(self.directory, self.receipt(item["id"]))
            if item["id"] == gate:
                return

    def test_examples_and_no_cloud_or_command_execution(self):
        with (
            patch("subprocess.run", side_effect=AssertionError("No commands")),
            patch("subprocess.Popen", side_effect=AssertionError("No commands")),
            patch("os.system", side_effect=AssertionError("No commands")),
            patch("socket.socket", side_effect=AssertionError("No network")),
        ):
            for example in [
                ROOT / "operations/qualification.example.yaml",
                ROOT / "examples/recovery-existing-external.yaml",
                ROOT / "examples/recovery-cross-region.yaml",
            ]:
                for action in ["plan", "init", "receipt-template", "record", "check"]:
                    output = Path(self.tmp.name) / "never-created"
                    with redirect_stdout(io.StringIO()):
                        code = q.main(
                            [
                                action,
                                "--config",
                                str(example),
                                "--run-dir",
                                str(output),
                                "--check-only",
                            ]
                        )
                    self.assertEqual(code, 0)
                    self.assertFalse(output.exists())
            self.assertFalse(q.report(self.directory)["qualified"])
            q.record(self.directory, self.receipt("environment_identity"))

    def test_deployment_hashes_required_and_software_only_may_omit_terraform(self):
        for key in self.config["deploymentArtifacts"]:
            altered = copy.deepcopy(self.config)
            altered["deploymentArtifacts"][key] = "0" * 64
            planned = q.plan(altered)
            manifest = {"createdAt": BASE}
            receipt = self.receipt("environment_identity")
            receipt["configDigest"] = q.digest(altered)
            with self.subTest(key=key), self.assertRaises(ValueError):
                q.validate_receipt(receipt, altered, planned, manifest, {})
        altered = copy.deepcopy(self.config)
        altered["deploymentArtifacts"]["terraformPlanSHA256"] = None
        receipt = self.receipt("environment_identity")
        receipt["configDigest"] = q.digest(altered)
        with self.assertRaises(ValueError):
            q.validate_receipt(receipt, altered, q.plan(altered), {"createdAt": BASE}, {})
        altered["environment"]["mode"] = "software-only"
        receipt["configDigest"] = q.digest(altered)
        q.validate_receipt(receipt, altered, q.plan(altered), {"createdAt": BASE}, {})

    def test_receipt_template_cannot_be_mistaken_for_execution(self):
        template = q.receipt_template(self.directory, "environment_identity")
        self.assertEqual(template["status"], "pending")
        self.assertFalse(any(template["checks"].values()))
        self.assertIsNone(template["startedAt"])
        with self.assertRaises(ValueError):
            q.record(self.directory, template)
        with self.assertRaises(ValueError):
            q.receipt_template(self.directory, "profiling")

    def test_pending_and_required_skips_never_pass(self):
        initial = q.report(self.directory)
        self.assertFalse(initial["qualified"])
        statuses = {g["id"]: g["status"] for g in initial["gates"]}
        self.assertEqual(statuses["environment_identity"], "pending")
        self.assertEqual(statuses["profiling"], "skipped")
        q.record(self.directory, self.receipt("environment_identity", "skipped"))
        self.assertFalse(q.report(self.directory)["qualified"])
        with self.assertRaises(ValueError):
            q.record(self.directory, self.receipt("ownership"))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(q.main(["check", "--run-dir", str(self.directory)]), 2)

    def test_requires_matching_run_config_assertions_and_past_timestamps(self):
        original = self.receipt("environment_identity")
        for key, value in [
            ("runId", "different-run"),
            ("configDigest", "b" * 64),
            ("completedAt", "9999-01-01T00:00:00Z"),
            ("startedAt", "2000-01-01T00:00:00+00:00"),
            ("startedAt", "1999-12-31T23:59:59Z"),
            ("completedAt", "2000-02-31T00:00:00Z"),
            ("checks", {}),
            ("evidenceRefs", []),
            ("gate", "unknown"),
        ]:
            bad = copy.deepcopy(original)
            bad[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                q.record(self.directory, bad)
        bad = copy.deepcopy(original)
        bad["checks"]["unexpected_failure"] = False
        with self.assertRaises(ValueError):
            q.record(self.directory, bad)
        bad = copy.deepcopy(original)
        bad["rawSecrets"] = "never-accepted"
        with self.assertRaises(ValueError):
            q.record(self.directory, bad)
        self.assertEqual(list((self.directory / "receipts").iterdir()), [])

    def test_dependency_retest_invalidates_dependent_success(self):
        self.complete_through("ownership")
        q.record(self.directory, self.receipt("environment_identity", "failed"))
        statuses = {g["id"]: g["status"] for g in q.report(self.directory)["gates"]}
        self.assertEqual(statuses["environment_identity"], "failed")
        self.assertEqual(statuses["ownership"], "pending")
        q.record(self.directory, self.receipt("environment_identity"))
        statuses = {g["id"]: g["status"] for g in q.report(self.directory)["gates"]}
        self.assertEqual(statuses["ownership"], "pending")
        with self.assertRaises(ValueError):
            q.record(self.directory, self.receipt("capacity_cost"))
        q.record(self.directory, self.receipt("ownership"))
        statuses = {g["id"]: g["status"] for g in q.report(self.directory)["gates"]}
        self.assertEqual(statuses["ownership"], "passed")

    def test_complete_operator_attestation_is_explicitly_not_live_proof(self):
        self.complete_through("handover")
        result = q.report(self.directory)
        self.assertTrue(result["qualified"])
        self.assertIn("no independent cloud verification", result["evidenceType"])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(q.main(["check", "--run-dir", str(self.directory)]), 0)
        self.assertEqual((self.directory.stat().st_mode & 0o777), 0o700)
        for filename in (self.directory / "receipts").iterdir():
            self.assertEqual((filename.stat().st_mode & 0o777), 0o600)
        with self.assertRaises(FileExistsError):
            q.initialize(self.config, self.directory)

    def test_recovery_requires_reviewed_targets_provider_and_bounded_measurements(self):
        for key in ["rpoMinutes", "rtoMinutes"]:
            altered = copy.deepcopy(self.config)
            altered["recovery"][key] = None
            with self.assertRaises(ValueError):
                q.prerequisites(altered, "backup_restore")
        altered = copy.deepcopy(self.config)
        altered["recovery"].update(backupMode="external", providerRef=None)
        with self.assertRaises(ValueError):
            q.prerequisites(altered, "backup_restore")
        altered["recovery"]["providerRef"] = "PROVIDER-RUNBOOK"
        q.prerequisites(altered, "backup_restore")
        self.complete_through("sampler_drain")
        original = self.receipt("backup_restore")
        for measurement in [
            {},
            {"data_gap_minutes": -1, "recovery_minutes": 1},
            {"data_gap_minutes": 16, "recovery_minutes": 1},
            {"data_gap_minutes": 1, "recovery_minutes": 61},
            {"data_gap_minutes": True, "recovery_minutes": 1},
            {"data_gap_minutes": float("nan"), "recovery_minutes": 1},
        ]:
            bad = copy.deepcopy(original)
            bad["measurements"] = measurement
            with self.subTest(measurement=measurement), self.assertRaises(ValueError):
                q.record(self.directory, bad)
        q.record(self.directory, original)

    def test_environment_cross_region_and_config_changes_fail_closed(self):
        for change in [
            lambda c: c["recovery"].update(
                destination={k: c["environment"][k] for k in ["accountId", "region", "cluster"]}
            ),
            lambda c: c["recovery"]["destination"].update(region="us-west-2"),
            lambda c: c["recovery"]["crossRegion"].update(enabled=True),
            lambda c: c["features"].update(unknown=True),
        ]:
            bad = copy.deepcopy(self.config)
            change(bad)
            path = Path(self.tmp.name) / "bad.json"
            path.write_text(json.dumps(bad))
            with self.assertRaises(ValueError):
                q.configuration(path)
        cross = q.configuration(ROOT / "examples/recovery-cross-region.yaml")
        cross["recovery"].update(rpoMinutes=15, rtoMinutes=60)
        with self.assertRaises(ValueError):
            q.prerequisites(cross, "cross_region")
        cross["recovery"]["crossRegion"].update(
            rpoMinutes=30, rtoMinutes=120, costAssessmentRef="COST-REVIEW"
        )
        q.prerequisites(cross, "cross_region")
        changed = copy.deepcopy(self.config)
        changed["environment"]["cluster"] = "different"
        (self.directory / "config.json").write_text(json.dumps(changed))
        with self.assertRaises(ValueError):
            q.report(self.directory)

    def test_receipts_cannot_be_overwritten_tampered_or_loaded_through_symlink(self):
        receipt = self.receipt("environment_identity")
        q.record(self.directory, receipt)
        path = self.directory / "receipts/000001.json"
        with self.assertRaises(FileExistsError):
            q.write_new(path, {})
        data = json.loads(path.read_text())
        data["receipt"]["operator"] = "changed"
        path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            q.report(self.directory)
        path.unlink()
        secret = Path(self.tmp.name) / "private.json"
        secret.write_text("{}")
        path.symlink_to(secret)
        with self.assertRaises(ValueError):
            q.report(self.directory)


if __name__ == "__main__":
    unittest.main()
