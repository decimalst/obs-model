# Deferred AWS qualification batch

All AWS creation, EKS execution, real receiver delivery and fault/restore testing are **deferred to the account owner**. The local command below prepares a single ordered acceptance batch and records evidence; it has no AWS, Kubernetes, shell, deployment or network execution path. A passing local test suite does not pass any of these gates.

## Prepare locally

Choose the nonsecret configuration that matches ownership:

| Starting point | Intended environment |
|---|---|
| [qualification.example.yaml](../operations/qualification.example.yaml) | Managed regional EKS and native backend backup mechanisms |
| [recovery-existing-external.yaml](../examples/recovery-existing-external.yaml) | Existing EKS, independently owned backup service, optional profiles/Hubble |
| [recovery-cross-region.yaml](../examples/recovery-cross-region.yaml) | Software-only installation with explicitly planned remote recovery |

Copy the file to a private directory outside Git. Set the actual account, region, cluster, kube context, unique `runId` and full reviewed source commit. Choose features that will actually be deployed. Set numeric recovery objectives only after their owners review them; `null` deliberately prevents claiming recovery success. Record SHA-256 digests of the exact private platform contract and rendered Helm values under `deploymentArtifacts`; managed/existing mode also requires the saved reviewed Terraform plan digest. Software-only mode may use a null Terraform plan digest. Hash those files locally without printing or archiving their contents, and keep the plan/state/values in the restricted deployment system. Zero placeholder hashes or revision cannot pass identity acceptance. The planner records these operator-supplied hashes; it does not itself read the deployment files or independently verify the remotely applied settings. `mode` describes the installation ownership; it does not provision or adopt anything.

```sh
.venv/bin/python scripts/qualification.py --config /secure/qualification.yaml --check-only
.venv/bin/python scripts/qualification.py init --config /secure/qualification.yaml \
  --run-dir /secure/acceptance/run-001
.venv/bin/python scripts/qualification.py check --run-dir /secure/acceptance/run-001
```

`plan` is the default action. `--check-only` always performs read-only local validation/planning, even if another action is supplied; it never creates the run directory. `init` creates private local files (directory mode 0700; files 0600) and fails if the directory already exists. A new run has pending required gates and explicitly skipped disabled features. `check` returns **2 until every enabled gate passes**, **1 for invalid evidence**, and **0 only for a complete operator-attested batch**. Disabling a feature limits what the run qualifies; it does not prove the feature works.

The [gate catalog](../operations/qualification-gates.yaml) contains 26 gates, ordered dependencies, required assertions and per-gate runbooks. Review it before the live window. Record account budget, fault windows, traffic/load ceilings, stop conditions, cleanup owner and human approvals in the private change record. Schedule deployment, load, fault and recovery work together when practical; prerequisites still run in order. Keep a run for each independently deployed region/environment/revision. A changed configuration or gate catalog requires a new run instead of reusing an old success.

## Record evidence after execution

When the user later runs a gate, generate its draft receipt:

```sh
.venv/bin/python scripts/qualification.py receipt-template \
  --run-dir /secure/acceptance/run-001 --gate environment_identity \
  > /secure/acceptance/environment-identity.receipt.json
```

A draft says `pending`, has null times and false assertions, and cannot be recorded. After the actual check, set `status` to `passed`, `failed` or `skipped`, real UTC `startedAt`/`completedAt` timestamps ending in `Z`, operator identity, a brief nonsecret reason, measured values and references to reviewed evidence in your private change/evidence system. Set an assertion true only when its acceptance condition was observed. Retain the configuration digest generated in the draft.

```sh
.venv/bin/python scripts/qualification.py record --run-dir /secure/acceptance/run-001 \
  --receipt /secure/acceptance/environment-identity.receipt.json
.venv/bin/python scripts/qualification.py check --run-dir /secure/acceptance/run-001
```

`record` appends a new numbered receipt; retries do not overwrite earlier results. Required skips remain incomplete. Failed assertions, missing evidence references, wrong run/environment digest, future or inverted timestamps, unmet dependencies and out-of-target RPO/RTO cannot pass. A prerequisite that later fails or is repeated invalidates its prior downstream successes: repeat those gates. Recovery measurements use `data_gap_minutes` (worst loss across enabled backends) and `recovery_minutes` (fault declaration to verified usable queries/intake); attach the per-backend breakdown separately.

These are **operator attestations**, not automatically witnessed AWS results or cryptographic audit signatures. Digests detect accidental changes; anyone able to rewrite all local run files can forge them. The operator string is attribution, not authentication. Restrict write access, have a second reviewer inspect source evidence, and archive approved receipts in the organization's access-controlled audit store. The tool never fetches or uploads referenced evidence. Do not include credentials, Terraform state/plans, Kubernetes Secrets, alert receiver configurations, raw logs, pod specs, annotation dumps or sensitive payloads. Prefer counters, timestamps, synthetic identities and approved ticket/object references without signed URLs. Review text before sharing.

## Identity and ownership

For `environment_identity`, use the future runner's AWS STS identity, EKS cluster ARN/region and explicitly selected kube context; compare these to the plan rather than trusting local context names. Compare the applied Git/chart/image digests to `sourceRevision` and recompute all applicable deployment artifact hashes against the reviewed files used for that deployment. Verify the runner reaches the private API and has exactly the intended installation/operations roles.

