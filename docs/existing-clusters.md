# Install into an existing environment

Choose the ownership boundary that fits your environment:

| Mode | What this repository creates | What stays externally managed |
|---|---|---|
| New region | VPC, EKS, nodes, IAM, data services, charts | Deployment identity and Terraform state bucket |
| Existing EKS | Regional S3/RDS/Valkey, optional workload roles/associations, charts | EKS, VPC, subnets, nodes, cluster/node roles, access entries, add-ons, routing and capacity management |
| Software only | Selected controllers and platform Helm release | All AWS infrastructure, workload roles/associations, and existing secrets |

All modes retain three-AZ placement, regional ingestion, shared log quotas, sampling limits and expiring debug grants. Existing EKS support does not import or take over your cluster. Namespaces and release names remain fixed at `observability`, `observability-system`, `olly` and `operators`; use one platform installation per cluster.

Review the [IAM and Kubernetes identity inventory](iam.md) before choosing role ownership. It lists every role created by each path, trust and storage permissions, externally supplied installer/CI identities, and the separate EKS administrator access grant.

## Existing EKS with new data services

Use [the existing-cluster Terraform root](../terraform/live/existing-cluster). Start from `inputs.example.tfvars`, with your region, cluster name, three private subnet IDs in distinct AZs, ownership tags, and a unique platform name. The module discovers the VPC, cluster security group and actual AZs from AWS. It rejects subnets from another VPC and subnets that automatically assign public IPs. Review private route tables as well: absence of automatic public IP assignment is not proof of a private route.

Optionally provide:

- `existing_role_arns`: any subset of `mimir`, `tempo`, `flink`, plus `pyroscope` when `enable_profiling = true`. Omitted active roles are created; supplied roles receive no IAM policy/trust changes. Terraform outputs `required_role_policies` for the existing role owner to review and attach. The Flink storage scaffold is created even while its optional application remains disabled.
- `manage_pod_identity_associations = false`: retain external ownership of all service-account associations. In this mode provide all three base roles and the fourth, `pyroscope`, when profiling is enabled; the cluster owner must already manage their exact associations. Do not leave newly generated roles disconnected from externally managed associations.
- `terraform_role_arn`: assume your existing provisioning role. Standard AWS credential chains and `AWS_PROFILE` also work. This role is not automatically used by AWS CLI or granted Kubernetes access; configure the install runner's identity separately.
- `additional_client_security_group_ids`: existing node/pod security groups that need database/cache access. Rules are created on the **new** data-services security group; existing groups are not modified. Their egress rules remain your responsibility.

