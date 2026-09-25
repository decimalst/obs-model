#!/usr/bin/env python3
"""Explicit live lab checks. Default: read-only fresh canary; --noisy-neighbor creates Jobs.

No Terraform, cloud mutation, Secret API reads, raw logs or notification delivery.
AWS identity/EKS reads bind the explicit Kubernetes context to the selected lab.
"""

import argparse
import ipaddress
import json
import math
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

SIGNALS = ("logs", "metrics", "traces")
COUNTERS = ("accepted", "dropped", "unavailable")
NAMESPACE = "observability"
DURATION = 30
FLOOD_RATE = 50
QUIET_RATE = 1
FLOOD_PODS = 2

# Executed inside the already deployed support container. Credential material
# never leaves its mounted file / process memory, including on errors.
POD_PROBE = r"""
import base64, importlib.util, json, pathlib, re, sys, urllib.error, urllib.parse

try:
    mode, arg = sys.argv[1], json.loads(sys.argv[2])
    spec = importlib.util.spec_from_file_location("support", "/config/agent.py")
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)

    def metrics(url, names):
        _, body = agent.http(url)
        text = body.decode()
        out = {}
        for key, metric in names.items():
            match = re.search(r"^" + re.escape(metric) + r" ([0-9.eE+\-]+)$", text, re.M)
            if not match:
                raise ValueError("missing metric")
            out[key] = float(match.group(1))
        return out

    if mode == "canary":
        names = {
            "completed": "olly_support_completed_timestamp_seconds",
            "interval": "olly_support_interval_seconds",
        }
        for signal in ("logs", "metrics", "traces"):
            names[signal + "Success"] = 'olly_support_check_success{signal="' + signal + '"}'
            names[signal + "At"] = (
                'olly_support_last_success_timestamp_seconds{signal="' + signal + '"}'
            )
        out = metrics("http://127.0.0.1:8000/metrics", names)
        try:
            out["health"] = agent.http("http://127.0.0.1:8000/health")[0]
        except urllib.error.HTTPError as error:
            out["health"] = error.code
    elif mode == "quota":
        out = {
            name: metrics(
                "http://" + address + ":8080/metrics",
                {
                    key: "log_quota_" + key + "_total"
                    for key in ("accepted", "dropped", "unavailable")
                },
            )
            for name, address in arg.items()
        }
    elif mode == "counts":
        config = json.loads(pathlib.Path("/config/settings.json").read_text())
        password = pathlib.Path(config["clickhousePasswordFile"]).read_text().strip()
        headers = {
            "Authorization": "Basic " + base64.b64encode(("canary:" + password).encode()).decode()
        }
        sql = (
            "SELECT uniqExactIf(message, service_name={quiet:String}) AS quiet, "
            "uniqExactIf(message, service_name={flood:String}) AS flood "
            "FROM observability.logs WHERE timestamp > now() - INTERVAL 15 MINUTE "
            "AND cluster_name={cluster:String} AND namespace='observability' "
            "AND startsWith(message, {marker:String}) FORMAT JSONEachRow"
        )
        query = {
            "query": sql,
            "param_cluster": config["cluster"],
            "param_quiet": arg["quiet"],
            "param_flood": arg["flood"],
            "param_marker": arg["marker"],
        }
        _, body = agent.http(
            config["clickhouse"] + "/?" + urllib.parse.urlencode(query), headers=headers
        )
        row = json.loads(body)
        out = {key: int(row[key]) for key in ("quiet", "flood")}
    else:
        raise ValueError("unsupported probe")
    print(json.dumps(out, allow_nan=False))
except Exception:
    print("Support probe unavailable", file=sys.stderr)
    raise SystemExit(2)
"""

PRODUCER = r"""
import json, os, sys, time

start, rate, count = float(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
marker, kind = sys.argv[4], sys.argv[5]
if time.time() > start:
    raise SystemExit("Missed synthetic start; no traffic emitted")
time.sleep(max(0, start - time.time()))
origin = time.monotonic()
for index in range(count):
    time.sleep(max(0, origin + index / rate - time.monotonic()))
    print(
        json.dumps(
            {
                "level": "ERROR" if kind == "flood" else "INFO",
                "message": marker + kind + ":" + os.environ["POD_NAME"] + ":" + str(index),
            }
        ),
        flush=True,
    )
"""


