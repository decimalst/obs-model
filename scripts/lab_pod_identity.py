#!/usr/bin/env python3
"""Create one bounded lab Job to test existing Mimir Pod Identity S3 permissions.

Requires explicit lab identity, LabRun and exact known lab bucket names. The Job
uses only its injected Pod Identity token; credentials and HTTP bodies remain in
pod memory. No IAM changes, Secret reads, object writes or production targets.
"""

import argparse
import json
import os
import re
import sys
import time
import uuid

if __package__:
    from .lab_acceptance import Acceptance, Rejected, require, validate_options
else:
    from lab_acceptance import Acceptance, Rejected, require, validate_options


POD_PROBE = r"""
import datetime, hashlib, hmac, json, os, pathlib, re, sys
import urllib.error, urllib.parse, urllib.request

out = {"version": 1, "credentialsDelivered": False,
       "allowedStatus": 0, "deniedStatus": 0, "passed": False}
try:
    region, account, allowed, denied = sys.argv[1:]
    if not re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]", region):
        raise ValueError()
    if not re.fullmatch(r"[0-9]{12}", account):
        raise ValueError()
    if not all(re.fullmatch(r"[a-z0-9-]{3,63}", b) for b in (allowed, denied)):
        raise ValueError()
    endpoint = os.environ["AWS_CONTAINER_CREDENTIALS_FULL_URI"]
    if endpoint not in ("http://169.254.170.23/v1/credentials",
                         "http://169.254.170.23:80/v1/credentials"):
        raise ValueError()
    token_path = os.environ["AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE"]
    expected_path = "/var/run/secrets/pods.eks.amazonaws.com/serviceaccount/eks-pod-identity-token"
    if token_path != expected_path:
        raise ValueError()
    with pathlib.Path(token_path).open() as stream:
        token = stream.read(16385)
    if not 1 <= len(token) <= 16384 or "\n" in token or "\r" in token:
        raise ValueError()

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    # Ignore proxy environment variables; never send Pod Identity tokens elsewhere.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(urllib.request.Request(endpoint, headers={"Authorization": token}),
                     timeout=5) as response:
        if response.status != 200:
            raise ValueError()
        payload = response.read(8193)
    if len(payload) > 8192:
        raise ValueError()
    credentials = json.loads(payload)
    for name in ("AccessKeyId", "SecretAccessKey", "Token"):
        value = credentials[name]
        if (not isinstance(value, str) or not 1 <= len(value) <= 4096
                or "\n" in value or "\r" in value):
            raise ValueError()
    expires = datetime.datetime.fromisoformat(credentials["Expiration"].replace("Z", "+00:00"))
    if expires <= datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=60):
        raise ValueError()
    out["credentialsDelivered"] = True

    def status(bucket):
        host = bucket + ".s3." + region + ".amazonaws.com"
        now = datetime.datetime.now(datetime.timezone.utc)
        day, stamp = now.strftime("%Y%m%d"), now.strftime("%Y%m%dT%H%M%SZ")
        payload_hash = hashlib.sha256(b"").hexdigest()
        headers = {"host": host, "x-amz-content-sha256": payload_hash,
                   "x-amz-date": stamp, "x-amz-security-token": credentials["Token"],
                   "x-amz-expected-bucket-owner": account}
        names = sorted(headers)
        signed_headers = ";".join(names)
        query = "list-type=2&max-keys=1"
        canonical = ("GET\n/\n" + query + "\n" +
                     "".join(name + ":" + headers[name] + "\n" for name in names) +
                     "\n" + signed_headers + "\n" + payload_hash)
        scope = day + "/" + region + "/s3/aws4_request"
        to_sign = ("AWS4-HMAC-SHA256\n" + stamp + "\n" + scope + "\n" +
                   hashlib.sha256(canonical.encode()).hexdigest())
        key = ("AWS4" + credentials["SecretAccessKey"]).encode()
        for piece in (day, region, "s3", "aws4_request"):
            key = hmac.new(key, piece.encode(), hashlib.sha256).digest()
        signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
        headers["Authorization"] = ("AWS4-HMAC-SHA256 Credential=" +
            credentials["AccessKeyId"] + "/" + scope + ", SignedHeaders=" +
            signed_headers + ", Signature=" + signature)
        try:
            with opener.open(urllib.request.Request("https://" + host + "/?" + query,
                             headers=headers), timeout=10) as response:
                return response.status  # Never read or print bucket contents.
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()  # Never read or print error bodies or exception strings.
            return code

    out["allowedStatus"] = status(allowed)
    out["deniedStatus"] = status(denied)
    out["passed"] = out["allowedStatus"] == 200 and out["deniedStatus"] == 403
except Exception:
    pass  # No credentials, token, response bodies, URLs or exception text in output.
print(json.dumps(out, allow_nan=False))
"""


