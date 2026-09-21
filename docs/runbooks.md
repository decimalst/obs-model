# Operations runbook

## Bootstrap and deploy

For existing EKS, IAM roles, controllers, storage classes or data services, use [the existing-environment guide](existing-clusters.md). That path has a different ownership boundary and does not create or adopt the cluster.

Deployment also requires AWS CLI v2, kubectl compatible with EKS 1.35, and the Argo Rollouts kubectl plugin compatible with controller 1.10.0. The private runner must have API network access as well as the configured EKS access entry.

1. Choose the first region and verify three usable AZs, EKS/EC2/RDS/ElastiCache quotas, and the baseline cost. The example roots use `a/b/c`; account-specific AZ letters may map to different physical zones. Region CIDRs do not overlap.
2. Use a versioned, encrypted S3 state bucket with public access blocked and a backend key per region. Copy `backend.hcl.example` to an ignored local file outside source control. The backend uses S3 lockfiles. State includes the sensitive Valkey password; tightly restrict state read access. State infrastructure is a bootstrap prerequisite, not managed recursively by this module.
3. Run from a private deployment runner with regional VPC/API reachability and an authorized IAM role. Set the required Owner/Environment/CostCenter and administrator ARN in a private `.tfvars`. Generate a 32+ character alphanumeric Valkey password in your secret manager and expose it only as `TF_VAR_quota_redis_auth_token` for Terraform. Do not put it on the command line or print it.
4. Initialize and review a plan, then apply the saved plan when ready:

```sh
terraform -chdir=terraform/live/us-east-1 init -backend-config=/secure/us-east-1.backend.hcl
terraform -chdir=terraform/live/us-east-1 plan -var-file=/secure/production.tfvars -out=/secure/us-east-1.tfplan
terraform -chdir=terraform/live/us-east-1 apply /secure/us-east-1.tfplan
mkdir -p build
terraform -chdir=terraform/live/us-east-1 output -json > build/us-east-1.terraform.json
```

5. Publish the quota image to a registry accessible by worker nodes (the manual workflow publishes an amd64 image to GHCR; use ECR or add a pull secret for private GHCR). Use the immutable registry digest in generated values:

```sh
aws eks update-kubeconfig --region us-east-1 --name olly-us-east-1
.venv/bin/python scripts/install.py --config build/us-east-1.terraform.json \
  --quota-image REGISTRY/log-quota@sha256:YOUR_PUBLISHED_DIGEST --check-only
# Remove --check-only to install after the read-only checks and local render pass.
```

`deploy.sh` prepares CRDs, installs controllers before custom resources, fetches RDS/Valkey credentials from Secrets Manager, preserves existing generated Grafana/ClickHouse credentials, and installs the regional platform. It never runs Terraform apply. There is no public load balancer. Access Grafana by private port-forward during acceptance, then add an authenticated HTTPS front door appropriate to your organization. `cookie_secure` assumes HTTPS for routine browser use.

Altinity 0.27.3 CRDs use YAML merge keys that Helm expands to duplicate descriptions/type entries. `bootstrap_crds.py` expands and canonicalizes those pinned definitions; it allows repeated descriptions or identical values only and rejects conflicting schema keys. Apply `build/bootstrap-crds.yaml` server-side, including during upgrades, and use Helm `--skip-crds`; the Altinity CRD hook is disabled. Helm alone is not the supported installation path. Cert-manager and OTel templated CRDs remain owned by their charts.

The first install can take time for operator reconciliation, PVCs, and backends to become ready. Inspect all data stores before the schema step. The SQL is idempotent for creating the initial schema; changing an existing table requires an explicit migration, not merely editing `CREATE IF NOT EXISTS`.

6. Run acceptance in [testing.md](testing.md). Then repeat with the west root and independent state. Do not copy a region's generated values into another cluster.

## Upgrade and rollback

- Run validation and review dependency/CRD changes. Save database snapshots and record Kafka lag/ring health before stateful changes.
- Stateless upgrades create a preview ReplicaSet; readiness analysis must succeed and operators inspect data-path metrics. Promote one service with `scripts/promote.sh NAME`. Keep old pods alive for at least 60 seconds to let Kubernetes routing converge. Manual approval does not replace end-to-end canary verification.
- On failure, use `kubectl argo rollouts abort NAME -n observability`, inspect the failure, and restore the previous manifest/image. A healthy old ReplicaSet remains the rollback target during the delay. Long-lived OTLP connections can outlive a Service selector change; server shutdown/drain and client retries matter.
- Tail samplers have `OnDelete`. Do not use HPA or bulk delete. Pause/reduce tracing traffic, allow the decision window and exporter queues to drain, replace one ordinal, verify routing/health, then continue. The in-memory cache cannot be transferred; strictly lossless sampling upgrades are not provided.
- Kafka/Keeper/ClickHouse: verify ISR/quorum/replica health and available AZs; replace one member at a time. Never reduce replication to save cost during an outage. Helm/Argo rollback cannot undo storage format or database schema migrations.
- Mimir: keep Kafka partitions and per-zone ingester ownership aligned. Tempo's zone-aware chart option is upstream experimental. Prove AZ failure behavior for the pinned releases before production reliance.
- RDS managed password rotation requires re-seeding `grafana-db` and restarting Grafana pods; env-based consumers do not automatically refresh. Coordinate Valkey auth rotation with the store and quota consumers. Back up generated Kubernetes application secrets securely before cluster replacement.

## Regional incident

Keep ingestion region-local by default. A surviving region serves its own workloads and telemetry. Do not automatically forward a failing region's entire backlog to another: that changes egress cost, quota load, and trace affinity. If regional history must survive region loss, explicitly design and price asynchronous object/database/Kafka replication and define RPO/RTO. This repo does not assert a tested cross-region recovery time.

## Debug operations automation

Install `charts/debug-grants` into each application namespace to handle replacement pods. The manual workflow expects an `observability-private` runner, an `observability-operations` GitHub environment, and `OBSERVABILITY_OPERATIONS_ROLE`. Configure GitHub OIDC trust to the exact repository/environment and grant that role namespace-scoped Kubernetes access to get/list workloads/ReplicaSets/pods, patch workload metadata, and patch pod grant annotations. Do not use the cluster administrator role for routine debug grants.

## Teardown

No scheduled workflow destroys infrastructure. Expired debug grants only stop collection; they never delete cloud resources. Inventory data retention/export requirements first. S3 `force_destroy=false`, RDS deletion protection/final snapshots, and retained PVCs deliberately prevent casual destructive teardown. Remove Helm releases only after draining and preserving data. Disable protection and delete retained data only as an explicit separately reviewed operation. Terraform destroy will otherwise stop at protected resources, which is expected.

## Alert rules

`docs/alerts.yaml` contains a Mimir rule group for sustained quota drops, dependency failures, and refused spans. After verifying ingestion, use the authenticated regional Mimir ruler API to upload it to an owned namespace. Configure an Alertmanager receiver separately; this repository does not invent recipients or send notifications. Grafana's multi-replica unified alerting is disabled to avoid duplicate evaluation without additional HA coordination. The included Grafana dashboard shows the same protection metrics.