class Rejected(RuntimeError):
    """Safe operator-facing error; never embed subprocess output or credentials."""


def require(condition, message):
    if not condition:
        raise Rejected(message)


def validate_options(args):
    require(
        re.fullmatch(r"olly-lab-[a-z0-9]([a-z0-9-]{0,8}[a-z0-9])?", args.cluster),
        "Use the exact olly-lab- cluster name (18 characters maximum)",
    )
    require(re.fullmatch(r"[0-9]{12}", args.account), "Supply the explicit 12-digit account")
    require(
        re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]", args.region), "Supply the explicit commercial region"
    )
    require(
        re.fullmatch(r"[A-Za-z0-9:/@_.+=,-]{1,256}", args.context),
        "Supply an explicit safe Kubernetes context name",
    )
    require(180 <= args.timeout <= 1200, "Timeout must be 180-1200 seconds")
    expected = f"arn:aws:eks:{args.region}:{args.account}:cluster/{args.cluster}"
    require(args.confirm_cluster_arn == expected, "Confirm the exact account/region/cluster ARN")
    require(
        not Path(args.output).exists() and not Path(args.output).is_symlink(),
        "Evidence output must be a new file",
    )
    require(
        Path(args.output).parent.is_dir(), "Evidence output parent directory must already exist"
    )
    return expected


def execute(command, data=None):
    try:
        result = subprocess.run(
            command, input=data, capture_output=True, text=True, timeout=35, check=True
        )
    except (subprocess.SubprocessError, OSError) as error:
        raise Rejected(
            "A bounded prerequisite/probe command failed; raw output was suppressed"
        ) from error
    require(len(result.stdout) <= 1024 * 1024, "Probe response exceeded the evidence limit")
    return result.stdout.rstrip("\n")


def fresh_canary(sample, after, now):
    keys = {"completed", "interval", "health"} | {
        s + ending for s in SIGNALS for ending in ("Success", "At")
    }
    require(
        isinstance(sample, dict) and set(sample) == keys,
        "Canary returned unexpected evidence fields",
    )
    require(
        all(
            isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
            for v in sample.values()
        ),
        "Canary evidence must contain finite numbers",
    )
    require(1 <= sample["interval"] <= 900, "Canary interval is invalid")
    return (
        sample["health"] == 200
        and after < sample["completed"] <= now
        and now - sample["completed"] < 2 * sample["interval"]
        and all(
            sample[s + "Success"] == 1 and after < sample[s + "At"] <= sample["completed"]
            for s in SIGNALS
        )
    )


def fairness_evidence(counts, before, after):
    require(
        isinstance(counts, dict) and set(counts) == {"quiet", "flood"},
        "Unexpected stored count fields",
    )
    require(
        all(type(v) is int and v >= 0 for v in counts.values()),
        "Stored counts must be nonnegative integers",
    )
    require(
        set(before) == set(after) and bool(before), "Quota pod membership changed during the test"
    )
    delta = {key: 0 for key in COUNTERS}
    active = 0
    for pod in before:
        require(
            set(before[pod]) == set(after[pod]) == set(COUNTERS), "Unexpected quota counter fields"
        )
        for key in COUNTERS:
            a, b = before[pod][key], after[pod][key]
            require(
                all(
                    type(v) in (int, float) and math.isfinite(v) and v >= 0 and v == int(v)
                    for v in (a, b)
                ),
                "Quota counters must be finite nonnegative integers",
            )
            require(b >= a, "Quota counter reset makes this run inconclusive")
            delta[key] += int(b - a)
        active += int(
            after[pod]["accepted"] + after[pod]["dropped"]
            > before[pod]["accepted"] + before[pod]["dropped"]
        )
    expected_quiet = DURATION * QUIET_RATE
    offered_flood = DURATION * FLOOD_RATE * FLOOD_PODS
    require(
        counts["quiet"] == expected_quiet, "Not every distinct quiet-service marker reached storage"
    )
    require(0 < counts["flood"] < offered_flood, "Stored ERROR flood was not demonstrably limited")
    require(
        delta["dropped"] > 0 and delta["unavailable"] == 0,
        "Expected quota drops without upstream/store unavailability",
    )
    return {
        "offered": {"quiet": expected_quiet, "flood": offered_flood},
        "storedDistinct": counts,
        "quotaCounterDelta": delta,
        "quotaPodsWithTraffic": active,
        "scope": (
            "Stored service markers prove neighbor delivery; quota counters aggregate all services "
            "on each pod. No exact per-service token accounting or failover claim."
        ),
    }


