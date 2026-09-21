#!/usr/bin/env python3
"""Generate region coordinates from nonsensitive Terraform outputs."""

import argparse
import json
import pathlib
import re

import yaml


def values(platform, quota_image):
    region = platform["region"]
    if not re.fullmatch(r".+@sha256:[a-f0-9]{64}", quota_image):
        raise ValueError("Use the published log-quota image digest, not a mutable tag")
    zones = [
        {"name": z, "nodeSelector": {"topology.kubernetes.io/zone": region + z}} for z in "abc"
    ]
    buckets = platform["buckets"]
    return {
        "region": region,
        "clusterName": platform["cluster_name"],
        "images": {"quota": quota_image},
        "quota": {"redisAddress": platform["quota_redis_host"] + ":6379"},
        "networkPolicy": {"vpcCIDR": platform["vpc_cidr"]},
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
                key: {"zoneAwareReplication": {"zones": zones}}
                for key in ["ingester", "store_gateway", "alertmanager"]
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
            "ingester": {"zoneAwareReplication": {"zones": zones}},
        },
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--terraform-output", type=pathlib.Path, required=True)
    p.add_argument("--quota-image", required=True)
    p.add_argument("--output", type=pathlib.Path, required=True)
    args = p.parse_args()
    platform = json.loads(args.terraform_output.read_text())["platform"]["value"]
    result = values(platform, args.quota_image)
    args.output.parent.mkdir(exist_ok=True, parents=True)
    args.output.write_text(yaml.safe_dump(result, sort_keys=False))
    args.output.with_suffix(".operators.yaml").write_text(
        yaml.safe_dump(
            {
                "cluster-autoscaler": {
                    "awsRegion": platform["region"],
                    "autoDiscovery": {"clusterName": platform["cluster_name"]},
                }
            }
        )
    )
    print(f"Wrote regional values to {args.output}")


if __name__ == "__main__":
    main()
