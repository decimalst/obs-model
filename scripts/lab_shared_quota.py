#!/usr/bin/env python3
"""Explicit disposable-lab check of one shared service quota across three replicas.

Emits 300 normalized synthetic ERROR records directly from an existing support pod.
This supplements, and bypasses the node-agent path of, lab_acceptance.py's neighbor
Jobs. No retry, new Kubernetes resources, Secret API reads or cloud mutations.
"""

import argparse
import json
import math
import os
import sys
import time
import uuid

if __package__:
    from .lab_acceptance import Acceptance, Rejected, require, validate_options
else:
    from lab_acceptance import Acceptance, Rejected, require, validate_options

REPLICAS = 3
PER_REPLICA = 100
RATE = 20
BURST = 40
MARGIN = 2
MAX_RECORD_BYTES = 1024
STABLE_SECONDS = 20

# Only this in-memory process reads mounted ClickHouse credentials. Responses,
# exception strings and request/authorization headers never become evidence.
SHARED_PROBE = r"""
import base64, concurrent.futures, datetime, importlib.util, json, pathlib, sys
import time, urllib.parse, uuid

try:
    mode, arg = sys.argv[1], json.loads(sys.argv[2])
    spec = importlib.util.spec_from_file_location("support", "/config/agent.py")
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    config = json.loads(pathlib.Path("/config/settings.json").read_text())
    if config["cluster"] != arg["cluster"]:
        raise ValueError("cluster mismatch")
    if mode == "burst":
        addresses = arg["addresses"]
        if len(addresses) != 3:
            raise ValueError("unexpected replica count")
        bodies = {}
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        for name in addresses:
            records = []
            for index in range(100):
                record = json.dumps({
                    "cluster_name": arg["cluster"], "namespace": "observability",
                    "service_name": arg["service"],
                    "service_key": "observability/" + arg["service"],
                    "timestamp": stamp, "event_id": str(uuid.uuid4()), "level": "ERROR",
                    "message": arg["marker"] + name + ":" + str(index), "trace_id": "",
                }, separators=(",", ":")).encode()
                if len(record) + 1 > 1024:
                    raise ValueError("record bound exceeded")
                records.append(record)
            bodies[name] = b"\n".join(records) + b"\n"

        def send(name):
            try:
                # support.http has bounded reads/timeouts and rejects redirects.
                status, _ = agent.http("http://" + addresses[name] + ":8080/ingest",
                    payload=bodies[name], headers={"Content-Type": "application/x-ndjson"})
                return status
            except Exception:
                return 0  # Never retry or include HTTP error bodies / exception text.

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            start = time.monotonic()  # Before ANY submit/send; excludes payload construction.
            futures = {name: pool.submit(send, name) for name in addresses}
            statuses = {name: future.result() for name, future in futures.items()}
            elapsed = time.monotonic() - start  # After ALL complete responses.
        out = {"statuses": statuses, "elapsedSeconds": elapsed}
    elif mode == "count":
        password = pathlib.Path(config["clickhousePasswordFile"]).read_text().strip()
        encoded = base64.b64encode(("canary:" + password).encode()).decode()
        headers = {"Authorization": "Basic " + encoded}
        sql = (
            "SELECT uniqExact(message) AS stored FROM observability.logs "
            "WHERE timestamp > now() - INTERVAL 15 MINUTE "
            "AND cluster_name={cluster:String} AND namespace='observability' "
            "AND service_name={service:String} AND startsWith(message, {marker:String}) "
            "FORMAT JSONEachRow"
        )
        query = {"query": sql, "param_cluster": arg["cluster"],
                 "param_service": arg["service"], "param_marker": arg["marker"]}
        _, body = agent.http(
            config["clickhouse"] + "/?" + urllib.parse.urlencode(query), headers=headers)
        row = json.loads(body)
        # ClickHouse may encode UInt64 as a decimal string; reject lossy float conversions.
        stored = row["stored"]
        if not ((type(stored) is int and stored >= 0) or
                (type(stored) is str and stored.isascii() and stored.isdecimal())):
            raise ValueError("invalid count")
        out = {"stored": int(stored)}
    else:
        raise ValueError("unsupported mode")
    print(json.dumps(out, allow_nan=False))
except Exception:
    print("Shared quota probe unavailable", file=sys.stderr)
    raise SystemExit(2)
"""