def producer_job(name, run_id, kind, image, start):
    rate = FLOOD_RATE if kind == "flood" else QUIET_RATE
    replicas = FLOOD_PODS if kind == "flood" else 1
    labels = {
        "observability/service": f"lab-{kind}-{run_id}",
        "observability.good-olly.io/lab-acceptance": run_id,
    }
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": NAMESPACE, "labels": labels},
        "spec": {
            "parallelism": replicas,
            "completions": replicas,
            "backoffLimit": 0,
            "activeDeadlineSeconds": 120,
            "ttlSecondsAfterFinished": 300,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 65532,
                        "runAsGroup": 65532,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "producer",
                            "image": image,
                            "command": [
                                "python",
                                "-B",
                                "-c",
                                PRODUCER,
                                str(start),
                                str(rate),
                                str(DURATION * rate),
                                f"lab-acceptance:{run_id}:",
                                kind,
                            ],
                            "env": [
                                {
                                    "name": "POD_NAME",
                                    "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}},
                                }
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


class Acceptance:
    def __init__(self, args, command=execute):
        self.args, self.command = args, command
        self.expected_arn = validate_options(args)
        self.deadline = time.monotonic() + args.timeout
        self.created = []

    def kube(self, *args, data=None):
        return self.command(
            [
                "kubectl",
                "--context",
                self.args.context,
                "--request-timeout=25s",
                "-n",
                NAMESPACE,
                *args,
            ],
            data,
        )

    def aws(self, service, action, *args):
        return json.loads(
            self.command(
                [
                    "aws",
                    service,
                    action,
                    *args,
                    "--region",
                    self.args.region,
                    "--output",
                    "json",
                    "--no-cli-pager",
                ]
            )
        )

    def pause(self):
        require(time.monotonic() < self.deadline, "Live acceptance deadline elapsed")
        time.sleep(min(5, max(0, self.deadline - time.monotonic())))

    def identity(self):
        caller = self.aws("sts", "get-caller-identity")
        require(
            caller.get("Account") == self.args.account,
            "AWS caller account does not match the confirmed lab",
        )
        cluster = self.aws("eks", "describe-cluster", "--name", self.args.cluster)["cluster"]
        require(
            cluster.get("arn") == self.expected_arn and cluster.get("status") == "ACTIVE",
            "EKS identity/status does not match the confirmed lab",
        )
        require(
            cluster["resourcesVpcConfig"]["endpointPublicAccess"] is False,
            "Lab acceptance requires the private EKS endpoint",
        )
        endpoint = self.kube(
            "config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}"
        )
        require(
            endpoint.rstrip("/") == cluster["endpoint"].rstrip("/"),
            "Kubernetes context points at a different API server",
        )
        require(
            self.kube(
                "get",
                "namespace",
                NAMESPACE,
                "-o",
                "jsonpath={.metadata.labels.observability/collect}",
            )
            == "true",
            "Lab namespace must explicitly opt into log collection",
        )
        return {
            "account": self.args.account,
            "region": self.args.region,
            "clusterArn": self.expected_arn,
            "context": self.args.context,
        }

    def pods(self, app):
        projection = (
            '{range .items[*]}{.metadata.name}{"\\t"}{.metadata.uid}{"\\t"}'
            '{.status.podIP}{"\\t"}{.status.phase}{"\\t"}'
            '{.status.containerStatuses[0].ready}{"\\t"}'
            '{.status.containerStatuses[0].restartCount}{"\\n"}{end}'
        )
        raw = self.kube("get", "pods", "-l", f"app={app}", "-o", "jsonpath=" + projection)
        result = {}
        for line in raw.splitlines():
            fields = line.split("\t")
            require(
                len(fields) == 6 and fields[3:5] == ["Running", "true"],
                "Selected probe/quota pods must all be running and ready",
            )
            name, uid, address = fields[:3]
            require(
                re.fullmatch(r"[a-z0-9-]{1,253}", name) and re.fullmatch(r"[0-9a-f-]{36}", uid),
                "Unexpected pod identity",
            )
            ip = ipaddress.ip_address(address)
            require(
                ip.version == 4 and ip.is_private and not ip.is_loopback and not ip.is_link_local,
                "Probe target must use a private pod IPv4 address",
            )
            require(fields[5].isdigit(), "Unexpected restart count")
            result[name] = {"uid": uid, "ip": address, "restarts": int(fields[5])}
        require(result, "No matching probe/quota pods")
        return result

    def probe(self, pod, mode, argument=None):
        require(time.monotonic() < self.deadline, "Live acceptance deadline elapsed")
        return json.loads(
            self.kube(
                "exec",
                "-i",
                "pod/" + pod,
                "--",
                "python",
                "-B",
                "-",
                mode,
                json.dumps(argument or {}),
                data=POD_PROBE,
            )
        )

    def canaries(self, after, cycles=2):
        pods = self.pods("olly-support")
        require(len(pods) == 2, "Both support replicas are required")
        samples = []
        for _ in range(cycles):
            while True:
                require(
                    self.pods("olly-support") == pods,
                    "Support membership changed; rerun against stable replicas",
                )
                try:
                    current = {pod: self.probe(pod, "canary") for pod in pods}
                except Rejected:
                    self.pause()
                    continue
                if all(fresh_canary(sample, after, time.time()) for sample in current.values()):
                    samples.append(current)
                    after = max(sample["completed"] for sample in current.values())
                    break
                self.pause()
        return {"replicas": 2, "freshCycles": cycles, "samples": samples}

    def cleanup(self):
        clean = True
        for name, uid in reversed(self.created):
            try:
                found = self.kube(
                    "get", "job", name, "--ignore-not-found", "-o", "jsonpath={.metadata.uid}"
                )
                if not found:
                    continue
                require(found == uid, "Synthetic Job ownership changed; refuse deletion")
                options = {
                    "apiVersion": "v1",
                    "kind": "DeleteOptions",
                    "preconditions": {"uid": uid},
                    "propagationPolicy": "Foreground",
                }
                self.kube(
                    "delete",
                    "--raw",
                    f"/apis/batch/v1/namespaces/{NAMESPACE}/jobs/{name}",
                    "-f",
                    "-",
                    data=json.dumps(options),
                )
                # Acknowledging DeleteOptions alone is not completed cleanup.
                end = time.monotonic() + 40
                while (
                    self.kube(
                        "get", "job", name, "--ignore-not-found", "-o", "jsonpath={.metadata.uid}"
                    )
                    == uid
                ):
                    require(time.monotonic() < end, "Synthetic Job cleanup did not complete")
                    time.sleep(2)
            except (Rejected, ValueError, KeyError, OSError):
                clean = False
        return clean

    def noisy(self):
        support = self.pods("olly-support")
        support_pod = sorted(support)[0]
        quota = self.pods("log-quota")
        for pod in quota:
            for key, expected in [("EVENTS_PER_SECOND", "20"), ("EVENT_BURST", "40")]:
                value = self.kube(
                    "get",
                    "pod",
                    pod,
                    "-o",
                    'jsonpath={.spec.containers[0].env[?(@.name=="' + key + '")].value}',
                )
                require(
                    value == expected,
                    "Noisy test requires reviewed 20 EPS / 40 burst on every quota replica",
                )
        image = self.kube(
            "get",
            "deployment",
            "olly-support",
            "-o",
            "jsonpath={.spec.template.spec.containers[0].image}",
        )
        require(
            re.fullmatch(r"python:[0-9.]+-slim@sha256:[0-9a-f]{64}", image),
            "Synthetic producers require the existing digest-pinned Python support image",
        )
        addresses = {name: pod["ip"] for name, pod in quota.items()}
        before = self.probe(support_pod, "quota", addresses)
        run_id = uuid.uuid4().hex[:12]
        start = time.time() + 45
        try:
            for kind in ("flood", "quiet"):
                name = f"lab-acceptance-{kind}-{run_id}"
                obj = producer_job(name, run_id, kind, image, start)
                uid = self.kube(
                    "create", "-f", "-", "-o", "jsonpath={.metadata.uid}", data=json.dumps(obj)
                )
                require(
                    re.fullmatch(r"[0-9a-f-]{36}", uid), "Created Job returned an invalid identity"
                )
                self.created.append((name, uid))
            while True:
                completed = True
                for name, uid in self.created:
                    state = self.kube(
                        "get",
                        "job",
                        name,
                        "-o",
                        'jsonpath={.metadata.uid}{"\\t"}{.status.succeeded}{"\\t"}{.status.failed}',
                    )
                    fields = state.split("\t")
                    require(
                        len(fields) == 3 and fields[0] == uid and not int(fields[2] or 0),
                        "Synthetic Job failed or identity changed",
                    )
                    completed &= int(fields[1] or 0) == (FLOOD_PODS if "-flood-" in name else 1)
                if completed:
                    break
                self.pause()
            query = {
                "quiet": f"lab-quiet-{run_id}",
                "flood": f"lab-flood-{run_id}",
                "marker": f"lab-acceptance:{run_id}:",
            }
            while True:
                require(
                    self.pods("olly-support") == support and self.pods("log-quota") == quota,
                    "Probe/quota membership changed during the noisy test",
                )
                counts = self.probe(support_pod, "counts", query)
                if counts.get("quiet") == DURATION * QUIET_RATE and counts.get("flood", 0) > 0:
                    break
                self.pause()
            canary = self.canaries(start, cycles=1)
            # Allow post-production drain and require a stable aggregate window.
            # This is bounded observation, not a proof that all future queues are empty.
            stable_since = time.monotonic()
            previous = self.probe(support_pod, "counts", query)
            while time.monotonic() - stable_since < 20:
                self.pause()
                current = self.probe(support_pod, "counts", query)
                if current != previous:
                    stable_since, previous = time.monotonic(), current
            require(self.pods("log-quota") == quota, "Quota membership changed during drain")
            after = self.probe(support_pod, "quota", addresses)
            result = fairness_evidence(previous, before, after)
            result["freshCanaryAfterStart"] = canary
            result["stableObservationSeconds"] = 20
            result["runId"] = run_id
            result["durationSeconds"] = DURATION
        finally:
            require(
                self.cleanup(),
                "Synthetic Job cleanup incomplete; review only this run's exact Jobs",
            )
        result["jobsCleanedUp"] = True
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("context", "cluster", "region", "account", "confirm-cluster-arn", "output"):
        parser.add_argument("--" + flag, required=True)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument(
        "--noisy-neighbor",
        action="store_true",
        help="Explicitly create then delete bounded ERROR/INFO producer Jobs",
    )
    args = parser.parse_args()
    report = {"version": 1, "passed": False, "qualificationGatePassed": False}
    code = 1
    try:
        task = Acceptance(args)
        report["identity"] = task.identity()
        report["startedAt"] = time.time()
        report["canary"] = task.canaries(report["startedAt"])
        if args.noisy_neighbor:
            report["noisyNeighbor"] = task.noisy()
        report["passed"] = True
        report["limitations"] = [
            "Successful stored signals and bounded log fairness only; partial-ingestion rejection, "
            "Valkey failure/failover, scaling, recovery and full qualification remain pending."
        ]
        code = 0
    except (Rejected, ValueError, KeyError, OSError, TypeError) as error:
        report["failure"] = (
            str(error)
            if isinstance(error, Rejected)
            else "Invalid or unavailable prerequisite/probe response; raw details suppressed"
        )
    report["completedAt"] = time.time()
    try:
        validate_options(args)
        descriptor = os.open(
            args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "w") as stream:
            json.dump(report, stream, indent=2, allow_nan=False)
            stream.write("\n")
    except (Rejected, OSError, ValueError):
        print("Acceptance evidence could not be saved to a new private file", file=sys.stderr)
        return 1
    print(
        "Lab acceptance checks passed; aggregate evidence saved"
        if code == 0
        else "Lab acceptance incomplete; inspect aggregate evidence"
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
