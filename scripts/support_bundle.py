#!/usr/bin/env python3
"""Read-only support inventory; exclude secrets, logs, events, annotations and pod specs."""

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path


def summarize(kind, objects):
    result = []
    for obj in objects:
        meta, status = obj.get("metadata", {}), obj.get("status", {})
        item = {
            "kind": obj.get("kind", kind),
            "name": meta.get("name"),
            "namespace": meta.get("namespace"),
        }
        # Explicit projection, not a denylist: no environment, error messages or last-applied data.
        for key in [
            "phase",
            "replicas",
            "readyReplicas",
            "availableReplicas",
            "desiredNumberScheduled",
            "numberReady",
        ]:
            if key in status:
                item[key] = status[key]
        item["conditions"] = [
            {k: c[k] for k in ["type", "status", "reason", "lastTransitionTime"] if k in c}
            for c in status.get("conditions", [])
        ]
        item["containers"] = [
            {"name": c.get("name"), "ready": c.get("ready"), "restartCount": c.get("restartCount")}
            for c in status.get("containerStatuses", [])
        ]
        result.append(item)
    return result


def collect(context, namespace, directory, run=subprocess.run):
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    results = {}
    for kind in [
        "pods",
        "deployments",
        "statefulsets",
        "daemonsets",
        "persistentvolumeclaims",
        "rollouts",
        "scaledobjects",
    ]:
        command = [
            "kubectl",
            "--context",
            context,
            "-n",
            namespace,
            "get",
            kind,
            "-o",
            "json",
            "--request-timeout=15s",
        ]
        try:
            response = run(command, check=True, capture_output=True, text=True, timeout=20)
            content = {
                "status": "read",
                "items": summarize(kind, json.loads(response.stdout)["items"]),
            }
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            content = {"status": "unavailable"}
        path = directory / (kind + ".json")
        with path.open("x") as f:
            os.chmod(path, 0o600)
            f.write(json.dumps(content, indent=2) + "\n")
        results[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    (directory / "manifest.json").write_text(json.dumps({"files": results}, indent=2) + "\n")
    os.chmod(directory / "manifest.json", 0o600)
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", default="observability")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    collect(args.context, args.namespace, args.output)
    print(
        "Local support inventory written. "
        "Review names and topology before sharing; nothing uploaded."
    )
