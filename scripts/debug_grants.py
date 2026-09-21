#!/usr/bin/env python3
"""Short-lived workload debug grants. Collectors enforce expiry independently."""

import argparse
import datetime as dt
import json
import os
import ssl
import subprocess
import urllib.request

PREFIX = "observability.good-olly.io/"
FROM, UNTIL = PREFIX + "debug-from", PREFIX + "debug-until"
RESOURCES = {"deployment": "deployments", "statefulset": "statefulsets", "daemonset": "daemonsets"}
UTC = dt.timezone.utc


def parse_time(value):
    result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamp must have a timezone")
    return result.astimezone(UTC)


def active(annotations, now):
    try:
        start, end = parse_time(annotations[FROM]), parse_time(annotations[UNTIL])
        return start <= now < end and dt.timedelta(0) < end - start <= dt.timedelta(hours=24)
    except (KeyError, TypeError, ValueError):
        return False


def grant(hours, now):
    if not 0 < hours <= 24:
        raise ValueError("Grant duration must be greater than zero and at most 24 hours")
    stamp = lambda v: v.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")  # noqa: E731
    return {FROM: stamp(now), UNTIL: stamp(now + dt.timedelta(hours=hours))}


def owned_pods(workload, replicasets, pods):
    uid = workload["metadata"]["uid"]
    owners = {uid}
    for rs in replicasets:
        if any(
            o.get("controller") and o["uid"] == uid
            for o in rs["metadata"].get("ownerReferences", [])
        ):
            owners.add(rs["metadata"]["uid"])
    return [
        p
        for p in pods
        if any(
            o.get("controller") and o["uid"] in owners
            for o in p["metadata"].get("ownerReferences", [])
        )
    ]


def annotation_patch(obj, desired):
    meta = obj["metadata"]
    existing = meta.get("annotations", {})
    changed = any(existing.get(k) != desired.get(k) for k in [FROM, UNTIL])
    if not changed:
        return []
    ops = [{"op": "test", "path": "/metadata/resourceVersion", "value": meta["resourceVersion"]}]
    if "annotations" not in meta:
        ops.append({"op": "add", "path": "/metadata/annotations", "value": {}})
    for k in [FROM, UNTIL]:
        path = "/metadata/annotations/" + k.replace("~", "~0").replace("/", "~1")
        if k in desired:
            ops.append({"op": "add", "path": path, "value": desired[k]})
        elif k in existing:
            ops.append({"op": "remove", "path": path})
    return ops


class Kube:
    def __init__(self, namespace, in_cluster=False):
        self.namespace, self.in_cluster = namespace, in_cluster

    def request(self, resource, name="", patch=None):
        if not self.in_cluster:
            args = ["kubectl", "-n", self.namespace]
            if patch is None:
                args += ["get", resource] + ([name] if name else []) + ["-o", "json"]
            else:
                args += [
                    "patch",
                    resource,
                    name,
                    "--type=json",
                    "--patch-file=/dev/stdin",
                    "-o",
                    "json",
                ]
            return json.loads(
                subprocess.run(
                    args,
                    input=json.dumps(patch) if patch else None,
                    text=True,
                    capture_output=True,
                    check=True,
                ).stdout
            )
        base = "/api/v1" if resource == "pods" else "/apis/apps/v1"
        path = f"{base}/namespaces/{self.namespace}/{resource}" + (f"/{name}" if name else "")
        directory = "/var/run/secrets/kubernetes.io/serviceaccount/"
        with open(directory + "token") as f:
            token = f.read()
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        request = urllib.request.Request(
            f"https://{host}:{port}{path}",
            data=json.dumps(patch).encode() if patch is not None else None,
            method="PATCH" if patch is not None else "GET",
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json-patch+json",
            },
        )
        with urllib.request.urlopen(
            request, timeout=20, context=ssl.create_default_context(cafile=directory + "ca.crt")
        ) as r:
            return json.load(r)

    def patch(self, resource, obj, desired):
        ops = annotation_patch(obj, desired)
        if ops:
            self.request(resource, obj["metadata"]["name"], ops)


def reconcile(kube, workloads, now):
    pods = kube.request("pods")["items"]
    replicasets = kube.request("replicasets")["items"]
    desired_by_uid = {}
    for workload in workloads:
        annotations = workload["metadata"].get("annotations", {})
        desired = {k: annotations[k] for k in [FROM, UNTIL]} if active(annotations, now) else {}
        for pod in owned_pods(workload, replicasets, pods):
            desired_by_uid[pod["metadata"]["uid"]] = desired
    for pod in pods:
        if pod["metadata"]["uid"] in desired_by_uid:
            kube.patch("pods", pod, desired_by_uid[pod["metadata"]["uid"]])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["grant", "revoke", "reconcile"])
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--kind", choices=RESOURCES, default="deployment")
    parser.add_argument("--name")
    parser.add_argument("--hours", type=float, default=8)
    parser.add_argument("--in-cluster", action="store_true")
    args = parser.parse_args()
    now = dt.datetime.now(UTC)
    kube = Kube(args.namespace, args.in_cluster)
    if args.action != "reconcile":
        if not args.name:
            parser.error("--name is required for grant/revoke")
        desired = grant(args.hours, now) if args.action == "grant" else {}
        resource = RESOURCES[args.kind]
        obj = kube.request(resource, args.name)
        kube.patch(resource, obj, desired)
    workloads = [w for kind in RESOURCES.values() for w in kube.request(kind)["items"]]
    reconcile(kube, workloads, now)
    print(
        f"{args.action} complete in namespace {args.namespace}; "
        "collector expiry remains authoritative"
    )


if __name__ == "__main__":
    main()
