#!/usr/bin/env python3
"""Generate region coordinates from nonsensitive Terraform outputs."""

import argparse
import pathlib
import re

import yaml

try:
    from .platform_config import OPERATORS, load_platform, validate_platform
    from .tuning import load_tuning, merge, validate_tuning
except ImportError:
    from platform_config import OPERATORS, load_platform, validate_platform
    from tuning import load_tuning, merge, validate_tuning


def values(platform, quota_image):
    platform = validate_platform(platform)
    region = platform["region"]
    if not re.fullmatch(r".+@sha256:[a-f0-9]{64}", quota_image):
        raise ValueError("Use the published log-quota image digest, not a mutable tag")
    zones = [
        {"name": name, "nodeSelector": {"topology.kubernetes.io/zone": az}}
        for name, az in zip("abc", platform["availability_zones"], strict=True)
    ]
    buckets = platform["buckets"]
    generated = {
        "region": region,
        "clusterName": platform["cluster_name"],
        "storageClass": platform.get("storage_class", "gp3-encrypted"),
        "createStorageClass": platform.get("create_storage_class", True),
        "images": {"quota": quota_image},
        "quota": {"redisAddress": platform["quota_redis_host"] + ":6379"},
        "networkPolicy": {
            "vpcCIDR": platform["vpc_cidr"],
            "operatorNamespaces": platform.get("operator_namespaces", ["observability-system"]),
        },
        "grafana": {
            "grafana.ini": {
                "database": {
                    "name": platform.get("grafana_db_name", "grafana"),
                    "user": platform.get("grafana_db_user", "grafana"),
                }
            }
        },
        "mimir": {
            "mimir": {
                "structuredConfig": {
                    "common": {
                        "storage": {
                            "s3": {"endpoint": f"s3.{region}.amazonaws.com", "region": region}
                        }
                    },
                    "blocks_storage": {"s3": {"bucket_name": buckets["mimir-blocks"]}},
                    "ruler_storage": {"s3": {"bucket_name": buckets["mimir-ruler"]}},
                    "alertmanager_storage": {"s3": {"bucket_name": buckets["mimir-alertmanager"]}},
                }
            },
            **{
                key: {
                    **({"zoneAwareReplication": {"zones": zones}} if key != "compactor" else {}),
                    "persistentVolume": {
                        "storageClass": platform.get("storage_class", "gp3-encrypted")
                    },
                }
                for key in ["ingester", "store_gateway", "alertmanager", "compactor"]
            },
        },
        "tempo": {
            "storage": {
                "trace": {
                    "s3": {
                        "bucket": buckets["tempo"],
                        "endpoint": f"s3.{region}.amazonaws.com",
                        "region": region,
                    }
                }
            },
            "ingester": {
                "zoneAwareReplication": {"zones": zones},
                "persistence": {"storageClass": platform.get("storage_class", "gp3-encrypted")},
            },
        },
    }

    if platform.get("profiling", {}).get("enabled", False):
        generated["profiling"] = {"enabled": True}
        generated["pyroscope"] = {
            "pyroscope": {
                "structuredConfig": {
                    "storage": {
                        "s3": {
                            "bucket_name": buckets["pyroscope"],
                            "endpoint": f"s3.{region}.amazonaws.com",
                            "region": region,
                        }
                    }
                },
                "components": {
                    name: {
                        "persistence": {
                            "storageClassName": platform.get("storage_class", "gp3-encrypted")
                        }
                    }
                    for name in ("metastore", "segment-writer")
                },
            }
        }
    return merge(generated, validate_tuning(platform.get("tuning", {})))


def operator_values(platform):
    return {
        "components": {name: platform.get("operators", {}).get(name, True) for name in OPERATORS},
        "cluster-autoscaler": {
            "awsRegion": platform["region"],
            "autoDiscovery": {"clusterName": platform["cluster_name"]},
        },
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--terraform-output", type=pathlib.Path, required=True)
    p.add_argument("--quota-image", required=True)
    p.add_argument("--tuning", type=pathlib.Path)
    p.add_argument("--output", type=pathlib.Path, required=True)
    args = p.parse_args()
    platform = load_platform(args.terraform_output)
    if args.tuning:
        platform["tuning"] = merge(platform.get("tuning", {}), load_tuning(args.tuning))
    result = values(platform, args.quota_image)
    args.output.parent.mkdir(exist_ok=True, parents=True)
    args.output.write_text(yaml.safe_dump(result, sort_keys=False))
    args.output.with_suffix(".operators.yaml").write_text(yaml.safe_dump(operator_values(platform)))
    print(f"Wrote regional values to {args.output}")


if __name__ == "__main__":
    main()
