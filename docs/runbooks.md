# Operations runbook

## Bootstrap and deploy

For an authorized, short-lived real-account deployment test, use the [disposable AWS lab procedure](aws-lab.md). It preserves the production HA topology and makes private-runner setup, cost controls and retained-resource cleanup explicit.

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

The installer supports Helm 3 and 4. For the `olly` release, Helm 4 receives `--server-side=false` to retain the client-side three-way merge used by Helm 3: Argo Rollouts adds a selector hash to stable/preview Services, and server-side apply conflicts with that controller-owned selector map. Keep those hashes under Argo's control; do not use force-conflict, force-replacement or ownership-takeover flags to recover a failed upgrade. Re-run the installer with the reviewed configuration after the prior Helm operation has finished. [Helm upgrade apply modes](https://helm.sh/docs/helm/helm_upgrade/)

Altinity 0.27.3 CRDs use YAML merge keys that Helm expands to duplicate descriptions/type entries. `bootstrap_crds.py` expands and canonicalizes those pinned definitions; it allows repeated descriptions or identical values only and rejects conflicting schema keys. Apply `build/bootstrap-crds.yaml` server-side, including during upgrades, and use Helm `--skip-crds`; the Altinity CRD hook is disabled. Helm alone is not the supported installation path. Cert-manager and OTel templated CRDs remain owned by their charts.

The first install can take time for operator reconciliation, PVCs, and backends to become ready. Inspect all data stores before the schema step. The SQL is idempotent for creating the initial schema; changing an existing table requires an explicit migration, not merely editing `CREATE IF NOT EXISTS`.

6. Run acceptance in [testing.md](testing.md). Then repeat with the west root and independent state. Do not copy a region's generated values into another cluster.

## Upgrade and rollback

- Run validation and review dependency/CRD changes. Save database snapshots and record Kafka lag/ring health before stateful changes.
- Stateless upgrades create a preview ReplicaSet; readiness analysis must succeed and operators inspect data-path metrics. Promote one service with `scripts/promote.sh NAME`. Keep old pods alive for at least 60 seconds to let Kubernetes routing converge. Manual approval does not replace end-to-end canary verification.
- On failure, use `kubectl argo rollouts abort NAME -n observability`, inspect the failure, and restore the previous manifest/image. A healthy old ReplicaSet remains the rollback target during the delay. Long-lived OTLP connections can outlive a Service selector change; server shutdown/drain and client retries matter.
- Tail samplers have `OnDelete`. Do not use HPA or bulk delete. Pause/reduce tracing traffic, allow the decision window and exporter queues to drain, replace one ordinal, verify routing/health, then continue. The in-memory cache cannot be transferred; strictly lossless sampling upgrades are not provided.
- The read-only smoke check waits for sampler readiness and verifies the observed generation, desired/ready/updated replica counts, ownership and each pod's target revision. Healthy pods on an older revision report **pending trace drain** immediately; the check never replaces them. An OnDelete StatefulSet can retain an older `currentRevision` after a completed manual upgrade, so acceptance compares actual pod hashes with `updateRevision` instead.
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

Rules in `charts/platform/files/rules/platform.yaml` are mounted into every Mimir ruler and reconciled from Git. Edit these and their `tests/rules/` behavior tests through PRs; the ruler write API is disabled. Configure an Alertmanager receiver separately before relying on paging. Grafana unified alerting remains disabled to avoid duplicate evaluation. See [GitOps](gitops.md) for migration of existing API/S3 rules and dashboard/query ownership.

For PR-based configuration sync and drift repair, see [GitOps](gitops.md). For every component's scaling control and stateful constraints, see [capacity operations](scaling.md). Quota-policy changes intentionally backpressure while old policies drain; do not reset active keys.

ClickHouse now defaults to two shards × three replicas. Use the [sharding runbook](clickhouse-sharding.md) for the write-pause expansion procedure and the mandatory pre-0.3 schema migration. A post-install/upgrade Helm hook establishes the schema during GitOps reconciliation.

## Service onboarding, profiling and studio publication

Service registration/creation follows [the catalog runbook](service-catalog.md): reviewed definition, `make catalog`, generated content committed together, application identity and namespace onboarding, then end-to-end telemetry verification. Publish a scaffold only after committing it as its own reviewed repository. Owner labels are not alert-delivery configuration; prove the notification path separately.

Enable profiles with a separately reviewed Terraform plan (`enable_profiling=true`) or externally provisioned regional bucket/role/association, then the updated platform contract. Follow [eBPF qualification](ebpf.md), confirm namespace/RBAC and kernel prerequisites, and admit only labeled workloads. Cilium remains under its original networking owner; changing the observability release never migrates the CNI.

Before disabling profiles, remove workload opt-in labels, drain exporters, preserve backend query access for the retention period and rehearse metastore/object-store restore. Do not flip Terraform storage off as a cleanup shortcut: buckets deliberately reject deletion while nonempty and PVCs remain retained. Do not change Pyroscope storage architecture, metastore membership or role ownership through a casual Helm rollback. Plan upgrades and any removal as a separate stateful migration.

Publish the configuration studio only with the allowlisted Pages artifact and reviewed workflow environment. No catalog/environment data belongs in the public bundle. The [studio runbook](configuration-studio.md) covers offline generation, GitHub setup and the explicit publication step.

## Support rollout (0.6)

Follow [support.md](support.md) for namespace collection opt-in, a dedicated existing canary Secret and read-only database profile, bounded canary enablement, existing receiver Secrets and explicit Alertmanager tenant ownership. Preserve any previous tenant configuration securely before handover; disabling the reconciler does not delete stored routes. Observe the restored safe metric volume after the cardinality correction. ClickHouse's new metrics listener and optional user/profile require a rolling operator update with quorum preserved, not a schema or shard migration. Qualify owner/default notification routing and resolution against real receivers separately from local tests.

The debug workflow now requires the actual cluster and catalog service and defaults to denying actors until `operations/debug-access.json` is reviewed. Scope IAM/RBAC and protect the workflow/default branch before enabling requests. Collect support inventories locally and review them before sharing. Recovery, AZ-loss and upgrade rehearsal gates remain required.

## Configuration rollout and deferred qualification (0.7)

Prepare [setup options](setup-options.md) with `install.py --render-only` before any live work. Security profiles preserve existing Secret/IdP/ingress/mesh ownership; enabling a shared Grafana encryption key on an existing database requires the key-migration procedure in [security.md](security.md), never a casual key replacement. Local login removal, callback/role changes and certificate policy require a reviewed recovery path.

[Privacy and service graphs](privacy-and-service-graphs.md) introduce intentional data loss/metric schema choices. Keep policies opt-in, review historical buffer/storage treatment, drain the fixed sampler tier for graph or privacy configuration changes, and allow metric windows to age out after histogram changes. No Kafka topic, ClickHouse shard or quorum store is replaced by this release.

Use [recovery.md](recovery.md) for native/external backup ownership, isolated restore and stateful roll-forward. Run every real account scenario later through the single [qualification batch](live-qualification.md). Never turn an unexecuted receipt into a success or reuse evidence after configuration/dependency changes. Infra creation, fault injection and restore remain explicit operator actions outside the offline planner.