For `ownership`, inventory Terraform state ownership, Helm releases, controllers, CRDs, StorageClasses, Pod Identity associations, Secrets, CNI and EKS access entries. Managed mode follows [bootstrap](runbooks.md#bootstrap-and-deploy). Existing/software-only mode follows [existing environments](existing-clusters.md): supplied roles/controllers/data services stay external. Require a read-only preflight and reviewed Terraform plan; unexplained adoption, destruction, replacement or removal of a move declaration fails. Verify state locking with the chosen backend without copying state into receipts.

## Installation and security

`capacity_cost` records physical AZ IDs, usable per-AZ capacity, instance/storage/API quotas, node maximums, broker/storage limits and blue-green headroom. Use account-specific prices and budgets. Prove bulk paths use their originating region, verify S3 endpoint routes, and measure unavoidable cross-AZ replication independently of cross-region transfer.

For `installation`, the account owner executes the reviewed plan and selected installer path later. Verify all three AZ placements, encrypted PVCs and retained data, RDS/Valkey reachability, expected S3 prefixes and Pod Identity permissions from the actual service account. Test both permitted regional writes and denied foreign-bucket access. Ready pods alone are insufficient.

For `network_security` and `sso_authorization`, select the deployment's identity provider and transport owner: native OIDC, a trusted auth proxy, or an existing identity-aware ingress; mesh mTLS or service-specific TLS as supported by the actual overlay. Supply referenced Secrets and certificates out of band. Test unauthorized namespaces, anonymous access, tenant/header spoofing, wrong groups, direct-backend bypass, expired/untrusted certificates, logout and credential/certificate rotation. An external protection layer qualifies only after bypass paths are denied. If internal transport is still plaintext, the TLS gate stays pending/failed; documenting that limitation is not a pass. Existing CNI/mesh resources remain under their owners.

## Data and support

`sensitive_data` sends only approved synthetic credentials/identifiers through logs, traces and metrics; query stored signals to show the configured drop/redaction policy works and safe metrics survive. Capture transformed fixture results, not real personal data.

`stored_signals` follows [support enablement](support.md): namespace collection label, dedicated read-only canary credentials and three fresh exact signal identities. Exercise partial ingestion and a downstream outage; readiness must not mask stored-signal failure. Run RED fixtures from actual apps and document SDK sampling.

For `notifications`, request the account owner's explicit real-delivery authorization during the later test window, then use a dedicated test receiver or agreed paging schedule. Test default and owner routes, resolved notifications, secret rotation and recovery after receiver failure. A Mimir send counter is not a receiver receipt. If intentionally disabled, the batch does not qualify alert delivery.

## Load and failure

Run `log_fairness`, `metrics_trace_fairness` and `scaling` with a named loud test service and an independent normal service. Record offered, accepted, dropped/refused and stored rates plus query latency before/during/after each overload. Include ERROR storms, debug floods, cardinality churn, oversized OTLP batches and unavailable Valkey. State each limit's actual scope; tenant or per-pod ceilings must not be reported as shared per-service isolation. Measure the normal service's loss rather than inferring fairness from the loud service's rejection.

Drive Kafka lag above the configured threshold, observe KEDA writer growth and partition caps, then watch full catch-up with no offset acknowledgement before successful storage. Verify writes across actual ClickHouse shards and quorum, not just replica counts. Review partition expansion separately; adding brokers does not rebalance partitions. Keep load bounded by the approved storage/budget stop conditions.

For `blue_green` and `sampler_drain`, use the [upgrade procedure](recovery.md#upgrade-and-roll-forward). Only after the isolated backup restore succeeds may `az_loss` proceed. In the dedicated lab, the account owner selects an approved fault method and one AZ at a time. Monitor ISR, Keeper/ClickHouse quorum, Mimir/Tempo rings, sampler loss, Target Allocator reassignment, autoscaler behavior, intake/queries and actual data gaps. Stop on lost quorum or the agreed impact ceiling. Restore the AZ, prove all replicas/rings heal, and only then consider another fault. Do not automatically delete or terminate production nodes from this batch tool.

## Optional capabilities and handover

For `gitops`, change a representative Vector pipeline and its fixtures, dashboard, rule and catalog entry through a PR. Observe controller revision, rollout, stored results and bounded drift repair. Test suspend/resume ownership during a stateful maintenance exercise; preserve release names and controller-owned fields.

`debug_expiry` tests the protected workflow with an authorized user and a denied actor/foreign cluster/workload, malformed timestamps, the 24-hour bound, a replaced pod and expiration while cleanup is stopped. Test actual collection rather than annotation removal alone.

Hubble and profiles use [their independent acceptance steps](ebpf.md). Service graphs require cross-node client/server pairing, cache/label limits, orphan visibility and results before tail sampling; missing SDK spans remain missing. Flink acceptance requires the actual job image and connector semantics described in [Flink](flink.md).

Finish with [isolated restore, roll-forward and regional recovery](recovery.md), review every enabled gate, publish only reviewed receipts, and leave unresolved results pending/failed. Handover identifies on-call owners, recurring checks, retention obligations, tested capacity, known data loss and approved cleanup. No Terraform destroy, irreversible restore or real notification is triggered by this tooling.
