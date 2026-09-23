#!/usr/bin/env python3
"""Offline AWS qualification plan and evidence ledger. Never executes cloud commands."""

import argparse
import hashlib
import json
import math
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jsonschema
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schemas/qualification.schema.json"
CATALOG = ROOT / "operations/qualification-gates.yaml"
MAX_BYTES = 1024 * 1024


def read(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError("Input must be a regular local file no larger than 1 MiB")
    result = yaml.safe_load(path.read_text())
    # JSON round trip rejects YAML dates/objects and non-finite numeric values.
    return json.loads(json.dumps(result, allow_nan=False))


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def validate(value, definition=None):
    schema = json.loads(SCHEMA.read_text())
    if definition:
        schema = {"$ref": f"#/$defs/{definition}", "$defs": schema["$defs"]}
    errors = list(jsonschema.Draft202012Validator(schema).iter_errors(value))
    if errors:
        # Do not echo rejected input, which may contain mistakenly pasted credentials.
        raise ValueError("Invalid qualification document; inspect its schema and field types")
    return value


def configuration(path):
    config = validate(read(path))
    source, target = config["environment"], config["recovery"]["destination"]
    if all(source[key] == target[key] for key in ["accountId", "region", "cluster"]):
        raise ValueError("Restore destination must be a different isolated cluster")
    cross = config["recovery"]["crossRegion"]
    if source["region"] != target["region"] and not cross["enabled"]:
        raise ValueError("A destination in another region requires explicit cross-region opt-in")
    if cross["enabled"] and source["region"] == target["region"]:
        raise ValueError("Cross-region recovery requires a destination in a different region")
    return config


def plan(config):
    catalog = read(CATALOG)
    seen, gates = set(), []
    for gate in catalog["gates"]:
        name = gate["id"]
        if name in seen or any(dep not in seen for dep in gate["dependsOn"]):
            raise ValueError("Gate catalog must have unique IDs and ordered dependencies")
        seen.add(name)
        feature = gate.get("feature")
        enabled = (
            config["recovery"]["crossRegion"]["enabled"]
            if feature == "crossRegion"
            else config["features"].get(feature, True)
        )
        gates.append({**gate, "enabled": enabled})
    return {
        "version": 1,
        "runId": config["runId"],
        "configDigest": digest(config),
        "sourceRevision": config["sourceRevision"],
        "environment": config["environment"],
        "execution": "deferred-to-operator",
        "gates": gates,
    }


def now_string():
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def timestamp(value):
    if not value.endswith("Z"):
        raise ValueError("Evidence times must be UTC timestamps ending in Z")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Malformed evidence timestamp") from None
    return result


def write_new(path, content):
    # Exclusive creation makes accidental retries and concurrent overwrite fail closed.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(json.dumps(content, indent=2, allow_nan=False) + "\n")


def initialize(config, directory):
    directory = Path(directory)
    if directory.is_symlink():
        raise ValueError("A run directory cannot be a symlink")
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    planned = plan(config)
    write_new(directory / "config.json", config)
    write_new(directory / "plan.json", planned)
    write_new(
        directory / "manifest.json",
        {
            "version": 1,
            "runId": config["runId"],
            "configDigest": digest(config),
            "planDigest": digest(planned),
            "createdAt": now_string(),
        },
    )
    (directory / "receipts").mkdir(mode=0o700)
    return planned


def load_run(directory):
    directory = Path(directory)
    if directory.is_symlink() or (directory / "receipts").is_symlink():
        raise ValueError("Run paths cannot be symlinks")
    config = configuration(directory / "config.json")
    planned, manifest = read(directory / "plan.json"), read(directory / "manifest.json")
    validate(manifest, "manifest")
    if (
        manifest["runId"] != config["runId"]
        or manifest["configDigest"] != digest(config)
        or manifest["planDigest"] != digest(planned)
        or planned != plan(config)
    ):
        raise ValueError("Run inputs or gate catalog changed; start a new qualification run")
    timestamp(manifest["createdAt"])
    return config, planned, manifest


def prerequisites(config, gate):
    if gate in {"backup_restore", "upgrade_rollforward", "regional_recovery", "cross_region"}:
        if any(config["recovery"][key] is None for key in ["rpoMinutes", "rtoMinutes"]):
            raise ValueError("Set reviewed recovery RPO/RTO before claiming recovery success")
        if config["recovery"]["backupMode"] == "external" and not config["recovery"]["providerRef"]:
            raise ValueError("External backup mode requires a provider runbook reference")
    if gate == "cross_region":
        cross = config["recovery"]["crossRegion"]
        if any(cross[key] is None for key in ["rpoMinutes", "rtoMinutes", "costAssessmentRef"]):
            raise ValueError("Cross-region evidence requires RPO/RTO and a cost assessment")


def current_pass(gate_id, gates, previous):
    receipt = previous.get(gate_id)
    if not receipt or receipt["status"] != "passed":
        return False
    for dependency in gates[gate_id]["dependsOn"]:
        if not current_pass(dependency, gates, previous):
            return False
        if timestamp(previous[dependency]["completedAt"]) > timestamp(receipt["startedAt"]):
            return False
    return True


def validate_receipt(receipt, config, planned, manifest, previous, now=None):
    validate(receipt, "receipt")
    if receipt["runId"] != config["runId"] or receipt["configDigest"] != digest(config):
        raise ValueError("Evidence belongs to a different run or environment/configuration")
    gates = {gate["id"]: gate for gate in planned["gates"]}
    gate = gates.get(receipt["gate"])
    if not gate or not gate["enabled"]:
        raise ValueError("Cannot record evidence for an unknown or disabled gate")
    start, end = timestamp(receipt["startedAt"]), timestamp(receipt["completedAt"])
    if start < timestamp(manifest["createdAt"]) or end < start:
        raise ValueError("Evidence must start after run creation and end after its start")
    if end > (now or datetime.now(UTC)) + timedelta(seconds=30):
        raise ValueError("Future evidence is not accepted")
    prior = previous.get(gate["id"])
    if prior and start < timestamp(prior["completedAt"]):
        raise ValueError("Retests must follow the prior attempt; evidence is append-only")
    if receipt["status"] == "passed":
        prerequisites(config, gate["id"])
        if gate["id"] == "environment_identity":
            if config["sourceRevision"] == "0" * 40:
                raise ValueError(
                    "Replace the example revision before recording environment evidence"
                )
            artifacts = config["deploymentArtifacts"]
            required = ["platformContractSHA256", "renderedValuesSHA256"]
            if config["environment"]["mode"] != "software-only":
                required.append("terraformPlanSHA256")
            if any(artifacts[key] in {None, "0" * 64} for key in required):
                raise ValueError("Record non-placeholder hashes of applicable deployment artifacts")
        for key in gate.get("measurements", []):
            number = receipt["measurements"].get(key)
            if (
                isinstance(number, bool)
                or not isinstance(number, (float, int))
                or not math.isfinite(number)
                or number < 0
            ):
                raise ValueError("Passed gate requires finite, nonnegative recovery measurements")
        if gate.get("measurements"):
            targets = (
                config["recovery"]["crossRegion"]
                if gate["id"] == "cross_region"
                else config["recovery"]
            )
            for measurement, limit in [
                ("data_gap_minutes", "rpoMinutes"),
                ("recovery_minutes", "rtoMinutes"),
            ]:
                if targets[limit] is None or receipt["measurements"][measurement] > targets[limit]:
                    raise ValueError("Measured recovery exceeds the configured RPO/RTO")
        if not receipt["evidenceRefs"]:
            raise ValueError("Passed gates require a reviewed evidence reference")
        if any(receipt["checks"].get(key) is not True for key in gate["checks"]):
            raise ValueError("Passed gate lacks required positive assertions")
        if any(value is False for value in receipt["checks"].values()):
            raise ValueError("A failed assertion cannot be reported as passed")
        for dependency in gate["dependsOn"]:
            dep = previous.get(dependency)
            if (
                not current_pass(dependency, gates, previous)
                or timestamp(dep["completedAt"]) > start
            ):
                raise ValueError("Dependencies must have current passes before this gate starts")
    return receipt


def receipts(directory, config, planned, manifest):
    previous, records = {}, []
    paths = sorted((Path(directory) / "receipts").iterdir())
    for index, path in enumerate(paths, 1):
        if path.name != f"{index:06}.json":
            raise ValueError("Evidence sequence has unexpected files or gaps")
        record = read(path)
        validate(record, "record")
        receipt = record["receipt"]
        if record["sha256"] != digest(receipt):
            raise ValueError("Evidence receipt digest mismatch")
        validate_receipt(receipt, config, planned, manifest, previous)
        previous[receipt["gate"]] = receipt
        records.append(record)
    return previous, records


def record(directory, receipt):
    config, planned, manifest = load_run(directory)
    previous, records = receipts(directory, config, planned, manifest)
    validate_receipt(receipt, config, planned, manifest, previous)
    write_new(
        Path(directory) / "receipts" / f"{len(records) + 1:06}.json",
        {"receipt": receipt, "sha256": digest(receipt)},
    )


def receipt_template(directory, gate_id):
    config, planned, _ = load_run(directory)
    gate = next((g for g in planned["gates"] if g["id"] == gate_id and g["enabled"]), None)
    if not gate:
        raise ValueError("Choose an enabled gate from this run's plan")
    # Deliberately not a recordable receipt: unexecuted tests must never become passes.
    return {
        "version": 1,
        "runId": config["runId"],
        "configDigest": digest(config),
        "gate": gate_id,
        "status": "pending",
        "startedAt": None,
        "completedAt": None,
        "operator": "REPLACE_WITH_REVIEWER",
        "reason": "Not executed",
        "checks": dict.fromkeys(gate["checks"], False),
        "measurements": dict.fromkeys(gate.get("measurements", []), None),
        "evidenceRefs": [],
    }


def report(directory):
    config, planned, manifest = load_run(directory)
    latest, _ = receipts(directory, config, planned, manifest)
    gates = []
    effective = {}
    for gate in planned["gates"]:
        receipt = latest.get(gate["id"])
        status = receipt["status"] if receipt else "pending"
        reason = "Operator evidence required" if not receipt else receipt["reason"]
        if not gate["enabled"]:
            status, reason = "skipped", "Feature disabled in this run's configuration"
        elif status == "passed":
            # A later failed/retested prerequisite invalidates its earlier dependants.
            for dep in gate["dependsOn"]:
                dependency = latest.get(dep)
                if effective.get(dep) != "passed" or timestamp(
                    dependency["completedAt"]
                ) > timestamp(receipt["startedAt"]):
                    status, reason = "pending", "Prerequisite changed; repeat this gate"
                    break
        effective[gate["id"]] = status
        gates.append(
            {"id": gate["id"], "required": gate["enabled"], "status": status, "reason": reason}
        )
    return {
        "runId": config["runId"],
        "configDigest": digest(config),
        "qualified": all(g["status"] == "passed" for g in gates if g["required"]),
        "evidenceType": "operator-attested; no independent cloud verification",
        "gates": gates,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=["plan", "init", "receipt-template", "record", "check"],
        nargs="?",
        default="plan",
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--gate", help="Enabled gate for receipt-template")
    parser.add_argument(
        "--check-only", action="store_true", help="Validate/plan only; never writes"
    )
    args = parser.parse_args(argv)
    try:
        if args.check_only or args.action in {"plan", "init"}:
            if not args.config:
                raise ValueError("--config is required")
            config = configuration(args.config)
            if args.check_only or args.action == "plan":
                result = plan(config)
            else:
                if not args.run_dir:
                    raise ValueError("--run-dir is required")
                result = initialize(config, args.run_dir)
        else:
            if not args.run_dir:
                raise ValueError("--run-dir is required")
            if args.action == "record":
                if not args.receipt:
                    raise ValueError("--receipt is required")
                record(args.run_dir, read(args.receipt))
            result = (
                receipt_template(args.run_dir, args.gate)
                if args.action == "receipt-template"
                else report(args.run_dir)
            )
        print(json.dumps(result, indent=2))
        return (
            2 if args.action == "check" and not args.check_only and not result["qualified"] else 0
        )
    except (ValueError, OSError, yaml.YAMLError) as error:
        # Paths can be sensitive; do not emit OSError path contents or raw parser input.
        message = (
            str(error) if isinstance(error, ValueError) else "Cannot read/write local run files"
        )
        parser.exit(1, f"Qualification rejected: {message}\n")


if __name__ == "__main__":
    raise SystemExit(main())