def burst_bound(result, pods):
    require(
        isinstance(result, dict) and set(result) == {"statuses", "elapsedSeconds"},
        "Unexpected burst evidence fields",
    )
    statuses, elapsed = result["statuses"], result["elapsedSeconds"]
    require(
        isinstance(statuses, dict)
        and set(statuses) == set(pods)
        and len(statuses) == REPLICAS
        and all(type(v) is int and v == 200 for v in statuses.values()),
        "Every quota replica must complete exactly one HTTP 200 request; no retries",
    )
    require(
        type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed > 0,
        "Burst duration must be a positive finite monotonic measurement",
    )
    bound = math.floor(BURST + RATE * elapsed + MARGIN)
    require(
        bound < BURST * REPLICAS,
        "Request window too broad to distinguish shared from per-pod bursts; inconclusive",
    )
    return bound


def quota_delta(before, after, pods, bound):
    require(
        isinstance(before, dict)
        and isinstance(after, dict)
        and set(before) == set(after) == set(pods),
        "Quota counter membership changed",
    )
    delta = {key: 0 for key in ("accepted", "dropped", "unavailable")}
    for pod in pods:
        require(
            isinstance(before[pod], dict)
            and isinstance(after[pod], dict)
            and set(before[pod]) == set(after[pod]) == set(delta),
            "Unexpected quota counter fields",
        )
        local = {}
        for key in delta:
            a, b = before[pod][key], after[pod][key]
            require(
                all(
                    type(v) in (int, float) and math.isfinite(v) and v >= 0 and v == int(v)
                    for v in (a, b)
                )
                and b >= a,
                "Invalid or reset quota counters; inconclusive",
            )
            local[key] = int(b - a)
            delta[key] += local[key]
        require(
            local["accepted"] + local["dropped"] == PER_REPLICA,
            "Counters must isolate 100 offered records per replica; "
            "background traffic is inconclusive",
        )
    require(delta["unavailable"] == 0, "Quota upstream/store unavailability invalidates the burst")
    require(delta["dropped"] > 0, "Expected intentional quota drops")
    require(
        0 < delta["accepted"] <= bound,
        "Accepted records exceed the shared bound or none were accepted",
    )
    return delta


def stored_evidence(count, delta, bound):
    require(type(count) is int and count >= 0, "Stored count must be a nonnegative integer")
    require(0 < count <= bound, "Stored records exceed the shared bound or none reached storage")
    require(
        count == delta["accepted"],
        "Not every accepted distinct marker reached durable storage; "
        "partial delivery is inconclusive",
    )


