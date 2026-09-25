import argparse
import contextlib
import copy
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, mock_open, patch

from scripts import lab_pod_identity as lab


class LabPodIdentity(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.args = argparse.Namespace(
            context="olly-lab-test",
            cluster="olly-lab-test",
            region="us-east-2",
            account="123456789012",
            lab_run="test-run",
            run_probe=True,
            timeout=180,
            confirm_cluster_arn="arn:aws:eks:us-east-2:123456789012:cluster/olly-lab-test",
            output=str(Path(self.temp.name) / "result.json"),
            allowed_bucket="olly-lab-test-123456789012-us-east-2-mimir-blocks",
            denied_bucket="olly-lab-test-123456789012-us-east-2-artifacts",
        )
        self.env = {
            "AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://169.254.170.23/v1/credentials",
            "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE": "/var/run/secrets/pods.eks.amazonaws.com/serviceaccount/eks-pod-identity-token",
        }
        self.creds = {
            "AccessKeyId": "SYNTHETIC-ACCESS",
            "SecretAccessKey": "SYNTHETIC-SECRET",
            "Token": "SYNTHETIC-SESSION",
            "Expiration": "2100-01-01T00:00:00Z",
        }
        self.uid = "12345678-1234-1234-1234-123456789012"
        self.good = {
            "version": 1,
            "credentialsDelivered": True,
            "allowedStatus": 200,
            "deniedStatus": 403,
            "passed": True,
        }

    def test_exact_lab_bucket_and_mutation_optin_required(self):
        lab.validate(self.args)
        for key, value in (
            ("run_probe", False),
            ("lab_run", "../unsafe"),
            ("allowed_bucket", "production-mimir-blocks"),
            ("denied_bucket", self.args.allowed_bucket),
            ("confirm_cluster_arn", "arn:aws:eks:us-west-2:123456789012:cluster/olly-lab-test"),
        ):
            args = copy.copy(self.args)
            setattr(args, key, value)
            with self.subTest(key=key), self.assertRaises(lab.Rejected):
                lab.validate(args)

    def test_job_uses_existing_role_without_api_token_or_secrets(self):
        image = "python:3.14.7-slim@sha256:" + "a" * 64
        job = lab.probe_job(self.args, "a" * 12, image)
        self.assertEqual(job["spec"]["activeDeadlineSeconds"], 90)
        self.assertEqual(job["spec"]["ttlSecondsAfterFinished"], 120)
        self.assertEqual(job["spec"]["backoffLimit"], 0)
        pod = job["spec"]["template"]["spec"]
        self.assertEqual(pod["serviceAccountName"], "mimir")
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertNotIn("volumes", pod)
        self.assertNotIn("env", pod["containers"][0])
        self.assertTrue(pod["containers"][0]["securityContext"]["readOnlyRootFilesystem"])
        with self.assertRaises(lab.Rejected):
            lab.probe_job(self.args, "a" * 12, "python:latest")

    def execute_probe(self, opener, env=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        args = [
            "probe",
            self.args.region,
            self.args.account,
            self.args.allowed_bucket,
            self.args.denied_bucket,
        ]
        with (
            patch.dict("os.environ", self.env if env is None else env, clear=True),
            patch("sys.argv", args),
            patch("pathlib.Path.open", mock_open(read_data="SYNTHETIC-JWT")),
            patch("urllib.request.build_opener", return_value=opener),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            exec(compile(lab.POD_PROBE, "probe", "exec"), {})
        self.assertEqual(stderr.getvalue(), "")
        for secret in ("SYNTHETIC", "AccessKeyId", "Authorization", "SecretAccessKey"):
            self.assertNotIn(secret, stdout.getvalue())
        return lab.approved_result(stdout.getvalue())

    def credential_response(self):
        response = Mock(status=200)
        response.read.return_value = json.dumps(self.creds).encode()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    def test_only_safe_statuses_leave_pod_and_both_requests_are_signed(self):
        response = Mock(status=200)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        error = urllib.error.HTTPError(
            "ignored", 403, "SYNTHETIC-SECRET", {}, io.BytesIO(b"PRIVATE-BODY")
        )
        opener = Mock()
        opener.open.side_effect = [self.credential_response(), response, error]
        self.assertEqual(self.execute_probe(opener), self.good)
        response.read.assert_not_called()
        calls = opener.open.call_args_list
        self.assertEqual(calls[0].args[0].full_url, self.env["AWS_CONTAINER_CREDENTIALS_FULL_URI"])
        for index, bucket in ((1, self.args.allowed_bucket), (2, self.args.denied_bucket)):
            request = calls[index].args[0]
            self.assertEqual(
                request.full_url,
                f"https://{bucket}.s3.us-east-2.amazonaws.com/?list-type=2&max-keys=1",
            )
            headers = {key.lower(): value for key, value in request.header_items()}
            self.assertEqual(headers["x-amz-expected-bucket-owner"], self.args.account)
            self.assertEqual(headers["x-amz-security-token"], self.creds["Token"])
            self.assertRegex(headers["authorization"], r"Signature=[0-9a-f]{64}$")
            self.assertIn("x-amz-expected-bucket-owner", headers["authorization"])
        self.assertNotEqual(
            calls[1].args[0].get_header("Authorization"),
            calls[2].args[0].get_header("Authorization"),
        )

    def test_credentials_are_never_requested_from_redirect_or_wrong_endpoint(self):
        for endpoint in (
            "http://169.254.169.254/latest/meta-data/",
            "https://169.254.170.23/v1/credentials",
            "http://169.254.170.23:8080/v1/credentials",
            "http://example.com/v1/credentials",
        ):
            opener = Mock()
            result = self.execute_probe(
                opener, {**self.env, "AWS_CONTAINER_CREDENTIALS_FULL_URI": endpoint}
            )
            self.assertFalse(result["credentialsDelivered"])
            opener.open.assert_not_called()
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError("ignored", 302, "SYNTHETIC-JWT", {}, None)
        self.assertFalse(self.execute_probe(opener)["passed"])
        self.assertEqual(opener.open.call_count, 1)

    def test_expired_credentials_and_raw_exceptions_are_suppressed(self):
        for expiry in ("2000-01-01T00:00:00Z", "bad", "2100-01-01T00:00:00"):
            self.creds["Expiration"] = expiry
            opener = Mock()
            opener.open.return_value = self.credential_response()
            self.assertFalse(self.execute_probe(opener)["credentialsDelivered"])
            self.assertEqual(opener.open.call_count, 1)
        opener = Mock()
        opener.open.side_effect = OSError("SYNTHETIC-SECRET")
        self.assertFalse(self.execute_probe(opener)["passed"])

    def test_unknown_fields_or_false_success_evidence_rejected(self):
        for change in (
            {"secret": "SYNTHETIC"},
            {"deniedStatus": 200},
            {"allowedStatus": True},
            {"credentialsDelivered": False},
        ):
            with self.subTest(change=change), self.assertRaises(lab.Rejected):
                lab.approved_result(json.dumps({**self.good, **change}))

    def test_job_cleanup_runs_after_probe_parse_failure(self):
        task = Mock(args=self.args, created=[])
        task.aws.return_value = {"LabRun": self.args.lab_run, "Environment": "lab"}
        task.kube.side_effect = [
            "python:3.14.7-slim@sha256:" + "a" * 64,
            self.uid,
            self.uid + "|1||",
            "NOT-JSON",
        ]
        task.cleanup.return_value = True
        with self.assertRaises(ValueError):
            lab.run_probe(task)
        task.cleanup.assert_called_once()
        self.assertEqual(task.created[0][1], self.uid)


if __name__ == "__main__":
    unittest.main()
