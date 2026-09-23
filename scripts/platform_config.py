"""Nonsecret deployment contract shared by both infrastructure paths."""

import ipaddress
import json
import re
from pathlib import Path

import yaml

try:
    from .tuning import validate_tuning
except ImportError:
    from tuning import validate_tuning

OPERATORS = (
    "cert-manager",
    "argo-rollouts",
    "strimzi-kafka-operator",
    "opentelemetry-operator",
    "altinity-clickhouse-operator",
    "cluster-autoscaler",
    "metrics-server",
    "keda",
)
BUCKETS = ("mimir-blocks", "mimir-ruler", "mimir-alertmanager", "tempo")


def load_platform(path):
    document = yaml.safe_load(Path(path).read_text())
    if not isinstance(document, dict):
        raise ValueError("Configuration must be a mapping")
    return validate_platform(document["platform"]["value"] if "platform" in document else document)


def validate_platform(p):
    p = dict(p)
    allowed = {
        "tuning",
        "profiling",
        "network_policy_provider",
        "region",
        "cluster_name",
        "cluster_arn",
        "vpc_cidr",
        "availability_zones",
        "buckets",
        "quota_redis_host",
        "grafana_db_host",
        "grafana_db_secret_arn",
        "quota_secret_arn",
        "role_arns",
        "operators",
        "operator_namespaces",
        "secret_source",
        "storage_class",
        "create_storage_class",
        "required_role_policies",
        "grafana_db_name",
        "grafana_db_user",
    }
    if set(p) - allowed:
        raise ValueError(
            "Unknown configuration keys; keep secret values out of this file: "
            + ", ".join(sorted(set(p) - allowed))
        )
    for key in ("region", "cluster_name", "vpc_cidr", "quota_redis_host"):
        if not isinstance(p.get(key), str) or not p[key] or "REPLACE" in p[key]:
            raise ValueError(f"Set {key} in the nonsecret platform configuration")
    region = p["region"]
    if not re.fullmatch(r"[a-z]{2}-[a-z]+-\d", region):
        raise ValueError("Use a supported commercial AWS region")
    ipaddress.IPv4Network(p["vpc_cidr"])
    zones = p.get("availability_zones", [])
    if (
        len(zones) != 3
        or len(set(zones)) != 3
        or not all(re.fullmatch(re.escape(region) + "[a-z]", z) for z in zones)
    ):
        raise ValueError(
            "Supply availability_zones: three distinct standard AZs in this region; "
            "no a/b/c guessing"
        )
    p.setdefault("network_policy_provider", "aws")
    if p["network_policy_provider"] not in ("aws", "cilium"):
        raise ValueError("network_policy_provider must be aws or cilium")
    profiling = p.get("profiling", {"enabled": False})
    if (
        not isinstance(profiling, dict)
        or set(profiling) != {"enabled"}
        or type(profiling["enabled"]) is not bool
    ):
        raise ValueError(
            "profiling must contain only a boolean enabled; use tuning for sample rates"
        )
    p["profiling"] = profiling
    if (
        p.get("tuning", {}).get("hubble", {}).get("enabled", False)
        and p["network_policy_provider"] != "cilium"
    ):
        raise ValueError("Hubble requires an explicitly reused Cilium installation")
    for key in BUCKETS + (("pyroscope",) if profiling["enabled"] else ()):
        name = p.get("buckets", {}).get(key, "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", name):
            raise ValueError(f"Set the existing regional bucket for {key}")
    if any(k not in OPERATORS or type(v) is not bool for k, v in p.get("operators", {}).items()):
        raise ValueError("operators maps known chart names to true (install) or false (reuse)")
    p.setdefault("operators", {})
    p.setdefault("secret_source", "aws")
    if p["secret_source"] not in ("aws", "existing"):
        raise ValueError("secret_source must be aws or existing")
    if p["secret_source"] == "aws":
        if not p.get("grafana_db_host"):
            raise ValueError("grafana_db_host is required for AWS secret seeding")
        for key in ("grafana_db_secret_arn", "quota_secret_arn"):
            if not re.fullmatch(
                r"arn:aws:secretsmanager:" + re.escape(region) + r":\d{12}:secret:.+",
                p.get(key, ""),
            ):
                raise ValueError(f"{key} must be a Secrets Manager ARN in {region}")
    for key in ("mimir", "tempo") + (("pyroscope",) if profiling["enabled"] else ()):
        if not re.fullmatch(r"arn:aws:iam::\d{12}:role/.+", p.get("role_arns", {}).get(key, "")):
            raise ValueError(f"Provide the existing or newly created {key} Pod Identity role ARN")
    p.setdefault("storage_class", "gp3-encrypted")
    p.setdefault("create_storage_class", True)
    if type(p["create_storage_class"]) is not bool or not re.fullmatch(
        r"[a-z0-9][a-z0-9.-]*", p["storage_class"]
    ):
        raise ValueError("Set a valid storage_class and boolean create_storage_class")
    if "tuning" in p:
        p["tuning"] = validate_tuning(p["tuning"])
    return p


def write_platform(p, path):
    Path(path).write_text(json.dumps({"platform": {"value": p}}, indent=2) + "\n")
