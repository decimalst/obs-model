"""Validated, nonsecret pipeline overrides; also consumed by chart schema tests."""

import copy
import json
from pathlib import Path

import jsonschema
import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = yaml.safe_load((ROOT / "charts/platform/values.yaml").read_text())
SCHEMA = json.loads((ROOT / "charts/platform/values.schema.json").read_text())
# Keep identity, topology, endpoint, image, and credential overrides out of tuning.
ALLOWED = {
    "quota": {k: None for k in DEFAULTS["quota"] if k.endswith(("PerSecond", "Burst"))},
    "logs": {
        k: None
        for k in [
            "agentEventsPerSecond",
            "retentionDays",
            "kafkaRetentionHours",
            "agentBufferBytes",
            "gatewayBufferEvents",
            "writerBufferEvents",
            "kafkaStorage",
            "clickhouseStorage",
        ]
    },
    "collectors": {k: None for k in DEFAULTS["collectors"] if k != "replicas"},
    "autoscaling": {k: None for k in DEFAULTS["autoscaling"]},
    "writerAutoscaling": {k: None for k in DEFAULTS["writerAutoscaling"]},
    "scaling": {k: None for k in DEFAULTS["scaling"]},
    "clickhouse": {"shards": None, "insertQuorumTimeoutMs": None, "pauseWrites": None},
    "resources": {
        role: {kind: {key: None for key in resource[kind]} for kind in resource}
        for role, resource in DEFAULTS["resources"].items()
    },
    "mimir": {
        "ingester": {"replicas": None},
        "mimir": {
            "structuredConfig": {
                "limits": {
                    k: None for k in DEFAULTS["mimir"]["mimir"]["structuredConfig"]["limits"]
                }
            }
        },
    },
    "tempo": {
        "overrides": {
            "defaults": {
                "ingestion": {"rate_limit_bytes": None, "burst_size_bytes": None},
                "global": {"max_bytes_per_trace": None},
            }
        },
        "compactor": {"config": {"compaction": {"block_retention": None}}},
    },
}


def merge(base, override):
    result = copy.deepcopy(base)
    for key, value in override.items():
        result[key] = (
            merge(result.get(key, {}), value) if isinstance(value, dict) else copy.deepcopy(value)
        )
    return result


def _keys(value, allowed, path="tuning"):
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be a mapping")
    for key, child in value.items():
        if key not in allowed:
            raise ValueError(f"Unsupported tuning field: {path}.{key}")
        if allowed[key] is not None:
            _keys(child, allowed[key], f"{path}.{key}")


def validate_tuning(tuning):
    _keys(tuning, ALLOWED)
    full = merge(DEFAULTS, tuning)
    errors = list(jsonschema.Draft7Validator(SCHEMA).iter_errors(full))
    if errors:
        error = errors[0]
        raise ValueError(
            f"Invalid tuning {'.'.join(map(str, error.absolute_path))}: {error.message}"
        )
    c, q = full["collectors"], full["quota"]
    scaling = full["scaling"]
    if (
        full["autoscaling"]["enabled"]
        and scaling["writerReplicas"] > full["writerAutoscaling"]["maxReplicas"]
    ):
        raise ValueError("writerReplicas cannot exceed the HPA maximum")
    if scaling["writerReplicas"] > scaling["logPartitions"]:
        raise ValueError("writerReplicas cannot exceed logPartitions")
    if full["mimir"]["ingester"]["replicas"] < 3 * scaling["metricsPartitions"]:
        raise ValueError("Mimir needs 3 ingester replicas per metrics partition")
    if c["arrowQueueItems"] < c["batchMaxSize"]:
        raise ValueError("arrowQueueItems must hold at least batchMaxSize items")
    if c["batchSize"] > c["batchMaxSize"]:
        raise ValueError("batchSize must not exceed batchMaxSize")
    if int(c["scrapeTimeout"][:-1]) > int(c["scrapeInterval"][:-1]):
        raise ValueError("scrapeTimeout must not exceed scrapeInterval")
    if (
        sum(
            c[k] for k in ["traceErrorAllocation", "traceSlowAllocation", "traceBaselineAllocation"]
        )
        != 100
    ):
        raise ValueError("trace allocations must sum to 100")
    for debug, total in [
        ("debugEventsPerSecond", "eventsPerSecond"),
        ("debugEventBurst", "eventBurst"),
        ("debugBytesPerSecond", "bytesPerSecond"),
        ("debugByteBurst", "byteBurst"),
    ]:
        if q[debug] > q[total]:
            raise ValueError("debug quota must fit within the total quota")
    for role, resource in full["resources"].items():
        for dimension in ["cpu", "memory"]:

            def amount(value, dimension=dimension):
                if dimension == "cpu":
                    return int(value[:-1]) if value.endswith("m") else int(value) * 1000
                return int(value[:-2]) * (1024 if value.endswith("Gi") else 1)

            if amount(resource["requests"][dimension]) > amount(resource["limits"][dimension]):
                raise ValueError(f"resources.{role}: {dimension} request exceeds limit")
    return copy.deepcopy(tuning)


def load_tuning(path):
    return validate_tuning(yaml.safe_load(Path(path).read_text()))
