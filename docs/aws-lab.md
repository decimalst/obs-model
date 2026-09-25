# Disposable AWS deployment test

Use `terraform/live/lab` for an explicitly authorized live test. It wraps the production regional module, keeps three AZs, six initial on-demand nodes and retained storage, and adds a private SSM runner and a private artifact bucket/ECR repository. It is separate from the offline qualification recorder. This procedure incurs AWS charges and deletes only the new lab's disposable data.

The first pass targets deployment, service-account credentials, actual stored signals, bounded noisy-neighbor behavior and verified teardown. Each result must be recorded separately; completing an installation is not a teardown result. This procedure does not establish complete DR, production SSO, AZ-loss tolerance or every gate in [live qualification](live-qualification.md). A small isolated Grafana database restore can be added after deployment succeeds; full distributed-backend recovery follows [the recovery runbook](recovery.md) in another reviewed window if necessary. The bounded 2026-09-25 results are recorded below and in [the validation record](validation.md#2026-09-25-authorized-disposable-aws-deployment).

## Prerequisites before creating resources

- Use an explicit named SSO profile and verify `aws sts get-caller-identity` against the selected account. The existing EKS administrator is the full IAM role ARN, including its path, not the returned STS session ARN. Never copy credentials into the repository or command output.
- Confirm three real account AZs, EKS 1.35 availability and instance offerings. Six `m7i.xlarge` nodes plus a `t3.medium` runner need **26 standard on-demand vCPUs**. The lab caps each AZ at three worker nodes: nine workers plus the runner need **38 vCPUs** at maximum. A quota request does not reserve capacity or create instances. Do not create partial infrastructure while a required quota is pending.
- Record a unique `olly-lab-...` name, `LabRun`, owner, `Environment=lab`, cost center and `ExpiresAt`, with a six-hour working window and room for cleanup. Expiry tags are inventory metadata; they do not automatically delete AWS resources.
- Create a dedicated encrypted/versioned state bucket with public access blocked and a unique S3 lockfile/state key, or use an explicitly supplied existing backend. Keep bootstrap ownership recorded separately. The runner must not have state access.
- Resolve an official Amazon Linux 2023 x86-64 AMI to an exact AMI ID in the chosen region. The runner has no public IP or SSH ingress. SSM and client downloads use the regional NAT path; the EKS security group admits HTTPS only from its runner security group.
- Write private run inputs, quota password, plans, state exports and evidence under a restricted directory outside Git. Plan/state may contain secrets even when Terraform marks a variable sensitive. Avoid printing plan/state JSON. No existing IAM roles, controllers or storage are adopted.

SSO credentials must be refreshable for the whole run and teardown. The runner uses its own scoped EC2 role for EKS installation, prerequisite reads and the two platform Secrets; it does not get Terraform provisioning permissions. Provider authentication and state-backend authentication remain separate. See [IAM setup](iam.md) and [AWS private EKS access](https://docs.aws.amazon.com/eks/latest/userguide/cluster-endpoint.html).

## Cost envelope

Public Ohio prices checked on 2026-09-25 give a default fixed baseline of approximately **$1.79/hour**, before the runner, variable traffic, requests, logs and backups. This includes six workers, three NAT gateways/IPv4 addresses, EKS standard support, Multi-AZ Grafana RDS, three Valkey nodes, 1,156 GiB rendered persistent data claims and 480 GiB worker root disks. At this lab's nine-worker ceiling, workers and their extra roots add about $0.63/hour. Storage is prorated here using September's 720 hours.

A **$60 planning allowance for a six-hour window plus cleanup** provides room under a $100 target, but is not a guarantee or billing cap. Bound test traffic and storage growth, record the actual creation time, and begin cleanup early if installation stalls. Credits offset eligible billing; they do not make leaked resources safe. AWS Budgets updates are delayed, so alerts cannot serve as an immediate hard stop. [AWS Budgets timing](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-managing-costs.html)

Price sources: [Ohio EC2, EBS and NAT offer](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/us-east-2/index.json), [Ohio RDS offer](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonRDS/current/us-east-2/index.json), [Ohio ElastiCache offer](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonElastiCache/current/us-east-2/index.json), [EKS pricing](https://aws.amazon.com/eks/pricing/), [IPv4/NAT pricing](https://aws.amazon.com/vpc/pricing/). Refresh these before another run; costs and available instance capacity can change.

## Deploy and prove the data paths

1. Run repository checks and lab mock tests locally. Initialize the dedicated lab backend with its committed provider lock. Save a real Terraform plan to the private evidence directory; check account, region, all creates/changes, tags and ownership before applying that exact plan. Keep its digest with the reviewed revision.
2. Apply the reviewed lab plan. Recheck actual EKS ARN, node placement and private runner identity. Save only the nonsecret `platform` output for the installer, plus an inventory of created IDs; keep raw state private.
3. Build the quota service for **linux/amd64**, push it to the lab's immutable ECR repository and resolve its registry digest. The locally tested arm64 image ID is not a deployable amd64 registry digest. Use the immutable digest in installation values.
4. Build a nonsecret source bundle from explicitly selected scripts, charts, integrations, pinned Python requirements and the platform contract. Do not archive the repository root, `.git`, AWS profiles, Terraform state, plans, environment files or credential-bearing values. Store its SHA-256 digest and upload only to the private lab artifact bucket.
5. Run `scripts/lab_runner_bootstrap.sh` on the exact runner via SSM with account, region, cluster, artifact bucket and bundle hash. It verifies identity and the bundle, installs checksum-pinned Helm/kubectl and Python requirements, uses `helm_dependencies.py` to register the selected HTTPS repositories and build platform/operator dependencies from their committed chart locks, and creates a private dedicated kubeconfig. Existing matching repository aliases are reused; foreign aliases are never overwritten. This dependency preparation works with an empty Helm configuration/cache and is required before the first render: tracked-source archives omit ignored chart packages. It does not deploy. Keep `KUBECONFIG=/opt/CLUSTER/kubeconfig` explicit in every subsequent runner command.
6. Run `install.py --render-only`, then `--check-only`, then the installer with the same configuration and immutable image. The installer orders CRDs/controllers before platform resources. Wait for actual Kafka/ClickHouse reconciliation, all expected replicas and encrypted three-AZ volume placement. Preserve the platform's supported Helm hook sequencing described in [startup gates](startup-gates.md): waiting for initialized writers before the post-install schema hook would deadlock. Reapply the same configuration and inspect convergence without replacements or unexpected changes.
7. Enable the [stored-signal canary](support.md) using synthetic traffic, the explicit collection namespace label and a separately generated read-only ClickHouse credential. Confirm fresh exact logs in ClickHouse, metrics in Mimir and traces in Tempo. Readiness alone is insufficient. Verify Pod Identity access succeeds for assigned buckets and fails for an unrelated lab-owned test target.
8. Exercise one noisy logging service alongside a quiet service and verify that ERROR traffic spends its budget; test expiring debug collection and configured cardinality rejection. Observe Kafka consumer lag, stable consumer group, bounded KEDA scaling, ClickHouse write shards and catch-up. Keep these tests inside the chosen traffic/time ceilings; collect success and failure evidence without secrets.
9. Optionally take a Grafana RDS snapshot, restore into a distinct lab database, and verify a known synthetic row/query before deleting the restored database and snapshot. A database restore does not qualify ClickHouse, Kafka, Mimir, Tempo or profile recovery. Leave all unexecuted qualification gates pending.

After enabling the canary, run the explicit live helper from the private runner:

```sh
.venv/bin/python scripts/lab_acceptance.py \
  --context "$lab_cluster" --cluster "$lab_cluster" \
  --account "$lab_account" --region "$lab_region" \
  --confirm-cluster-arn "arn:aws:eks:$lab_region:$lab_account:cluster/$lab_cluster" \
  --output build/lab-acceptance.json --timeout 900
```

The default mode is read-only and requires two fresh completed canary cycles from both support replicas. For the bounded noisy-neighbor test, deploy an explicit lab tuning override of `quota.eventsPerSecond: 20`, `eventBurst: 40`, `debugEventsPerSecond: 5`, and `debugEventBurst: 10` before the initial install, then append `--noisy-neighbor`. This creates three short-lived producer pods: 3,000 total ERROR messages from one service and 30 INFO messages from a separate quiet service. It verifies distinct stored quiet markers, limited stored flood markers, quota drops without unavailability, and fresh canaries afterward; its `finally` block deletes only the test Jobs using their exact UIDs. The output file must be new. Production defaults are unchanged. These successful subchecks still leave partial-ingestion rejection, failover and full qualification pending; aggregate quota counters are not exact per-service accounting.

For a separate shared-service burst check, run:

```sh
.venv/bin/python scripts/lab_shared_quota.py \
  --context "$lab_cluster" --cluster "$lab_cluster" \
  --account "$lab_account" --region "$lab_region" \
  --confirm-cluster-arn "arn:aws:eks:$lab_region:$lab_account:cluster/$lab_cluster" \
  --output build/lab-shared-quota.json --timeout 300
```

This emits 100 normalized ERROR records directly to each of exactly three stable active quota replicas using one fresh synthetic service and no retry. It deliberately bypasses node-agent collection and supplements the end-to-end neighbor test. All replicas must have the reviewed 20 EPS / 40-event burst, and each byte burst must be at least 307,200 bytes so a byte limit cannot conceal an event-quota defect. The helper measures the complete concurrent request window, requires all three HTTP 200 responses and a global bound below three independent 40-event bursts, then verifies that every accepted distinct marker reaches ClickHouse and remains stable for 20 seconds. It reads mounted ClickHouse credentials only inside the existing support pod; it does not read the Secret API or print response bodies.

Aggregate admission counters must be isolated from other traffic during this measurement. Background traffic, a slow request window, changing replica membership, a failed request or partial durable delivery makes the check inconclusive or failed; do not relax the assertions to obtain a pass. The helper does not pause agents automatically. If a disposable lab needs a short collection pause, explicitly bound that maintenance window, record the exact affected agents, arrange restoration even on failure, and require all agents plus fresh complete canary cycles afterward. Do not copy that pause into production as an automatic test step.

For a narrow Pod Identity allow/deny probe, use the exact recorded `LabRun` tag and this lab's known buckets:

```sh
.venv/bin/python scripts/lab_pod_identity.py \
  --context "$lab_cluster" --cluster "$lab_cluster" \
  --account "$lab_account" --region "$lab_region" \
  --confirm-cluster-arn "arn:aws:eks:$lab_region:$lab_account:cluster/$lab_cluster" \
  --lab-run "$lab_run" \
  --allowed-bucket "${lab_cluster}-${lab_account}-${lab_region}-mimir-blocks" \
  --denied-bucket "${lab_cluster}-${lab_account}-${lab_region}-artifacts" \
  --output build/lab-pod-identity.json --timeout 180 --run-probe
```

The explicit `--run-probe` creates a bounded temporary Job using the existing Mimir service account and deletes that exact Job afterward. It proves credential delivery and `ListBucket` HTTP 200/403 for the assigned/unrelated lab buckets; it does not create IAM roles, read object contents, test object writes or audit all effective permissions. Credentials, projected tokens, signed headers and response bodies are never part of the report. Confirm `jobsCleanedUp: true`; a successful permission check does not excuse failed cleanup.

All three helpers require explicit account/region/context/cluster-ARN agreement and a private EKS endpoint. They save new private aggregate files and deliberately leave `qualificationGatePassed: false`. Retain each result with the source revision, actual chart/image digests, selected tuning and reviewed configuration used for that run. The separate [qualification recorder](live-qualification.md) stores operator attestations; it does not execute these probes or independently verify their claims. Never upload raw state, plans, tfvars, Secrets, pod specifications or logs as evidence.

## 2026-09-25 executed lab

The first disposable run applied 121 Terraform resources and reached a no-change plan. Six `m7i.xlarge` workers ran across three AZs. The live ClickHouse layout had two write shards with three AZ-separated replicas each; the same flood service stored 171 records on one shard and 192 on the other.

The node-agent noisy-neighbor test offered 3,000 ERROR records and stored 363 distinct records, while the quiet service stored all 30 of its 30 offered INFO records. Both canary replicas completed fresh exact logs, metrics and traces before and after. A first direct shared-quota measurement was inconclusive because of background counters. A separate approximately 73-second pause of only the six lab Vector agents isolated the burst: 300 offered, 40 admitted, 260 dropped, 40 stored, no unavailability, and a 0.222171-second request window against a conservative global bound of 46. All six agents were restored and fresh complete signals verified again. The Mimir service-account probe returned 200 for its assigned bucket and 403 for the unrelated artifact bucket, then removed its Job. All eight rollout active/preview Service hashes and endpoint selections were verified after the Helm 4 client-side-apply correction; a real promotion was not attempted.

Bootstrap did lose synthetic logs before Kafka topic and ClickHouse schema creation. The new startup gates passed local native Kafka/ClickHouse tests after this finding; **a new cold AWS installation with those gates remains pending**. Later canary success does not recover those earlier drops. This run did not perform DR, AZ/region fault injection, real rollout promotion, induced-lag autoscaling or full IAM/security qualification. See the [validation record](validation.md#2026-09-25-authorized-disposable-aws-deployment) for exact evidence hashes and limitations.

**Teardown completed at 17:58:22 UTC.** All 26 inventoried CSI data volumes totaling 1,156 GiB were deleted, and Terraform destruction removed all 121 managed resources. Independent AWS inventory found no remaining lab cluster, active instances, EBS volumes, EC2/RDS/cache snapshots, retained database backups, EIPs, NAT gateways, interfaces, VPC, load balancers, databases, caches, buckets, ECR repositories, log groups, IAM roles/policies or Secrets. The final RDS snapshot and disposable quota Secret were explicitly removed; the automated snapshot also disappeared. The dedicated bootstrap state bucket and all historical state/lock versions were deleted last after empty remote state was preserved locally. Existing SSO and AWS service-linked roles were preserved. The run lasted approximately 102 minutes and is expected to remain below the $100 target on the planning estimate; actual billing has not been verified. The approximately $1.79/hour baseline excludes the runner and variable usage.

## Teardown is a required test result

Capture exact Terraform resource IDs and Kubernetes PV/claim UIDs plus CSI volume IDs **before** destroying the cluster. The read-only `scripts/lab_inventory.py` projects approved fields into a private inventory; it neither proves cloud ownership nor authorizes deletion. Compare its IDs against the live account/region, dedicated Terraform state, cluster ARN and cloud tags. The lab is not clean while retained billable resources remain.

1. Stop synthetic producers/canaries and scheduled work. Suspend any lab GitOps reconciliation and autoscaling. Drain ingestion, Kafka consumers and trace decision/export windows where possible; record loss when a failed test prevents draining.
2. Remove the lab platform release while its operators, EBS CSI and private runner still exist. Wait for custom-resource finalizers, dependent pods and volume attachments to complete. Do not blindly remove finalizers or delete controllers first.
3. For only the inventoried lab claims/volumes, explicitly arrange deletion rather than retention, remove the claims, and verify those exact EBS volumes disappear. Alternatively delete the detached volumes by exact ID after checking ownership. Node root-disk `delete_on_termination` does not cover these data volumes. Remove lab-owned operators after their managed resources finish deletion.
4. Stop all bucket writers, enumerate and remove every object version/delete marker and abort multipart uploads in only the lab buckets. ECR image versions and private artifact objects also require explicit removal. Do not use account-wide or tag-only bulk deletion.
5. Disable RDS deletion protection on only the recorded lab DB. Review the dedicated Terraform destroy plan and remove the lab root. Production defaults still require a final snapshot; remove that exact lab snapshot afterward when all test evidence has been preserved. RDS snapshots and retained backups continue to incur charges until deleted. [RDS deletion behavior](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_DeleteInstance.html)
6. Preserve the private runner until Kubernetes cleanup completes. After infrastructure deletion, verify node groups, instances, EBS volumes, NAT gateways, EIPs, RDS/Valkey, snapshots, bucket versions, repository images and IAM resources against the initial inventory. Let AWS services delete their own requester-managed interfaces. Account for the quota Secret's seven-day scheduled deletion; record or explicitly remove the exact disposable Secret when full removal is required.
7. Verify and remove the dedicated bootstrap state bucket last if this run created it, including versions and lockfiles, after retaining only approved private evidence needed for the report. Never delete a reused state bucket, original SSO roles, shared service-linked roles or any unrelated account resource.

Record the final resource inventory and any unresolved cleanup item explicitly. Terraform's empty state alone is not proof that controller-created volumes, snapshots or other out-of-state resources were deleted.
