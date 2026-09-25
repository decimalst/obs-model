#!/usr/bin/env python3
"""Project private Terraform show/PV JSON into an offline disposable-lab review.

This tool never calls AWS, Kubernetes or a subprocess and never deletes anything.
Use ``terraform show -json`` state (not plan JSON or raw tfstate) and a PV List
fetched through the independently verified lab cluster endpoint. PV JSON alone
cannot establish that provenance. Before any deletion, independently compare AWS
resource tags and CSI volume ownership to the recorded IDs and cluster identity.
Input files may contain secrets; only the explicit projection below is emitted.
"""

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime
from pathlib import Path

MAX_BYTES = 32 * 1024 * 1024
NAME = r"[a-z][a-z0-9-]{2,23}"
RUN = r"[a-z0-9][a-z0-9-]{2,63}"
UID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
ID_PREFIX = {
    "aws_vpc": "vpc",
    "aws_subnet": "subnet",
    "aws_internet_gateway": "igw",
    "aws_nat_gateway": "nat",
    "aws_eip": "eipalloc",
    "aws_vpc_endpoint": "vpce",
    "aws_route_table": "rtb",
    "aws_security_group": "sg",
    "aws_launch_template": "lt",
    "aws_instance": "i",
}
PRIMARY = {
    "aws_vpc": ("id",),
    "aws_subnet": ("id", "vpc_id", "availability_zone"),
    "aws_internet_gateway": ("id", "vpc_id"),
    "aws_nat_gateway": ("id", "subnet_id", "allocation_id"),
    "aws_eip": ("id", "allocation_id"),
    "aws_vpc_endpoint": ("id", "vpc_id"),
    "aws_route_table": ("id", "vpc_id"),
    "aws_security_group": ("id", "vpc_id", "name"),
    "aws_launch_template": ("id", "name"),
    "aws_instance": ("id", "subnet_id"),
    "aws_s3_bucket": ("id", "bucket", "arn"),
    "aws_db_instance": ("id", "identifier", "arn", "final_snapshot_identifier"),
    "aws_db_subnet_group": ("id", "name", "arn"),
    "aws_secretsmanager_secret": ("id", "name", "arn"),
    "aws_elasticache_replication_group": ("id", "arn"),
    "aws_elasticache_subnet_group": ("id", "name", "arn"),
    "aws_elasticache_parameter_group": ("id", "name", "arn"),
    "aws_cloudwatch_log_group": ("id", "name", "arn"),
    "aws_eks_cluster": ("id", "name", "arn"),
    "aws_eks_node_group": ("id", "cluster_name", "node_group_name", "arn"),
    "aws_eks_addon": ("id", "cluster_name", "addon_name", "arn"),
    "aws_eks_access_entry": ("id", "cluster_name", "principal_arn", "access_entry_arn"),
    "aws_eks_pod_identity_association": (
        "id",
        "cluster_name",
        "namespace",
        "service_account",
        "role_arn",
        "association_arn",
    ),
    "aws_iam_role": ("id", "name", "arn"),
    "aws_iam_instance_profile": ("id", "name", "arn"),
    "aws_ecr_repository": ("id", "name", "arn"),
}
# These objects have no independent lab tags. Emit only their Terraform address
# and type for state review, never an unverified AWS identifier or policy payload.
RELATIONSHIPS = {
    "aws_route_table_association",
    "aws_iam_role_policy_attachment",
    "aws_iam_role_policy",
    "aws_eks_access_policy_association",
    "aws_autoscaling_group_tag",
    "aws_s3_bucket_public_access_block",
    "aws_s3_bucket_ownership_controls",
    "aws_s3_bucket_server_side_encryption_configuration",
    "aws_s3_bucket_versioning",
    "aws_s3_bucket_lifecycle_configuration",
    "aws_s3_bucket_policy",
    "aws_vpc_security_group_ingress_rule",
    "aws_vpc_security_group_egress_rule",
    "aws_secretsmanager_secret_version",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def safe_string(value):
    return (
        isinstance(value, str)
        and 0 < len(value) <= 512
        and re.fullmatch(r"[A-Za-z0-9_./:@+=,*-]+", value)
    )


def read_json(path):
    path = Path(path)
    require(not path.is_symlink(), "Input must not be a symlink")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        require(stat.S_ISREG(info.st_mode), "Input must be a regular file")
        payload = source.read(MAX_BYTES + 1)
    require(len(payload) <= MAX_BYTES, "Input exceeds the 32 MiB limit")
    try:
        value = json.loads(payload)
    except (ValueError, UnicodeError):
        raise ValueError("Input is not valid JSON") from None
    require(isinstance(value, dict), "Input must be a JSON object")
    return value, hashlib.sha256(payload).hexdigest()


def resources(module):
    require(isinstance(module, dict), "Malformed Terraform module")
    require(isinstance(module.get("resources", []), list), "Malformed Terraform resources")
    require(isinstance(module.get("child_modules", []), list), "Malformed Terraform children")
    yield from module.get("resources", [])
    for child in module.get("child_modules", []):
        yield from resources(child)


def arn_scope(arn, account, region):
    require(isinstance(arn, str), "Malformed resource ARN")
    parts = arn.split(":", 5)
    require(len(parts) == 6 and parts[:2] == ["arn", "aws"], "Unsupported resource ARN")
    require(parts[3] in ("", region), "Resource ARN belongs to another region")
    require(parts[4] in ("", account), "Resource ARN belongs to another account")
    if parts[2] not in ("s3", "iam"):
        require(
            parts[3] == region and parts[4] == account,
            "Regional ARN must identify the exact account and region",
        )
    elif parts[2] == "iam":
        require(parts[3] == "" and parts[4] == account, "IAM ARN must identify the exact account")


def expiry(value):
    require(
        isinstance(value, str) and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", value),
        "ExpiresAt must be an explicit UTC timestamp",
    )
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise ValueError("ExpiresAt is malformed") from None
    return value


def project_state(state, account, region, name, run_id):
    require(re.fullmatch(r"\d{12}", account), "Use a twelve-digit AWS account")
    require(re.fullmatch(r"[a-z]{2}-[a-z]+-\d", region), "Use a commercial AWS region")
    require(re.fullmatch(NAME, name), "Use a valid 3-24 character lab name")
    require(re.fullmatch(RUN, run_id), "Use a valid lab run ID")
    require(
        "planned_values" not in state and "resource_changes" not in state,
        "Supply Terraform show JSON for state, not a plan",
    )
    require(isinstance(state.get("values"), dict), "Supply Terraform show JSON for state")
    primary, related, seen, identities, expires = [], [], set(), set(), set()
    for resource in resources(state["values"].get("root_module")):
        require(isinstance(resource, dict), "Malformed Terraform resource")
        if resource.get("mode") == "data":
            continue
        require(resource.get("mode") == "managed", "Unknown Terraform resource mode")
        kind, address, values = (
            resource.get("type"),
            resource.get("address"),
            resource.get("values"),
        )
        require(
            isinstance(address, str) and re.fullmatch(r'[A-Za-z0-9_.\[\]"-]+', address),
            "Malformed Terraform resource address",
        )
        require(address not in seen, "Duplicate Terraform resource address")
        seen.add(address)
        require(isinstance(values, dict), "Missing Terraform resource values")
        require(kind in PRIMARY or kind in RELATIONSHIPS, "Unreviewed managed resource type")
        if kind in RELATIONSHIPS:
            related.append({"address": address, "type": kind, "ownershipCheck": "pending"})
            continue
        tags = values.get("tags_all") or values.get("tags") or {}
        require(
            isinstance(tags, dict) and tags.get("LabRun") == run_id,
            "Managed resource lacks the exact LabRun tag",
        )
        expires.add(expiry(tags.get("ExpiresAt")))
        fields = {}
        for key in PRIMARY[kind]:
            value = values.get(key)
            if value is None:
                continue
            require(safe_string(value), "Malformed resource identity field")
            if value.startswith("arn:"):
                arn_scope(value, account, region)
            fields[key] = value
        require(fields.get("id"), "Managed resource is missing its ID")
        require((kind, fields["id"]) not in identities, "Duplicate managed AWS identity")
        identities.add((kind, fields["id"]))
        if kind in ID_PREFIX:
            require(
                re.fullmatch(ID_PREFIX[kind] + r"-[0-9a-f]{8,17}", fields["id"]),
                "Malformed AWS resource ID",
            )
        if kind in {"aws_iam_role", "aws_iam_instance_profile"}:
            require(
                fields.get("name", "").startswith(name + "-"),
                "IAM identity is outside the lab name prefix",
            )
            require(
                fields.get("arn", "").startswith(f"arn:aws:iam::{account}:"),
                "IAM identity lacks the expected account ARN",
            )
        if kind in {"aws_secretsmanager_secret", "aws_launch_template", "aws_security_group"}:
            require(
                fields.get("name", "").startswith(name + "-"),
                "Named AWS resource is outside the lab name prefix",
            )
        if kind in {
            "aws_db_subnet_group",
            "aws_elasticache_subnet_group",
            "aws_elasticache_parameter_group",
            "aws_elasticache_replication_group",
        }:
            identity_name = fields.get("name", fields["id"])
            require(
                identity_name == name or identity_name.startswith(name + "-"),
                "Data-service resource is outside the lab name prefix",
            )
        if kind == "aws_ecr_repository":
            require(
                fields.get("name") == name
                or fields.get("name", "").startswith(name + "/")
                or fields.get("name", "").startswith(name + "-"),
                "Registry repository is outside the lab name prefix",
            )
        if kind == "aws_cloudwatch_log_group":
            require(
                fields.get("name") == f"/aws/eks/{name}/cluster"
                or fields.get("name", "").startswith("/" + name + "/"),
                "Log group is outside the lab name prefix",
            )
        if kind == "aws_s3_bucket":
            require(
                fields.get("bucket", "").startswith(f"{name}-{account}-{region}-"),
                "Bucket is outside the lab account/region/name prefix",
            )
            require(
                fields.get("arn") == "arn:aws:s3:::" + fields["bucket"]
                and fields["id"] == fields["bucket"],
                "Bucket identities disagree",
            )
        if kind == "aws_db_instance":
            require(fields.get("identifier") == name + "-grafana", "Unexpected lab database")
            require(
                fields.get("final_snapshot_identifier") == name + "-grafana-final",
                "Unexpected database final snapshot identity",
            )
        if kind in {
            "aws_eks_cluster",
            "aws_eks_node_group",
            "aws_eks_addon",
            "aws_eks_access_entry",
            "aws_eks_pod_identity_association",
        }:
            require(
                fields.get("name" if kind == "aws_eks_cluster" else "cluster_name") == name,
                "EKS resource belongs to another cluster",
            )
        primary.append(
            {
                "address": address,
                "type": kind,
                "identity": fields,
                "LabRun": run_id,
                "ExpiresAt": next(iter(expires)),
                "cloudOwnershipVerified": False,
            }
        )
    require(len(expires) == 1, "Managed resources must share one exact ExpiresAt tag")
    clusters = [r for r in primary if r["type"] == "aws_eks_cluster"]
    vpcs = [r for r in primary if r["type"] == "aws_vpc"]
    require(
        len(clusters) == len(vpcs) == 1, "Inventory requires exactly one lab EKS cluster and VPC"
    )
    cluster_arn = f"arn:aws:eks:{region}:{account}:cluster/{name}"
    require(
        clusters[0]["identity"].get("arn") == cluster_arn, "EKS cluster ARN is not the selected lab"
    )
    vpc_id = vpcs[0]["identity"]["id"]
    require(re.fullmatch(r"vpc-[0-9a-f]{8,17}", vpc_id), "Malformed lab VPC ID")
    subnets = {r["identity"]["id"] for r in primary if r["type"] == "aws_subnet"}
    zones = set()
    for resource in primary:
        fields = resource["identity"]
        require(
            "vpc_id" not in fields or fields["vpc_id"] == vpc_id,
            "Resource references a foreign VPC",
        )
        require(
            "subnet_id" not in fields or fields["subnet_id"] in subnets,
            "Resource references a foreign subnet",
        )
        if resource["type"] == "aws_subnet":
            zone = fields.get("availability_zone", "")
            require(re.fullmatch(re.escape(region) + "[a-z]", zone), "Subnet has a foreign AZ")
            zones.add(zone)
    return {
        "accountId": account,
        "region": region,
        "name": name,
        "runId": run_id,
        "clusterArn": cluster_arn,
        "vpcId": vpc_id,
        "expiresAt": next(iter(expires)),
        "zones": sorted(zones),
        "resources": sorted(primary, key=lambda r: r["address"]),
        "relationships": sorted(related, key=lambda r: r["address"]),
    }


def project_pvs(document, lab):
    require(
        document.get("kind") in ("List", "PersistentVolumeList")
        and isinstance(document.get("items"), list),
        "Supply a Kubernetes PV List JSON",
    )
    require(
        document.get("clusterArn", lab["clusterArn"]) == lab["clusterArn"],
        "PV source cluster attestation differs from the lab",
    )
    result, names, volumes, uids, claim_uids = [], set(), set(), set(), set()
    for item in document["items"]:
        require(
            isinstance(item, dict) and item.get("kind") == "PersistentVolume", "Unexpected PV item"
        )
        metadata, spec = item.get("metadata", {}), item.get("spec", {})
        claim, csi = spec.get("claimRef", {}), spec.get("csi", {})
        require(claim.get("namespace") == "observability", "PV claim is outside the lab namespace")
        require(spec.get("storageClassName") == "gp3-encrypted", "Unexpected lab storage class")
        require(csi.get("driver") == "ebs.csi.aws.com", "PV is not an EBS CSI volume")
        volume = csi.get("volumeHandle", "")
        require(
            isinstance(volume, str) and re.fullmatch(r"vol-[0-9a-f]{8,17}", volume),
            "PV has an invalid or nonlocal EBS volume handle",
        )
        uid, claim_uid, name = (
            metadata.get("uid", ""),
            claim.get("uid", ""),
            metadata.get("name", ""),
        )
        require(
            isinstance(uid, str)
            and re.fullmatch(UID, uid)
            and isinstance(claim_uid, str)
            and re.fullmatch(UID, claim_uid),
            "PV and claim must have exact Kubernetes UIDs",
        )
        require(safe_string(name) and safe_string(claim.get("name")), "PV/claim name is missing")
        require(
            name not in names
            and volume not in volumes
            and uid not in uids
            and claim_uid not in claim_uids,
            "Duplicate PV or EBS identity",
        )
        names.add(name)
        volumes.add(volume)
        uids.add(uid)
        claim_uids.add(claim_uid)
        labels = metadata.get("labels", {})
        require(isinstance(labels, dict), "Malformed PV labels")
        require(
            labels.get("topology.kubernetes.io/region", lab["region"]) == lab["region"],
            "PV carries a foreign region label",
        )
        terms = spec.get("nodeAffinity", {}).get("required", {}).get("nodeSelectorTerms", [])
        require(isinstance(terms, list) and terms, "PV requires explicit regional zone affinity")
        for term in terms:
            choices = [
                e
                for e in term.get("matchExpressions", [])
                if e.get("key") in ("topology.ebs.csi.aws.com/zone", "topology.kubernetes.io/zone")
            ]
            require(
                choices
                and all(
                    e.get("operator") == "In"
                    and e.get("values")
                    and set(e["values"]).issubset(lab["zones"])
                    for e in choices
                ),
                "PV affinity is not restricted to the lab's availability zones",
            )
        result.append(
            {
                "name": name,
                "uid": uid,
                "claimName": claim["name"],
                "claimUID": claim_uid,
                "claimNamespace": "observability",
                "volumeId": volume,
                "storageClass": "gp3-encrypted",
                "sourceClusterArn": lab["clusterArn"],
                "sourceClusterVerified": False,
                "cloudOwnershipVerified": False,
            }
        )
    return sorted(result, key=lambda p: p["name"])


def inventory(state, pvs, *, account, region, name, run_id):
    lab = project_state(state, account, region, name, run_id)
    return {
        "version": 1,
        "execution": "offline-review-only",
        "deletionAuthorized": False,
        "lab": lab,
        "persistentVolumes": project_pvs(pvs, lab),
        "cleanupReview": [
            "Verify caller account, region, exact cluster endpoint and every recorded cloud "
            "resource's LabRun/ExpiresAt tags independently; this file is not an ownership "
            "attestation.",
            "Compare PV UID/claim UID and EBS volume tags with this exact cluster and namespace. "
            "PV JSON alone cannot prove cluster provenance. Do not delete a volume based only "
            "on this inventory.",
            "Stop lab producers and suspend lab reconciliation/scaling. Remove platform custom "
            "resources while their controllers, EBS CSI and private runner remain available; "
            "wait for pod/volume detach.",
            "Review each recorded PVC/PV and exact detached EBS volume for disposal. Terraform "
            "does not own CSI data volumes. Do not remove finalizers blindly.",
            "After writers stop, review all versions, delete markers and multipart uploads in "
            "the exact recorded lab buckets. Current-key deletion alone cannot empty "
            "versioned buckets.",
            "Disable deletion protection only on the recorded lab database through a separately "
            "reviewed operation. Terraform requires its named final snapshot; review removal "
            "of that snapshot separately.",
            "Review destruction against the dedicated Terraform state. Wait for owning services "
            "to remove their ENIs; do not forcibly detach requester-managed interfaces.",
            "Verify snapshots, retained EBS volumes, bucket versions, scheduled secret deletion, "
            "registry artifacts, NAT/EIPs and any separately owned runner resources. Preserve "
            "the backend/state and external identities unless separately authorized.",
        ],
        "limitations": [
            "Only declared primary resource identity fields are projected. Relationship "
            "resources require review through the exact saved Terraform destroy plan.",
            "AWS-managed policies, service-linked roles, final snapshots, generated RDS secrets, "
            "ASGs/ENIs and external bootstrap resources may not be standalone Terraform resources "
            "in this state.",
            "ExpiresAt is recorded metadata, never deletion authorization or an automatic "
            "cleanup schedule.",
        ],
    }


def write_inventory(directory, content):
    directory = Path(directory)
    require(not directory.is_symlink(), "Output directory cannot be a symlink")
    directory.mkdir(mode=0o700, exist_ok=False)
    descriptor = os.open(directory / "inventory.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(content, output, indent=2, allow_nan=False)
        output.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ("state", "pvs", "account", "region", "name", "run-id", "output"):
        parser.add_argument("--" + argument, required=True)
    args = parser.parse_args(argv)
    try:
        state, state_digest = read_json(args.state)
        pvs, pv_digest = read_json(args.pvs)
        result = inventory(
            state, pvs, account=args.account, region=args.region, name=args.name, run_id=args.run_id
        )
        result["inputSHA256"] = {
            "terraformStateJSON": state_digest,
            "persistentVolumeJSON": pv_digest,
        }
        write_inventory(args.output, result)
    except (ValueError, OSError, TypeError, KeyError, AttributeError, RecursionError):
        # State can contain credentials. Never echo exception bodies or rejected input.
        print(
            "Lab inventory rejected: verify scoped state/PV inputs, tags "
            "and a new output directory.",
            file=sys.stderr,
        )
        return 1
    print("Offline lab inventory written; cloud ownership and cleanup remain unverified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