class SharedQuotaAcceptance(Acceptance):
    def shared_probe(self, pod, mode, argument):
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
                json.dumps(argument),
                data=SHARED_PROBE,
            )
        )

    def active_quota(self):
        pods = self.pods("log-quota")
        require(len(pods) == REPLICAS, "Require exactly three stable ready quota pods")
        projection = (
            '{range .items[*].endpoints[*]}{.targetRef.name}{"\\t"}'
            '{.targetRef.uid}{"\\t"}{.addresses[0]}{"\\t"}{.conditions.ready}{"\\n"}{end}'
        )
        rows = self.kube(
            "get",
            "endpointslices",
            "-l",
            "kubernetes.io/service-name=log-quota",
            "-o",
            "jsonpath=" + projection,
        ).splitlines()
        require(len(rows) == REPLICAS, "Require exactly three active quota Service endpoints")
        active = {}
        for line in rows:
            fields = line.split("\t")
            require(
                len(fields) == 4 and fields[3] == "true" and fields[0] not in active,
                "Active quota Service endpoints must be unique and ready",
            )
            active[fields[0]] = {"uid": fields[1], "ip": fields[2]}
        require(
            active == {name: {k: p[k] for k in ("uid", "ip")} for name, p in pods.items()},
            "Quota pods do not match the active Service endpoints",
        )
        return pods

    def guard_policy(self, pods):
        for pod in pods:
            for key, exact in [("EVENTS_PER_SECOND", RATE), ("EVENT_BURST", BURST)]:
                value = self.policy_value(pod, key)
                require(value == str(exact), "Require reviewed 20 EPS / 40 burst on every replica")
            value = self.policy_value(pod, "BYTE_BURST")
            require(
                value.isascii()
                and value.isdecimal()
                and int(value) >= REPLICAS * PER_REPLICA * MAX_RECORD_BYTES,
                "Byte burst must admit all synthetic bytes so byte throttling "
                "cannot mask event quotas",
            )

    def policy_value(self, pod, key):
        return self.kube(
            "get",
            "pod",
            pod,
            "-o",
            'jsonpath={.spec.containers[0].env[?(@.name=="' + key + '")].value}',
        )

    def run_burst(self):
        support, quota = self.pods("olly-support"), self.active_quota()
        require(len(support) == 2, "Both ready support replicas are required")
        self.guard_policy(quota)
        support_pod = sorted(support)[0]
        run_id = uuid.uuid4().hex
        argument = {
            "cluster": self.args.cluster,
            "service": "lab-shared-" + run_id,
            "marker": "lab-shared-quota:" + run_id + ":",
            "addresses": {name: item["ip"] for name, item in quota.items()},
        }
        before = self.probe(support_pod, "quota", argument["addresses"])
        burst = self.shared_probe(support_pod, "burst", argument)
        bound = burst_bound(burst, quota)
        after = self.probe(support_pod, "quota", argument["addresses"])
        delta = quota_delta(before, after, quota, bound)
        previous, stable_since = None, None
        while True:
            require(
                self.active_quota() == quota and self.pods("olly-support") == support,
                "Active quota/support identity or restart count changed; inconclusive",
            )
            sample = self.shared_probe(support_pod, "count", argument)
            require(
                isinstance(sample, dict)
                and set(sample) == {"stored"}
                and type(sample["stored"]) is int
                and sample["stored"] >= 0,
                "Unexpected stored count evidence",
            )
            count = sample["stored"]
            require(count <= delta["accepted"], "Stored count exceeds the isolated accepted count")
            now = time.monotonic()
            if count != previous:
                previous, stable_since = count, now
            if count == delta["accepted"] and now - stable_since >= STABLE_SECONDS:
                break
            self.pause()
        stored_evidence(count, delta, bound)
        return {
            "runId": run_id,
            "service": argument["service"],
            "marker": argument["marker"],
            "replicas": quota,
            "offeredPerReplica": PER_REPLICA,
            "offeredTotal": REPLICAS * PER_REPLICA,
            "requestResult": burst,
            "sharedBound": bound,
            "marginEvents": MARGIN,
            "storedDistinct": count,
            "quotaCounterDelta": delta,
            "stableObservationSeconds": STABLE_SECONDS,
            "scope": "Direct normalized ingress to quota replicas; node-agent path bypassed. "
            "Per-service token sharing checked only for this stable, isolated burst. "
            "No failover, scaling, sustained-load or full qualification claim.",
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("context", "cluster", "region", "account", "confirm-cluster-arn", "output"):
        parser.add_argument("--" + flag, required=True)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    report = {"version": 1, "passed": False, "qualificationGatePassed": False}
    code = 1
    try:
        task = SharedQuotaAcceptance(args)
        report["identity"] = task.identity()
        report["startedAt"] = time.time()
        report["sharedQuotaBurst"] = task.run_burst()
        report["passed"], code = True, 0
    except (Rejected, ValueError, KeyError, OSError, TypeError) as error:
        report["failure"] = (
            str(error)
            if isinstance(error, Rejected)
            else ("Invalid or unavailable prerequisite/probe response; raw details suppressed")
        )
    report["completedAt"] = time.time()
    try:
        validate_options(args)
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(report, stream, indent=2, allow_nan=False)
            stream.write("\n")
    except (Rejected, OSError, ValueError):
        print("Shared quota evidence could not be saved to a new private file", file=sys.stderr)
        return 1
    print(
        "Shared quota check passed"
        if code == 0
        else "Shared quota check incomplete; inspect evidence"
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