Supplied roles must belong to the EKS account and trust `pods.eks.amazonaws.com` for `sts:AssumeRole` and `sts:TagSession`. An IRSA-only trust policy cannot be used unchanged as a Pod Identity role. The owner may add Pod Identity trust while retaining IRSA, or supply a separate role. See [AWS's association prerequisites](https://docs.aws.amazon.com/eks/latest/userguide/pod-id-association.html). The installer checks association ARNs; it does not simulate IAM policy evaluation.

Create an independent encrypted state key, supply `TF_VAR_quota_redis_auth_token` securely, and review the saved plan before applying:

```sh
terraform -chdir=terraform/live/existing-cluster init -backend-config=/secure/existing.backend.hcl
terraform -chdir=terraform/live/existing-cluster plan -var-file=/secure/existing.tfvars -out=/secure/existing.tfplan
terraform -chdir=terraform/live/existing-cluster apply /secure/existing.tfplan
mkdir -p build
terraform -chdir=terraform/live/existing-cluster output -json > build/existing.json
```

The example intentionally defaults to reusing metrics-server and external capacity management. Install/repair those prerequisites with the cluster owner before proceeding. It never installs EKS add-ons into someone else's cluster or alters EKS access entries automatically.

## One installation entry point

After `make setup deps`, select the intended Kubernetes context and run:

```sh
.venv/bin/python scripts/install.py --config build/existing.json \
  --quota-image REGISTRY/log-quota@sha256:YOUR_PUBLISHED_DIGEST \
  --reuse-operators cert-manager \
  --check-only
```

`--check-only` performs read-only AWS/Kubernetes checks and local Helm rendering. It writes generated files under ignored `build/install/`. Remove `--check-only` to install after the checks pass. The same command accepts new-region Terraform outputs. It never applies Terraform or switches your Kubernetes context.

Use `--reuse-operators` with any already installed controllers: `cert-manager`, `argo-rollouts`, `strimzi-kafka-operator`, `opentelemetry-operator`, `altinity-clickhouse-operator`, `cluster-autoscaler`, `metrics-server`, `keda`. A disabled controller contributes neither Helm resources nor bootstrap CRD updates. Existing controllers must serve the required CRD versions, run a compatible release and watch `observability`; the check verifies served versions and availability, while version compatibility and watch configuration require review. Detected controller namespaces are added to the platform's trusted ingress namespaces.

Use `--existing-storage-class retained-encrypted-gp3` to reuse storage. It must use encrypted EBS CSI, `WaitForFirstConsumer` and `Retain`. The chosen class propagates into the backend charts and all platform PVCs. The installer refuses to adopt a pre-existing class into Helm. It also refuses to turn an already managed controller into an externally owned controller simply by disabling it, because Helm would otherwise uninstall it.

## Software-only installation

Copy [examples/existing-platform.yaml](../examples/existing-platform.yaml) to an ignored local file under `build/` and fill in the real regional coordinates. Point `--config` at this YAML instead of Terraform output. No Terraform is needed.

The profile describes existing regional buckets, TLS Valkey on port 6379, roles/associations and storage. Use dedicated bucket namespaces and an empty Grafana PostgreSQL database rather than mixing unrelated installations. Valkey needs `noeviction`, bounded memory, same-region placement and Multi-AZ failover; PostgreSQL needs TLS, a dedicated user/database and HA. The source-of-truth remains your infrastructure repository.

With `secret_source: existing`, seed these Secrets in `observability` yourself or through your secret manager:

| Secret | Required keys |
|---|---|
| `grafana-db` | `GF_DATABASE_HOST` (host:5432), `GF_DATABASE_PASSWORD` |
| `grafana-admin` | `admin-user`, `admin-password` |
| `quota-redis` | `password` |
| `clickhouse-auth` | `password` |

No secret is created or updated in this mode. Optional `grafana_db_name` and `grafana_db_user` default to `grafana`. Alternatively, use `secret_source: aws` with `grafana_db_host`, `grafana_db_secret_arn` (JSON containing `password`) and `quota_secret_arn` (raw password string); the existing secret-seeding path applies. Keep passwords/tokens out of the configuration file.

## Prerequisites and what preflight proves

The current installer compatibility target is EKS 1.35 with at least two Ready, schedulable, untainted Linux/amd64 nodes in each selected AZ, the EBS CSI driver, a healthy EKS Pod Identity agent, and AWS VPC CNI NetworkPolicy support. The preflight compares the actual API endpoint and cluster ARN, checks node/AZ placement, ownership conflicts, bucket regions, association ARNs, existing storage settings, metrics API availability and secret key names. The runner needs read access to those AWS/Kubernetes objects. Installation needs the usual controller/CRD and namespace permissions. Newly created clusters use the same checks.

It does not prove network reachability from every pod, effective IAM permissions, RDS/Valkey configuration, available compute headroom, controller watch scope, image pull access, actual throughput, failover, or application data-path health. Follow [live acceptance](testing.md). Alternate Kubernetes versions, IRSA-only installation, IPv6-only clusters, tainted dedicated pools, different network-policy engines and EKS Auto Mode storage need a separately qualified profile; checks are intentionally explicit about this boundary.

Existing VPCs must already provide regional S3 gateway routing and appropriate NAT/endpoints, DNS and database/cache connectivity. This root does not edit shared route tables or security groups to reduce cost. Verify those paths before assuming the cost characteristics of the new-region root.

## Upgrading an earlier checkout

Storage resources moved from `modules/region` into the shared `modules/platform-services`. Committed Terraform `moved` declarations preserve their existing addresses. Run `terraform init`, inspect a saved plan, and require address moves rather than unexpected deletes/replacements. This migration was syntax/mock tested; no live state migration has been performed. Preserve the move declarations for existing users.

Refresh Terraform outputs to obtain the explicit cluster ARN, AZ list and role ARNs required by the installer. Existing `scripts/deploy.sh` entry points now also run preflight and reject stale/mismatched generated overlays. Do not switch owned roles to `existing_role_arns` or toggle association ownership on a live stack without a separately reviewed state/ownership migration: ordinary Terraform removal would otherwise destroy resources it currently owns.

Kafka backlog scaling uses KEDA 2.20.2 by default. Reuse a compatible existing installation with `operators.keda: false` (or `--reuse-operators keda`); preflight verifies its CRD, controller and external metrics API availability and includes its namespace in Kafka access rules. Confirm the reused operator watches `observability`, has the default `app: keda-operator` label, and supports `currentReplicasIfHigher`. The installer rejects silently replacing another external metrics adapter. When KEDA cannot coexist with that adapter, explicitly set `operators.keda: false` and `tuning.writerAutoscaling.mode: cpu`. CPU fallback does not respond directly to consumer lag. Do not disable an already owned KEDA release without an ownership/migration plan.