def validate(args):
    validate_options(args)
    require(args.run_probe, "Explicit --run-probe is required to create a temporary lab Job")
    require(re.fullmatch(r"[a-z0-9][a-z0-9-]{2,63}", args.lab_run), "Supply the exact LabRun tag")
    prefix = f"{args.cluster}-{args.account}-{args.region}-"
    require(
        args.allowed_bucket == prefix + "mimir-blocks"
        and args.denied_bucket == prefix + "artifacts"
        and max(len(args.allowed_bucket), len(args.denied_bucket)) <= 63,
        "Supply this lab's exact known Mimir blocks and artifact bucket names",
    )


def probe_job(args, run_id, image):
    require(re.fullmatch(r"[0-9a-f]{12}", run_id), "Invalid probe run identity")
    require(
        re.fullmatch(r"python:[0-9.]+-slim@sha256:[0-9a-f]{64}", image),
        "Probe requires the existing digest-pinned Python support image",
    )
    labels = {"observability.good-olly.io/pod-identity-test": run_id}
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": "lab-pod-identity-" + run_id,
            "namespace": "observability",
            "labels": labels,
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 90,
            "ttlSecondsAfterFinished": 120,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "serviceAccountName": "mimir",
                    "automountServiceAccountToken": False,
                    "restartPolicy": "Never",
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 10001,
                        "runAsGroup": 10001,
                        "fsGroup": 10001,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "probe",
                            "image": image,
                            "command": [
                                "python",
                                "-B",
                                "-c",
                                POD_PROBE,
                                args.region,
                                args.account,
                                args.allowed_bucket,
                                args.denied_bucket,
                            ],
                            "resources": {
                                "requests": {"cpu": "25m", "memory": "32Mi"},
                                "limits": {"cpu": "100m", "memory": "64Mi"},
                            },
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                        }
                    ],
                },
            },
        },
    }


def approved_result(raw):
    require(len(raw) <= 1024, "Probe output exceeded the aggregate evidence limit")
    result = json.loads(raw)
    require(
        isinstance(result, dict)
        and set(result)
        == {"version", "credentialsDelivered", "allowedStatus", "deniedStatus", "passed"},
        "Unexpected probe evidence fields",
    )
    require(
        type(result["version"]) is int
        and result["version"] == 1
        and all(type(result[key]) is bool for key in ("credentialsDelivered", "passed"))
        and all(
            type(result[key]) is int and result[key] in {0, *range(100, 600)}
            for key in ("allowedStatus", "deniedStatus")
        ),
        "Invalid probe evidence types",
    )
    require(
        result["passed"]
        == (
            result["credentialsDelivered"]
            and result["allowedStatus"] == 200
            and result["deniedStatus"] == 403
        ),
        "Inconsistent probe evidence",
    )
    return result


