# Operating and supporting a regional cell

The support increment provides stored-signal checks, tenant alert routing, backend scrapes, a capacity dashboard, a read-only support inventory and owner-scoped debug requests. It does not replace deployment qualification, identity infrastructure or a recovery rehearsal.

## Enable stored-signal checks

The small two-replica `olly-support` deployment runs in the platform namespace, with AZ/host spread, no Kubernetes API token, a read-only filesystem and the pinned Python runtime. Canary generation is **off until explicitly enabled** because the namespace owner must opt the platform namespace into log collection. Keep that label under the owner's Git control; the installer verifies it and does not adopt or relabel the namespace.

```yaml
# Namespace metadata managed by its owner:
metadata:
  labels:
    observability/collect: 'true'
```

```yaml
# Pipeline tuning file:
support:
  enabled: true
  intervalSeconds: 120
  canary:
    enabled: true
    secretName: clickhouse-canary
    deadlineSeconds: 90
    pollSeconds: 5
```

Provision a separate existing `clickhouse-canary` Secret with a `password` key in the platform namespace through your secret-management process. Do not reuse the ingestion password. Enabling the canary adds a dedicated read-only ClickHouse user on each shard replica, with server-enforced scan, memory, result and execution-time limits. The support pod mounts only this credential; with canaries disabled it mounts no ClickHouse credential. Existing-cluster preflight checks the Secret's key names without reading its values. The operator must reconcile the user/profile before canary reads can succeed.

Every replica emits one INFO log, one metric point and one synthetic ERROR server span per interval. The log follows stdout → node Vector → shared admission → Kafka → writer → ClickHouse. Metrics/traces enter the node-local OTel service and follow the existing regional pipelines. The agent checks the exact log marker, metric value and trace ID at the query endpoints; an HTTP 200, an old sample or an unrelated trace cannot pass. Partial OTLP rejection fails the corresponding check. Query timeouts, response sizes and ClickHouse scan/time budgets are bounded. The replica's Pod UID identifies the writer; unique probe IDs are never metric labels.

Synthetic errors consume the same bounded error-sampling budget as other services. A missing trace can indicate a sampling/drop budget or overload, not total trace loss. Exclude `olly-canary` when assessing business error rates. Deadline tuning must allow the tail decision wait and polling, while remaining shorter than the probe interval. This canary verifies three signals; CPU profile capture and flamegraph queries still require the profiling acceptance procedure.

`/metrics` exports the last result and last-success time for each enabled check. `/health` returns 503 on failed/stale checks or when no checks are enabled. Monitor each support pod's health from an **independent** monitoring system with access to the private endpoint. The Service load-balances replicas, so a single successful Service request does not prove every replica is healthy. Mimir-hosted alerts cannot reliably report a complete Mimir outage. `/ready` remains available during dependency failures so Kubernetes does not hide the diagnostic endpoint; it is not a telemetry acceptance test.

## Reconcile alert delivery through Git

Provision an existing `alert-receivers` Secret through your normal secret process. Its `default-url` and optional owner keys contain HTTPS URLs for receivers accepting the **Alertmanager webhook JSON format**. These URLs may contain credentials; never put them in a tuning file, Git, a support bundle or an issue. This is not a Slack/Teams-specific payload adapter.

```yaml
notifications:
  enabled: true
  manageTenantConfig: true
  secretName: alert-receivers
  defaultReceiverKey: default-url
  owners:
    payments: payments-url
    platform: platform-url
```

Enabling `manageTenantConfig` explicitly gives this release ownership of the **entire `platform` tenant Alertmanager configuration**. Export and securely preserve any existing configuration before enabling it. It replaces routes/receivers in that tenant, including manual API edits, on each reconciliation interval. It does not merge an externally managed tenant configuration. Leave it disabled to retain external ownership.

The support workers read the mounted configuration and receiver Secret each interval, group notifications by region/cluster/owner/service, route known owners to their receiver and fall back to the default receiver. Redirect following is disabled. The Mimir API receives the rendered configuration in memory and persists it in its encrypted regional alertmanager store. Internal API transport still requires the environment's TLS/authentication hardening; the public receiver uses HTTPS. Secret rotation propagates after Kubernetes refreshes the volume and the next reconciliation cycle. Disabling the reconciler **does not erase the last stored routes**: migrate/delete tenant configuration explicitly when handing ownership back.

Reconciliation success proves that configuration was accepted, not that a person was paged. Test a deliberate alert through the regional ruler, Mimir Alertmanager and your actual receiver; verify receipt, routing and resolution. Local `make support-test` verifies the API and deliveries to a disposable local receiver only. Support failures have alert rules, but an independent external monitor is still required for a broken notification system.

