#!/usr/bin/env python3
"""Offline review of independent regional cells and their configured telemetry budgets."""

import argparse
import json
import re
from pathlib import Path

import jsonschema
import yaml

try:
    from .platform_config import load_platform
    from .tuning import DEFAULTS, merge
except ImportError:
    from platform_config import load_platform
    from tuning import DEFAULTS, merge

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "schemas/cells.schema.json").read_text())


def grafana_database_identity(platform, cell):
    declared = cell.get("grafanaDatabaseIdentity", "")
    if declared:
        host, database = declared.split("/", 1)
        declared = host.rstrip(".").lower() + "/" + database
    if platform["secret_source"] == "aws":
        # Secret seeding takes its host from this contract, not a cell label.
        actual = (
            platform["grafana_db_host"].rstrip(".").lower()
            + "/"
            + platform.get("grafana_db_name", "grafana")
        )
        if declared and declared != actual:
            raise ValueError("Grafana database identity contradicts the AWS-seeded host/database")
        return actual
    return declared


def plan(document, directory):
    jsonschema.Draft7Validator(SCHEMA).validate(document)
    if document["mode"] == "shared" and len(document["cells"]) != 1:
        raise ValueError("Shared mode describes exactly one cell")
    identities, assignments, resources, results = set(), set(), {}, []
    for cell in document["cells"]:
        if cell["id"] in identities:
            raise ValueError("Cell IDs must be unique")
        identities.add(cell["id"])
        p = load_platform(Path(directory) / cell["platform"])
        budget = merge(DEFAULTS, p.get("tuning", {}))
        if document["mode"] == "isolated-clusters":
            # These are declared coordinates, not proof of IAM or network separation.
            coordinates = {
                "cluster": p.get("cluster_arn", ""),
                "quota": p["quota_redis_host"],
                "grafana_database": grafana_database_identity(p, cell),
                **{"bucket/" + k: v for k, v in p["buckets"].items()},
                **{"role/" + k: v for k, v in p["role_arns"].items()},
            }
            if not coordinates["cluster"] or not coordinates["grafana_database"]:
                raise ValueError("Isolated cells require cluster ARN and Grafana database identity")
            if not re.fullmatch(
                rf"arn:aws:eks:{re.escape(p['region'])}:\d{{12}}:cluster/{re.escape(p['cluster_name'])}",
                coordinates["cluster"],
            ):
                raise ValueError("Cluster ARN must match the cell's region and cluster name")
            for kind, value in coordinates.items():
                # Detect sharing across backend types as well as across cells.
                family = kind.split("/")[0]
                key = (family, value)
                if key in resources:
                    raise ValueError(
                        f"Declared {kind} is shared by {resources[key]} and {cell['id']}"
                    )
                resources[key] = cell["id"]
        for service in cell["services"]:
            identity = (p["region"], service)
            if identity in assignments:
                raise ValueError("A service may target only one cell in each region")
            assignments.add(identity)
        limits = budget["mimir"]["mimir"]["structuredConfig"]["limits"]
        tempo = budget["tempo"]["overrides"]["defaults"]["ingestion"]
        results.append(
            {
                "id": cell["id"],
                "region": p["region"],
                "cluster": p["cluster_name"],
                "services": cell["services"],
                "status": "live-qualification-pending",
                "budgets": {
                    "logs_per_namespace_service": {
                        "events_per_second": budget["quota"]["eventsPerSecond"],
                        "bytes_per_second": budget["quota"]["bytesPerSecond"],
                    },
                    "metrics_platform_tenant": {
                        "samples_per_second": limits["ingestion_rate"],
                        "active_series": limits["max_global_series_per_user"],
                        "series_per_metric": limits["max_global_series_per_metric"],
                    },
                    "tempo_platform_tenant": {
                        "bytes_per_second": tempo["rate_limit_bytes"],
                        "burst_bytes": tempo["burst_size_bytes"],
                    },
                    "tail_sampling_per_pod": {
                        "spans_per_second": budget["collectors"]["traceSpansPerSecond"],
                        "baseline_percent": budget["collectors"]["traceSamplingPercent"],
                    },
                    "consumer_ceiling": min(
                        budget["writerAutoscaling"]["maxReplicas"],
                        budget["scaling"]["logPartitions"],
                    ),
                    "clickhouse_write_shards": budget["clickhouse"]["shards"],
                },
            }
        )
    return {
        "version": 1,
        "mode": document["mode"],
        "cells": results,
        "deployment_performed": False,
        "isolation_verified": False,
        "limits": [
            "Configured ceilings are not measured throughput or cost estimates.",
            "Shared-cell metrics, traces and profiles have no shared per-service fair scheduler.",
            "Separate clusters/backends reduce shared bottlenecks; verify workload identity, "
            "IAM, routing and network isolation.",
            "Raw telemetry remains regional; no cross-region replication is generated.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = plan(yaml.safe_load(args.config.read_text()), args.config.parent)
    with args.output.open("x") as f:
        f.write(json.dumps(result, indent=2) + "\n")
    print("Offline cell budget review written; AWS validation remains pending.")


if __name__ == "__main__":
    main()
