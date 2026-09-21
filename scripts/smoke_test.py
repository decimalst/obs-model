#!/usr/bin/env python3
"""Live deployment acceptance: rollouts, quorum, and service readiness (no deletion)."""

import json
import subprocess


def kubectl(*args):
    return subprocess.run(
        ["kubectl", "-n", "observability", *args], check=True, text=True, capture_output=True
    ).stdout


def main():
    for name in ["log-quota", "vector-gateway", "otel-metrics", "otel-router"]:
        obj = json.loads(kubectl("get", "rollout", name, "-o", "json"))
        desired = obj["spec"]["replicas"]
        if obj.get("status", {}).get("readyReplicas", 0) < desired:
            raise RuntimeError(
                f"{name} is not ready; inspect its rollout/analysis before promotion"
            )
    for name in ["keeper", "otel-traces"]:
        kubectl("rollout", "status", f"statefulset/{name}", "--timeout=300s")
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