## Platform support and capacity dashboard

The Git-provisioned dashboard uses native metrics, not invented exporter names:

- Stored-signal results and last-success age; backend scrape health.
- Kafka consumer lag and under-replicated log partitions from the pinned Strimzi Kafka exporter, restricted to `logs` and `clickhouse-writer-v1`.
- Collector retry-queue utilization, compared per exporter before finding the maximum.
- ClickHouse physical insert/delay rates, readonly replica counts and replication queues. Insert counts include physical operations across tables/replicas, not unique event identities.
- Tempo span ingress and Mimir Alertmanager notification attempts. Attempts do not prove delivery.

Mimir, Tempo and ClickHouse now have explicit bounded pod scrape jobs; the Kafka exporter is declared on the existing Strimzi Kafka resource. This adds observability, not another Kafka consumer or scaling controller. Missing data is unknown, never a fabricated zero. This increment does not yet provide queue age, every backend query-limit panel, per-shard disk forecasting or measured dollar-cost forecasts.

ClickHouse adds its native Prometheus listener on port 9363. Apply through the existing operator, one replica at a time, verifying quorum throughout; there is no data-schema, shard or retention change. The unauthenticated metrics listener stays within the trusted platform namespace and its NetworkPolicy. Do not expose it publicly.

### Scrape cardinality migration

The previous combined drop rule used an empty separator. The pinned Prometheus receiver defaulted that separator, causing even safe scraped samples to match the drop rule. `up` remained healthy because scrape-health series bypass metric relabeling. The policy now has **one source label per drop rule**, using `separator: ';'` and `regex: '.+'`. Mimir and all edge configurations share this policy. Existing custom grouped policies must be split before upgrade; the schema rejects them. This preserves counter identity and does not aggregate by deleting labels.

Native regression tests verify safe samples actually reach remote storage and identifier-bearing samples, including a semicolon-only identifier, are dropped. Expect restored scrape volume after this fix; review metric ingestion/capacity budgets before production promotion.

## Collect a local support inventory

```sh
.venv/bin/python scripts/support_bundle.py \
  --context YOUR-EXPLICIT-CONTEXT --namespace observability \
  --output build/support-incident-001
```

The command performs only reads and writes to a new private local directory. It projects resource names, health conditions, counts and restart totals; it excludes Secrets, pod specs, environment variables, logs, event messages and annotations. Failed reads are marked unavailable without copying command error bodies. Files have checksums. Names and topology are still internal information: review the inventory before sharing. Nothing is uploaded automatically. This is a first-pass health inventory, not a full forensic collection or proof that a backend is healthy.

## Owner-scoped debug requests

Register the service in the catalog and add exact actor/environment bindings under its owner in `operations/debug-access.json` through a reviewed PR:

```json
{
  "version": 1,
  "owners": {
    "payments": [
      {"actor": "YOUR-GITHUB-LOGIN", "region": "us-east-1", "cluster": "existing-eks", "namespace": "shop"}
    ]
  }
}
```

The default policy is empty and denies requests. The debug workflow runs only from the default branch and checks the trusted `github.actor` against catalog ownership and the exact region, cluster and namespace **before** obtaining AWS credentials. It verifies the workload's service label and touches only that workload and its owned pods. Successful requests emit a structured audit record in the workflow log, including actor, owner, target and expiry. Retain/export workflow logs under your organization's audit policy; they are not an immutable audit ledger.

Protect the default branch, workflow and policy paths, require environment approvals where appropriate, scope the operations role and Kubernetes RBAC, and trust only the configured private runner. The CLI's `--actor` argument is not authentication; this is a workflow authorization layer, not a substitute for AWS/Kubernetes authorization. The existing administrative `debug_grants.py` remains available for trusted operators. Runtime grant expiry is unchanged and independent of cleanup. The workflow now accepts the actual cluster name instead of constructing one from the region.

## Remaining qualification

No cloud resources or live notifications were changed during implementation. EKS scheduling, operator reconciliation, live stored-signal verification, external health polling, actual receiver credentials, AZ failure, restore and upgrade rehearsals remain deployment gates. Full SSO/intake authentication, internal TLS, general sensitive-data redaction, inferred service graphs and shared per-service fairness for non-log signals remain separate work.

References: [Mimir configuration API](https://grafana.com/docs/mimir/latest/references/http-api/), [Alertmanager webhook configuration](https://prometheus.io/docs/alerting/latest/configuration/#webhook_config), [Tempo trace API](https://grafana.com/docs/tempo/latest/api_docs/), [ClickHouse server metrics configuration](https://clickhouse.com/docs/operations/server-configuration-parameters/settings#prometheus). Pinned binaries and executable fixtures are the compatibility checks for this release.
