#!/usr/bin/env python3
"""Owner-scoped debug requests. The trusted workflow supplies GitHub identity."""

import argparse
import datetime as dt
import json
import re
from pathlib import Path

try:
    from .debug_grants import RESOURCES, UTC, Kube, grant, reconcile
except ImportError:
    from debug_grants import RESOURCES, UTC, Kube, grant, reconcile

ROOT = Path(__file__).resolve().parents[1]


def authorize(policy, services, actor, region, cluster, namespace, service, hours):
    grant(
        hours, dt.datetime.now(UTC)
    )  # Reject nonfinite and out-of-range durations before remote access.
    if set(policy) != {"version", "owners"} or policy["version"] != 1:
        raise ValueError("Unsupported debug access policy")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}(?:\[bot\])?", actor):
        raise ValueError("Invalid actor")
    found = [s for s in services if s["name"] == service and s["namespace"] == namespace]
    if len(found) != 1:
        raise ValueError("Register a unique namespace/service in the catalog first")
    wanted = {"actor": actor, "region": region, "cluster": cluster, "namespace": namespace}
    bindings = policy["owners"].get(found[0]["owner"], [])
    if not isinstance(bindings, list) or not any(b == wanted for b in bindings):
        raise ValueError("Actor is not authorized for this owner, region, cluster and namespace")
    return found[0]


def apply(kube, kind, workload, service, action, hours, now):
    obj = kube.request(RESOURCES[kind], workload)
    if (
        obj.get("spec", {})
        .get("template", {})
        .get("metadata", {})
        .get("labels", {})
        .get("observability/service")
        != service
    ):
        raise ValueError("Workload service label differs from the authorized catalog entry")
    desired = grant(hours, now) if action == "grant" else {}
    kube.patch(RESOURCES[kind], obj, desired)
    obj.setdefault("metadata", {}).setdefault("annotations", {}).update(desired)
    if action == "revoke":
        for key in grant(1, now):
            obj["metadata"]["annotations"].pop(key, None)
    reconcile(kube, [obj], now)  # Do not reconcile or mutate another service's workloads.
    return desired


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ["actor", "region", "cluster", "namespace", "service", "workload"]:
        parser.add_argument("--" + key, required=True)
    parser.add_argument("--kind", choices=RESOURCES, default="deployment")
    parser.add_argument("--action", choices=["grant", "revoke"], required=True)
    parser.add_argument("--hours", type=float, default=8)
    parser.add_argument("--check-policy-only", action="store_true")
    args = parser.parse_args()
    policy = json.loads((ROOT / "operations/debug-access.json").read_text())
    catalog = json.loads((ROOT / "catalog/index.json").read_text())["services"]
    service = authorize(
        policy,
        catalog,
        args.actor,
        args.region,
        args.cluster,
        args.namespace,
        args.service,
        args.hours,
    )
    if args.check_policy_only:
        print("Debug ownership policy permits this request; no remote calls made.")
        return
    now = dt.datetime.now(UTC)
    desired = apply(
        Kube(args.namespace), args.kind, args.workload, args.service, args.action, args.hours, now
    )
    print(
        json.dumps(
            {
                "event": "debug_request_applied",
                "actor": args.actor,
                "owner": service["owner"],
                "region": args.region,
                "cluster": args.cluster,
                "namespace": args.namespace,
                "service": args.service,
                "workload": args.workload,
                "kind": args.kind,
                "action": args.action,
                "at": now.isoformat(),
                "grant": desired,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
