# Operations through pull requests

Git is the desired state for pipelines, dashboards, saved queries, recording rules and alert definitions. Flux watches this repository and reconciles chart resources and configuration drift. Each region has its own cluster path and releases. Terraform remains a separate reviewed plan/apply boundary; Flux never changes Terraform state or runs cloud applies.

## Onboard a region once

1. Complete the existing-cluster or new-cluster installer, its preflight checks, secret seeding, schema creation and smoke test. Flux adoption intentionally uses the same Helm names (`operators`, `olly`) and storage namespaces. Do not run two independent deployment writers afterwards.
2. Export nonsecret desired state, using the same image digest and deployment contract:

   ```sh
   .venv/bin/python scripts/gitops_export.py \
     --config build/install/platform.json \
     --quota-image YOUR_REGISTRY/log-quota@sha256:YOUR_DIGEST \
     --tuning examples/tuning.yaml \
     --output gitops/clusters/us-east-1
   ```

3. Review and commit the generated directory to your shared infrastructure repository. The exporter refuses to overwrite an existing directory. Subsequent PRs edit `platform/release.yaml` values directly. Secrets remain existing Kubernetes Secrets; arrange their refresh through your organization's secret manager. Never commit generated secret values.
4. With an organization-approved Flux CLI, bootstrap the actual repository and branch into this cluster:

   ```sh
   flux bootstrap git --url=ssh://git@YOUR_GIT_HOST/YOUR_ORG/YOUR_REPO \
     --branch=YOUR_PROTECTED_BRANCH --path=gitops/clusters/us-east-1
   flux get kustomizations -A
   flux get helmreleases -A
   ```

   Bootstrap creates the `flux-system/` entry referenced by the generated root Kustomize file. The source must include `charts/` and the region path. Use a read-only deploy key for ongoing pulls and branch protection requiring the validation workflow. Cluster credentials and Git credentials are not arguments to our exporter. The exporter itself performs no network or cluster mutation.

Existing Flux users can reference `sync.yaml` from their current root. Its default source is `GitRepository/flux-system` in `flux-system`; change that reference consistently if your source has another name. Existing controllers marked `false` in the deployment contract emit no CRDs and remain externally owned. Controller CRDs reconcile before operators, then the platform. Deleting a region's sync object does **not** automatically uninstall its data platform (`prune: false`); use the teardown runbook.

## Daily changes

| Change | Edit in the PR | What reconciliation does |
|---|---|---|
| Vector normalization or pipeline routing | `charts/platform/files/normalize.vrl`, `templates/vector-config.yaml` and relevant tests | Config checksum triggers agents and gateway/writer updates; regional quotas remain in the path |
| OTel pipelines or sampling | `templates/otel-config.yaml`, `_helpers.tpl`; region release values | Updates gateways; sampler templates change but `OnDelete` requires a trace-drain operation |
| Budgets, retention, capacity | Region `platform/release.yaml` `spec.values` (start from `examples/tuning.yaml`) | Helm reconciles typed controls; see stateful exceptions below |
| Dashboards | `charts/platform/files/dashboards/*.json` | Mounted files refresh; Grafana's file provider polls every 10 seconds |
| Saved PromQL, TraceQL or SQL | `charts/platform/files/queries/catalog.yaml` | Rebuilds the **Saved query library** dashboard; users can inspect/copy/run each panel's query |
| Alert or recording rules | `charts/platform/files/rules/*.yaml`, `tests/rules/*.test.yaml` | All ruler replicas mount the same tenant `platform` rule source and poll every minute |

Chart reconciliation tracks **Git revisions**, so chart version bumps are not required for every pipeline or dashboard edit. CI validates Flux schemas and renders all checked-in regional releases; default and expanded-capacity platform custom resources are checked against pinned operator schemas; default CI also executes real Arrow, Vector, quota and log-storage tests. Add representative input/output fixtures when changing pipeline semantics. Dashboard JSON structure and query references are tested; browser rendering and arbitrary SQL/TraceQL semantic correctness still need acceptance checks for new content.

GitOps exports default to automatic promotion after the existing three preview-readiness checks; CI and PR review are prerequisites. `--manual-promotion` preserves the initial installer's manual promotion policy. Readiness is not a live delivery test. For changes requiring workload-specific acceptance, choose manual promotion and follow the smoke-test runbook. Quota changes can backpressure briefly while the previous policy expires; provision enough agent disk buffer.

Flux corrects drift except replica counts on the named HPA/KEDA-managed workloads and Argo's generated Service selector hash. This prevents reconciliation from fighting those controllers. UI dashboard edits are disabled, dashboard removals follow Git, and the Mimir ruler write API is disabled in favor of Git-mounted rules. This baseline provides OSS query panels, not Grafana Enterprise saved-query APIs.

## Alert delivery and migrations

Rules now evaluate automatically in Mimir. Configure a regional Alertmanager receiver, credentials and contact routing before relying on paging. No recipient or external webhook is invented. Grafana unified alerting remains disabled to avoid a second evaluation plane. Notification routes may be managed through reviewed chart values and existing secret mounts; secret material stays outside Git.

**Existing Mimir installations:** export API/S3-managed rules into the Git files before adopting this version. Switching to local rule storage stops evaluating rules left only in the old bucket. The bucket is retained for migration/rollback; this change does not delete it. All current ruler replicas use the same projected ConfigMap; the existing sharding ring distributes evaluation.

**ClickHouse sharding:** fresh installs use two shards × three replicas. The post-upgrade schema Job provisions the distributed/local tables. Pre-0.3 installs must follow the [legacy migration](clickhouse-sharding.md) first. Shard expansion uses three PRs (pause, expand, resume); CI blocks reductions and unsafe expansion sequences.

**Kafka consumers:** a regional PR can raise `writerAutoscaling.maxReplicas` or change the lag threshold. The default 36 partitions leave room above the initial ceiling of 12. KEDA owns the writer HPA, and database maintenance pauses it at zero. CI rejects Kafka partition decreases. See [consumer operations](kafka-consumers.md) for expansion, broker reassignment and adapter ownership.

**Existing ClickHouse retention:** `CREATE TABLE IF NOT EXISTS` does not modify an existing table's TTL. A retention change requires reviewed `ALTER TABLE ... ON CLUSTER ... MODIFY TTL ...` for both local tables (`logs_local` and `log_events_local`). Keep schema migrations explicit and forward-compatible; Git reverting a chart cannot reverse stored data or storage formats.

For a rollback, revert the configuration commit and watch Flux plus rollout status. For a failed stateful migration, use the component-specific recovery procedure; automatic Helm uninstall/remediation is disabled. Emergency changes require suspending the relevant HelmRelease, documenting the change in Git, then resuming it. Avoid suspending the whole observability platform for an isolated dashboard edit.

References: [Flux revision reconciliation and drift detection](https://fluxcd.io/flux/components/helm/helmreleases/), [Flux dependency ordering and pruning](https://fluxcd.io/flux/components/kustomize/kustomizations/). Schema fixtures are pinned to helm-controller **v1.4.1** and kustomize-controller **v1.7.3**, using their official `config/crd/bases/` sources. Deploy matching or compatible Flux API versions (`helm/v2`, `kustomize/v1`).

The 0.4 RED feature ships a file-provisioned `service-red` dashboard and a separate `red.yaml` rule group. `spanMetrics.enabled: false` removes the dashboard and collector branches and leaves an empty projected rule file for safe disabling. Agent configuration checksums trigger a rolling DaemonSet update. Histogram bucket changes need the coordinated migration described in [the RED guide](service-red.md), since mixed bucket sets affect aggregate quantiles.
