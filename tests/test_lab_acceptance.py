import argparse
import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import lab_acceptance as lab


class LabAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.args = argparse.Namespace(
            context="olly-lab-test",
            cluster="olly-lab-test",
            account="123456789012",
            region="us-east-2",
            confirm_cluster_arn="arn:aws:eks:us-east-2:123456789012:cluster/olly-lab-test",
            output=str(Path(self.temp.name) / "evidence.json"),
            timeout=900,
            noisy_neighbor=False,
        )
        self.uid = "12345678-1234-1234-1234-123456789012"

    def sample(self):
        return {
            "health": 200,
            "completed": 200,
            "interval": 120,
            **{signal + "Success": 1 for signal in lab.SIGNALS},
            **{signal + "At": 199 for signal in lab.SIGNALS},
        }

    def test_explicit_lab_identity_and_new_evidence_file_required(self):
        lab.validate_options(self.args)
        for key, value in [
            ("account", "not-an-account"),
            ("cluster", "production"),
            ("context", "test\n--all-namespaces"),
            ("region", "us-gov-west-1"),
            ("timeout", 0),
            ("timeout", 1201),
            ("confirm_cluster_arn", "arn:aws:eks:us-west-2:123456789012:cluster/olly-lab-test"),
        ]:
            args = copy.copy(self.args)
            setattr(args, key, value)
            with self.subTest(key=key), self.assertRaises(lab.Rejected):
                lab.validate_options(args)
        Path(self.args.output).write_text("preserve")
        with self.assertRaises(lab.Rejected):
            lab.validate_options(self.args)
        self.assertEqual(Path(self.args.output).read_text(), "preserve")

    def test_canary_requires_every_fresh_signal_and_a_completed_cycle(self):
        good = self.sample()
        self.assertTrue(lab.fresh_canary(good, after=150, now=201))
        for change in [
            {"health": 503},
            {"logsSuccess": 0},
            {"tracesAt": 150},
            {"metricsAt": 0},
            {"completed": 150},
            {"completed": 250},
            {"logsAt": 201},
        ]:
            with self.subTest(change=change):
                self.assertFalse(lab.fresh_canary({**good, **change}, 150, 201))
        self.assertFalse(lab.fresh_canary(good, 150, 450))
        for change in [{"interval": float("nan")}, {"secret": "SYNTHETIC"}]:
            with self.assertRaises(lab.Rejected):
                lab.fresh_canary({**good, **change}, 150, 201)

    def test_fairness_counts_and_counter_scope(self):
        before = {"quota-a": {"accepted": 10, "dropped": 20, "unavailable": 0}}
        after = {"quota-a": {"accepted": 640, "dropped": 2350, "unavailable": 0}}
        counts = {"quiet": 30, "flood": 600}
        report = lab.fairness_evidence(counts, before, after)
        self.assertEqual(report["offered"], {"quiet": 30, "flood": 3000})
        self.assertEqual(report["quotaCounterDelta"]["dropped"], 2330)
        self.assertIn("aggregate all services", report["scope"])
        for bad in [
            {"quiet": 29, "flood": 600},
            {"quiet": 30, "flood": 0},
            {"quiet": 30, "flood": 3000},
        ]:
            with self.assertRaises(lab.Rejected):
                lab.fairness_evidence(bad, before, after)
        for change in [
            {"accepted": 0, "dropped": 2350, "unavailable": 0},
            {"accepted": 640, "dropped": 2350, "unavailable": 1},
            {"accepted": 640, "dropped": 20, "unavailable": 0},
            {"accepted": float("nan"), "dropped": 2350, "unavailable": 0},
        ]:
            with self.assertRaises(lab.Rejected):
                lab.fairness_evidence(counts, before, {"quota-a": change})
        with self.assertRaises(lab.Rejected):
            lab.fairness_evidence(counts, before, {"new-pod": after["quota-a"]})

    def test_bounded_jobs_use_existing_pinned_image_without_credentials(self):
        image = "python:3.14.7-slim@sha256:" + "a" * 64
        job = lab.producer_job("lab-acceptance-flood-test", "abcd12345678", "flood", image, 1000)
        spec = job["spec"]
        self.assertEqual(spec["parallelism"], 2)
        self.assertEqual(spec["backoffLimit"], 0)
        self.assertEqual(spec["activeDeadlineSeconds"], 120)
        pod = spec["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(pod["restartPolicy"], "Never")
        self.assertNotIn("volumes", pod)
        container = pod["containers"][0]
        self.assertEqual(container["image"], image)
        self.assertIn("1500", container["command"])
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertEqual(len(container["env"]), 1)
        compile(lab.PRODUCER, "producer", "exec")
        compile(lab.POD_PROBE, "in_pod_probe", "exec")

    def test_command_preserves_empty_status_fields_and_redacts_errors(self):
        output = self.uid + "\t2\t\n"
        with patch.object(
            lab.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, output, "")
        ):
            received = lab.execute(["kubectl", "get", "job"])
        self.assertEqual(received.split("\t"), [self.uid, "2", ""])
        with patch.object(
            lab.subprocess,
            "run",
            side_effect=subprocess.CalledProcessError(1, ["kubectl"], stderr="SYNTHETIC-PASSWORD"),
        ):
            with self.assertRaises(lab.Rejected) as caught:
                lab.execute(["kubectl"])
        self.assertNotIn("SYNTHETIC-PASSWORD", str(caught.exception))

    def test_projected_pod_fields_use_real_jsonpath_delimiters(self):
        def command(args, data=None):
            self.assertIn("--context", args)
            projection = args[-1]
            self.assertIn('{"\\t"}', projection)
            self.assertNotIn('{"\\\\t"}', projection)
            self.assertNotIn("secret", projection)
            return f"support-a\t{self.uid}\t10.72.16.3\tRunning\ttrue\t0\n"

        task = lab.Acceptance(self.args, command)
        pods = task.pods("olly-support")
        self.assertEqual(pods["support-a"], {"uid": self.uid, "ip": "10.72.16.3", "restarts": 0})

    def test_wrong_account_aborts_before_any_kubernetes_command(self):
        command = Mock(return_value=json.dumps({"Account": "999999999999"}))
        task = lab.Acceptance(self.args, command)
        with self.assertRaises(lab.Rejected):
            task.identity()
        self.assertEqual(command.call_count, 1)
        self.assertEqual(command.call_args.args[0][:3], ["aws", "sts", "get-caller-identity"])

    def test_context_endpoint_and_explicit_namespace_optin_are_verified(self):
        responses = iter(
            [
                json.dumps({"Account": self.args.account}),
                json.dumps(
                    {
                        "cluster": {
                            "arn": self.args.confirm_cluster_arn,
                            "status": "ACTIVE",
                            "endpoint": "https://lab.eks.amazonaws.com",
                            "resourcesVpcConfig": {"endpointPublicAccess": False},
                        }
                    }
                ),
                "https://foreign.eks.amazonaws.com",
            ]
        )
        task = lab.Acceptance(self.args, lambda *_: next(responses))
        with self.assertRaisesRegex(lab.Rejected, "different API"):
            task.identity()

    def test_cleanup_uses_uid_preconditions_and_leaves_foreign_jobs_untouched(self):
        calls = []
        responses = iter([self.uid, "{}", ""])

        def command(args, data=None):
            calls.append((args, data))
            return next(responses)

        task = lab.Acceptance(self.args, command)
        task.created = [("lab-acceptance-flood-test", self.uid)]
        self.assertTrue(task.cleanup())
        deletion = calls[1]
        self.assertIn("--raw", deletion[0])
        self.assertEqual(json.loads(deletion[1])["preconditions"], {"uid": self.uid})
        self.assertEqual(json.loads(deletion[1])["propagationPolicy"], "Foreground")
        command = Mock(return_value="87654321-1234-1234-1234-123456789012")
        task.command = command
        self.assertFalse(task.cleanup())
        self.assertEqual(command.call_count, 1)
        self.assertNotIn("delete", command.call_args.args[0])

    def test_noisy_setup_failure_still_cleans_up_created_jobs(self):
        task = lab.Acceptance(self.args)
        task.pods = Mock(
            return_value={"pod-a": {"uid": self.uid, "ip": "10.72.16.3", "restarts": 0}}
        )
        task.probe = Mock(return_value={"pod-a": {"accepted": 1, "dropped": 0, "unavailable": 0}})
        task.cleanup = Mock(return_value=True)
        created = 0

        def kube(*args, data=None):
            nonlocal created
            if args[0] == "get" and args[1] == "pod":
                return "20" if "EVENTS_PER_SECOND" in args[-1] else "40"
            if args[0] == "get" and args[1] == "deployment":
                return "python:3.14.7-slim@sha256:" + "a" * 64
            if args[0] == "create":
                created += 1
                if created == 1:
                    return self.uid
                raise lab.Rejected("Synthetic create failed")
            raise AssertionError("Unexpected command")

        task.kube = kube
        with self.assertRaisesRegex(lab.Rejected, "Synthetic create"):
            task.noisy()
        self.assertEqual(len(task.created), 1)
        task.cleanup.assert_called_once()

    def test_successful_noisy_run_handles_empty_job_failure_fields(self):
        task = lab.Acceptance(self.args)
        support = {
            "support-a": {"uid": self.uid, "ip": "10.72.16.3", "restarts": 0},
            "support-b": {"uid": self.uid, "ip": "10.72.16.4", "restarts": 0},
        }
        quota = {"quota-a": {"uid": self.uid, "ip": "10.72.16.5", "restarts": 0}}
        task.pods = Mock(side_effect=lambda app: support if app == "olly-support" else quota)
        task.canaries = Mock(return_value={"replicas": 2, "freshCycles": 1})
        task.cleanup = Mock(return_value=True)
        task.pause = Mock()
        created = {}
        reads = 0

        def probe(pod, mode, argument=None):
            nonlocal reads
            if mode == "quota":
                reads += 1
                return {
                    "quota-a": {"accepted": 100 * reads, "dropped": 100 * reads, "unavailable": 0}
                }
            return {"quiet": 30, "flood": 600}

        task.probe = probe

        def kube(*args, data=None):
            if args[:2] == ("get", "pod"):
                return "20" if "EVENTS_PER_SECOND" in args[-1] else "40"
            if args[:2] == ("get", "deployment"):
                return "python:3.14.7-slim@sha256:" + "a" * 64
            if args[0] == "create":
                obj = json.loads(data)
                created[obj["metadata"]["name"]] = obj["spec"]["completions"]
                return self.uid
            if args[:2] == ("get", "job"):
                return self.uid + "\t" + str(created[args[2]]) + "\t"
            raise AssertionError("Unexpected command")

        task.kube = kube
        with patch.object(lab.time, "monotonic", side_effect=range(0, 1000, 10)):
            result = task.noisy()
        self.assertTrue(result["jobsCleanedUp"])
        self.assertEqual(result["storedDistinct"], {"quiet": 30, "flood": 600})
        self.assertEqual(result["freshCanaryAfterStart"]["replicas"], 2)
        task.cleanup.assert_called_once()

    def test_embedded_probe_keeps_credential_and_error_text_in_pod(self):
        directory = Path(self.temp.name)
        helper = directory / "agent.py"
        helper.write_text(
            "def http(url, payload=None, headers=None):\n"
            "    assert headers['Authorization'].startswith('Basic ')\n"
            "    if 'fail' in url: raise ValueError('SYNTHETIC-CREDENTIAL')\n"
            '    return 200, b\'{"quiet":"30","flood":"600"}\'\n'
        )
        password = directory / "password"
        password.write_text("SYNTHETIC-CREDENTIAL")
        settings = directory / "settings.json"
        config = {
            "cluster": "olly-lab-test",
            "clickhousePasswordFile": str(password),
            "clickhouse": "http://fixture",
        }
        settings.write_text(json.dumps(config))
        code = lab.POD_PROBE.replace("/config/agent.py", str(helper)).replace(
            "/config/settings.json", str(settings)
        )
        args = [
            ".venv/bin/python",
            "-B",
            "-c",
            code,
            "counts",
            json.dumps({"quiet": "quiet", "flood": "flood", "marker": "fixture:"}),
        ]
        result = subprocess.run(args, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), {"quiet": 30, "flood": 600})
        self.assertNotIn("SYNTHETIC-CREDENTIAL", result.stdout + result.stderr)
        config["clickhouse"] = "http://fail"
        settings.write_text(json.dumps(config))
        result = subprocess.run(args, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr.strip(), "Support probe unavailable")


if __name__ == "__main__":
    unittest.main()