def run_probe(task):
    args = task.args
    tags = task.aws("eks", "describe-cluster", "--name", args.cluster, "--query", "cluster.tags")
    require(
        tags.get("LabRun") == args.lab_run and tags.get("Environment") == "lab",
        "Cluster tags do not match the explicitly selected lab run",
    )
    image = task.kube(
        "get",
        "deployment",
        "olly-support",
        "-o",
        "jsonpath={.spec.template.spec.containers[0].image}",
    )
    run_id = uuid.uuid4().hex[:12]
    job = probe_job(args, run_id, image)
    name = job["metadata"]["name"]
    uncertain = False
    try:
        uncertain = True
        try:
            uid = task.kube(
                "create", "-f", "-", "-o", "jsonpath={.metadata.uid}", data=json.dumps(job)
            )
            require(re.fullmatch(r"[0-9a-f-]{36}", uid), "Invalid created Job identity")
            task.created.append((name, uid))
            uncertain = False
        except Rejected:
            # A create timeout can leave the Job alive. Recover only its exact nonce/UID.
            identity = task.kube(
                "get",
                "job",
                name,
                "--ignore-not-found",
                "-o",
                'jsonpath={.metadata.uid}{"|"}{.metadata.labels.observability\\.good-olly\\.io/pod-identity-test}',
            )
            if not identity:
                uncertain = False
            else:
                fields = identity.split("|")
                require(
                    len(fields) == 2
                    and re.fullmatch(r"[0-9a-f-]{36}", fields[0])
                    and fields[1] == run_id,
                    "Job creation ownership could not be recovered",
                )
                task.created.append((name, fields[0]))
                uncertain = False
            raise
        while True:
            state = task.kube(
                "get",
                "job",
                name,
                "-o",
                'jsonpath={.metadata.uid}{"|"}{.status.succeeded}{"|"}{.status.failed}{"|"}'
                '{.status.conditions[?(@.type=="Failed")].status}',
            )
            fields = state.split("|")
            require(len(fields) == 4 and fields[0] == uid, "Probe Job identity changed")
            require(fields[2] in ("", "0") and fields[3] != "True", "Probe Job did not complete")
            if fields[1] == "1":
                break
            require(fields[1] in ("", "0"), "Unexpected probe completion count")
            task.pause()
        result = approved_result(
            task.kube("logs", "job/" + name, "-c", "probe", "--tail=2", "--limit-bytes=2048")
        )
    finally:
        clean = task.cleanup()
        require(
            clean and not uncertain, "Probe Job cleanup incomplete; inspect this lab's probe Job"
        )
    return {**result, "jobsCleanedUp": True, "runId": run_id}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in (
        "context",
        "cluster",
        "region",
        "account",
        "confirm-cluster-arn",
        "output",
        "lab-run",
        "allowed-bucket",
        "denied-bucket",
    ):
        parser.add_argument("--" + flag, required=True)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--run-probe", action="store_true")
    args = parser.parse_args()
    report = {"version": 1, "passed": False, "qualificationGatePassed": False}
    code = 1
    try:
        validate(args)
        task = Acceptance(args)
        report["identity"] = task.identity()
        report["startedAt"] = time.time()
        report["probe"] = run_probe(task)
        report["passed"] = report["probe"]["passed"]
        report["limitations"] = (
            "Tests existing Mimir service-account credential delivery and S3 ListBucket allow/deny "
            "only; no object operations, other workload roles or full effective-policy audit."
        )
        code = 0 if report["passed"] else 1
    except (Rejected, ValueError, KeyError, OSError, TypeError):
        report["failure"] = (
            "Lab prerequisite, bounded probe or cleanup failed; raw details suppressed"
        )
    report["completedAt"] = time.time()
    try:
        validate(args)
        descriptor = os.open(
            args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "w") as stream:
            json.dump(report, stream, indent=2, allow_nan=False)
            stream.write("\n")
    except (Rejected, OSError, ValueError):
        print("Pod Identity evidence could not be saved to a new private file", file=sys.stderr)
        return 1
    print(
        "Pod Identity check passed; aggregate evidence saved"
        if code == 0
        else "Pod Identity check incomplete; inspect aggregate evidence"
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
