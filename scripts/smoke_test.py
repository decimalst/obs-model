#!/usr/bin/env python3
"""Live deployment acceptance: rollouts, quorum, and service readiness (no deletion)."""

import json
import subprocess
import time


def kubectl(*args):
    return subprocess.run(
        ["kubectl", "-n", "observability", *args], check=True, text=True, capture_output=True
    ).stdout


class PendingTraceDrain(RuntimeError):
    """The desired sampler template has not reached all existing ordinals."""


def verify_on_delete_sampler(statefulset, pods):
    metadata, spec, status = (
        statefulset["metadata"],
        statefulset["spec"],
        statefulset.get("status", {}),
    )
    name, uid = metadata["name"], metadata.get("uid")
    desired = spec.get("replicas", 1)
    if spec.get("updateStrategy", {}).get("type") != "OnDelete":
        raise RuntimeError(f"{name} must preserve its OnDelete trace-drain strategy")
    if not uid or desired < 1 or status.get("observedGeneration", 0) < metadata["generation"]:
        raise RuntimeError(f"{name} has not observed its requested configuration")
    revision = status.get("updateRevision")
    if not revision:
        raise RuntimeError(f"{name} has no target revision yet")
    owned = [
        pod
        for pod in pods
        if any(
            ref.get("kind") == "StatefulSet"
            and ref.get("name") == name
            and ref.get("uid") == uid
            and ref.get("controller") is True
            for ref in pod.get("metadata", {}).get("ownerReferences", [])
        )
    ]
    if any(
        pod.get("metadata", {}).get("labels", {}).get("controller-revision-hash")
        not in (None, revision)
        for pod in owned
    ):
        raise PendingTraceDrain(
            f"{name}: pending trace drain; old sampler revisions remain. "
            "Follow docs/runbooks.md to drain and replace one ordinal at a time, then rerun."
        )
    start = spec.get("ordinals", {}).get("start", 0)
    expected = {f"{name}-{ordinal}" for ordinal in range(start, start + desired)}
    if len(owned) != desired or {pod["metadata"]["name"] for pod in owned} != expected:
        raise RuntimeError(f"{name} does not have every expected owned sampler ordinal")
    # currentRevision can remain old under OnDelete even after every pod updates;
    # compare each actual pod to updateRevision instead of requiring equality.
    for pod in owned:
        meta, pod_status = pod["metadata"], pod.get("status", {})
        if (
            meta.get("deletionTimestamp")
            or meta.get("labels", {}).get("controller-revision-hash") != revision
            or pod_status.get("phase") != "Running"
            or not any(
                c.get("type") == "Ready" and c.get("status") == "True"
                for c in pod_status.get("conditions", [])
            )
        ):
            raise RuntimeError(f"{name} has a sampler not Ready at the requested revision")
    if any(
        status.get(field, 0) != desired
        for field in ("replicas", "readyReplicas", "updatedReplicas")
    ):
        raise RuntimeError(f"{name} has not reported every desired sampler ready and updated")


def wait_for_sampler(timeout=300):
    deadline = time.monotonic() + timeout
    while True:
        statefulset = json.loads(kubectl("get", "statefulset", "otel-traces", "-o", "json"))
        pods = json.loads(kubectl("get", "pods", "-l", "app=otel-traces", "-o", "json"))["items"]
        try:
            verify_on_delete_sampler(statefulset, pods)
            return
        except PendingTraceDrain:
            raise
        except RuntimeError as error:
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Sampler readiness timed out: {error}") from None
            time.sleep(2)


def main():
    for name in ["log-quota", "vector-gateway", "otel-metrics", "otel-router"]:
        obj = json.loads(kubectl("get", "rollout", name, "-o", "json"))
        desired = obj["spec"]["replicas"]
        if obj.get("status", {}).get("readyReplicas", 0) < desired:
            raise RuntimeError(
                f"{name} is not ready; inspect its rollout/analysis before promotion"
            )
    kubectl("rollout", "status", "statefulset/keeper", "--timeout=300s")
    wait_for_sampler()
    pods = json.loads(kubectl("get", "pods", "-o", "json"))["items"]
    failed = [
        p["metadata"]["name"] for p in pods if p["status"].get("phase") in ["Failed", "Pending"]
    ]
    if failed:
        raise RuntimeError(f"Unhealthy platform pods: {failed}")
    print(
        "Readiness checks passed. "
        "Run the data-path and AZ-loss acceptance steps in docs/testing.md."
    )


if __name__ == "__main__":
    main()
