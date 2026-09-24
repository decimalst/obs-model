# Observability platform in a box

An AWS regional observability platform designed to contain noisy log producers. Vector handles logs; OpenTelemetry handles metrics and traces; Grafana provides a common investigation interface. Terraform builds independent three-AZ regions, and Helm installs the data plane and its controllers.

**The principal invariant: an error storm still consumes a service's log budget.** Adding collector replicas must not multiply that budget. A small admission service uses atomic Valkey token buckets between Vector agents and gateways. Quotas count events **and bytes**, and temporary debug collection receives a smaller sub-budget.

```mermaid
flowchart LR
  Apps[Applications] --> VA[Vector node agents<br/>identity + local throttle + debug expiry]
  VA --> Q[HA log admission<br/>shared per-service quota]
  Q <--> R[(Regional Multi-AZ Valkey)]
  Q --> VG[HA Vector gateways]
  VG --> K[(Kafka RF=3 / min ISR=2)]
  K --> VW[Vector consumer group<br/>KEDA lag scaling]
  VW --> CH[(Sharded + replicated ClickHouse + Keeper)]
  CH --> EV[Derived event aggregates]
  Apps --> OA[OTel node agents]
  TA[Target Allocator] --> SP[Sharded scrape pool]
  SP --> MG[Metrics gateways]
  OA --> MG
  MG --> M[Mimir + regional S3]
  OA --> RT[Trace-ID routing]
  RT --> TS[Fixed tail-sampling pool]
  TS --> T[Tempo + regional S3]
  CH --> G[Grafana + Multi-AZ PostgreSQL]
  M --> G
  T --> G
```

The [reference diagram](docs/reference/architecture.png) is preserved as input evidence. [Architecture and tradeoffs](docs/architecture.md) explain implementation differences. This is a tested implementation baseline, **not a claim of production or disaster-recovery certification**. See the precise [validation record](docs/validation.md).

## What is included

- Independent Terraform roots for `us-east-1` and `us-west-2`: private EKS 1.35, six initial on-demand nodes per region across three AZs, regional NAT, S3 gateway endpoint, encrypted storage, Pod Identity, Multi-AZ PostgreSQL and Valkey, cluster autoscaling, ownership tags, and separate locked state.
- Pinned Helm charts for operators, Kafka, ClickHouse/Keeper, Mimir, Tempo, Grafana, Vector, OTel agents/gateways, and scrape allocation. A separate namespace-scoped debug-grant controller propagates grants to replacement pods without restarting applications.
- Blue-green, manual promotion, and readiness analysis for log admission, Vector gateways, trace routers, and metrics gateways. Fixed trace samplers and quorum stores use state-aware upgrade procedures.
- Tests for real collector configuration, debug expiry, replica-independent shared quotas, error storms, byte budgets, request/concurrency bounds, CRD schemas, Terraform invariants, and a local Kafka-to-ClickHouse log pipeline.
- CI, an image-publishing workflow, debug grant/revoke automation, deployment scripts, regional value generation, guarded secret seeding, dashboards, and runbooks.

## Validate locally

Prerequisites: Python 3.11+, Go 1.25+, Node 24 with npm, Terraform 1.10+, Helm 3.16+ (tested with Helm 4.2.3), Docker, OpenSSL, and kubeconform 0.7.0. Install kubeconform with `go install github.com/yannh/kubeconform/cmd/kubeconform@v0.7.0`.

```sh
make setup deps
make check
make terraform-init terraform-test
make runtime-test
make image integration-test
make collector-test rules-test sharding-test consumer-test
make red-test dashboard-test profiling-test studio-terraform-test
```

Checks create no AWS resources. Terraform tests use a mocked AWS provider for plan/apply assertions; they never apply against a live account. The integration test creates and removes only containers and a network bearing its unique test identifier. Its one-broker/one-database topology verifies the data path, not three-AZ failover.

## Deploy a region

Already have EKS? Use the [existing-environment guide](docs/existing-clusters.md): bring an existing cluster and IAM roles, or reuse all AWS data services and install only the software. Controller and storage-class reuse are explicit, with a read-only preflight command before installation.

```sh
.venv/bin/python scripts/install.py --config build/existing.json \
  --quota-image REGISTRY/log-quota@sha256:YOUR_PUBLISHED_DIGEST --check-only
```

Remove `--check-only` to install after prerequisites pass. The configuration can be Terraform output or the [software-only example](examples/existing-platform.yaml). Supply real AZs; the installer no longer assumes `a/b/c`.

For a new region, follow [the runbook](docs/runbooks.md). Terraform planning/applying is a separate operator action. A private runner, AWS account, state bucket, administrative role, and published quota image digest are required. The installer checks the actual API endpoint and cluster identity and uses existing AWS/Kubernetes secret mechanisms; it never embeds real credentials in example values.

Start with one region, pass live acceptance, then repeat for the second. Bulk telemetry stays in its originating region. There is no automatic cross-region ingestion failover or raw-data replication by default.

## Enable debug collection for eight hours

Applications must emit structured JSON with `level` or `severity`, and their pods need a stable `observability/service` label. Label the application's namespace `observability/collect=true` to opt it into collection. The grant enables **collection** of emitted debug logs; it does not change an application's logger level.

```sh
helm upgrade --install debug-grants charts/debug-grants -n applications
python3 scripts/debug_grants.py grant --namespace applications --name checkout --hours 8
python3 scripts/debug_grants.py revoke --namespace applications --name checkout
```

The collector independently checks both grant timestamps against its clock, rejects malformed/future/overlong grants, and stops debug collection at expiry. The controller refreshes new pods every minute. See [debug grants and quota semantics](docs/quotas.md).

## Default budgets

| Control | Default | Scope |
|---|---:|---|
| Early log throttle | 500 events/s | Service per node; all levels |
| Log line limit | 32 KiB | Individual and merged lines; oversized lines dropped |
| Shared log quota | 1,000 events/s, 1 MiB/s | Service per region |
| Burst allowance | 2,000 events, 2 MiB | Service per region |
| Debug sub-budget | 100 events/s, 100 KiB/s | Within the service total |
| Debug collection | Off; grants up to 24 h | Workload, enforced at collection |
| Trace baseline | 10% | Tail-sampling candidates |
| Trace export ceiling | 2,000 spans/s | Sampler; three fixed samplers |
| Scrape interval / limit | 30 s / 10,000 samples | Per target |
| Retention | Logs 7 d; Kafka 24 h; metrics 15 d; traces 72 h | Regional backend |

Tune these using measured workload size. Error/slow trace preference is bounded, not a promise to preserve every error. Quota drops are intentional, counted loss. Buffer saturation, replay, failover, and upgrade limitations are documented rather than hidden.

## Pipeline hardening and operations through Git

Use `scripts/install.py --tuning examples/tuning.yaml` to override budgets, sampling, queues, cardinality and retention at deploy time. Arrow is enabled on supported collector links; `collectors.interconnect: otlp` is the explicit fallback. The shared Mimir series-drop policy also protects the scrape and OTLP paths without merging distinct counters.

[GitOps setup](docs/gitops.md) exports regional Flux releases so PRs reconcile pipelines, dashboards, saved queries, alert and recording rules. [Scaling operations](docs/scaling.md) covers each stage and [the adversarial review](docs/adversarial-review.md) explains fixes, quota scope and remaining limits. Run `make collector-test rules-test` for live Arrow/routing/cardinality and alert behavior tests, in addition to the existing checks.

Application metrics must identify their writer (`service.instance.id`, `k8s.pod.uid` or `k8s.node.name`) to avoid merging replicas. Use the [SDK identity fragment](examples/sdk-identity.yaml) with your application's SDK; namespaces must opt in with `observability/collect=true`. Anonymous-writer points are dropped by default and filter-drop telemetry makes that visible.

ClickHouse defaults to **two shards × three AZ-separated replicas**. It distributes a busy source cluster across shards and queries them through one table. Increase `clickhouse.shards` using the [pause/expand/resume runbook](docs/clickhouse-sharding.md); historical data stays in place and remains queryable. Run `make sharding-test` for replicated storage, failure and expansion tests.

Kafka logs start with **36 partitions and 3–12 consumers**, scaled by queue lag through KEDA. Raise `writerAutoscaling.maxReplicas` through a PR to use the partition headroom; tune the lag target independently. Writers share one consumer group, drain gradually during scale-down, and commit offsets after ClickHouse acknowledgement. See [Kafka consumer operations](docs/kafka-consumers.md) and run `make consumer-test`.

## Service RED and remaining platform work

The **Service RED — observed traffic** dashboard shows request rate, error ratio and p50/p95/p99 latency by region, cluster and service. Node agents derive bounded metrics before trace sampling and export them over Arrow to the metrics gateway. Server spans count by default; raw span names/URLs/IDs do not become labels. See [the RED guide](docs/service-red.md), [SDK onboarding fragment](examples/sdk-identity.yaml), and [remaining platform gaps](docs/platform-gaps.md). Run `make red-test rules-test dashboard-test` to verify the pipeline, queries and Grafana provisioning. Full coverage requires application instrumentation and spans reaching the agent; head sampling and telemetry loss affect the result.

## Configuration studio, service catalog and eBPF

Open the [configuration studio](site/index.html) through a local HTTP server, or publish its static allowlist with the manual GitHub Pages workflow. Its seven-step flow selects new/existing EKS, reviews AWS IAM ownership, sets budgets and visibility, configures access and operations, then previews the downloadable files. Nothing applies or publishes automatically. See [studio setup and Pages publication](docs/configuration-studio.md).

The AWS IAM step separates the existing Terraform provisioning identity, the EKS administrator grant and workload role creation/reuse. Each role shows its trust, permission scope and workload binding, and downloads include `IAM.md` and `IAM-PLAN.json`. New regions create seven roles by default or eight with profiling, minus reused workload roles; software-only installations create none. Read the [IAM inventory and owner setup](docs/iam.md) for permissions, external identities and the account-scoped trust boundary.

The studio also exposes security/SSO profiles, privacy filters, bounded service graphs, regional cell plans and recovery/qualification drafts. Inline guidance explains ownership and unfinished setup, and full request import/export preserves those selections. Live AWS acceptance remains deferred; a downloaded draft does not run any of its checks.

Register applications in the [Git-owned service catalog](docs/service-catalog.md), or generate a Go service with telemetry, tests and an application chart. Grafana receives the service directory and optional availability burn alerts through the same GitOps flow. Repository creation is a separate explicit publish operation; the public site never carries private catalog contents or GitHub credentials.

[Network visibility and CPU profiling](docs/ebpf.md) are separate opt-ins: externally managed Cilium Hubble supplies network metrics/UI, while labeled Alloy eBPF collectors send CPU profiles to regional Pyroscope storage. Managed AWS installs can provision its bucket and Pod Identity role. Privileged kernel collection, CNI migrations, distributed profile ingestion and AZ/restore behavior still require live environment qualification.

Run `make studio-test service-test profiling-test` for these additions, alongside the existing required checks.

## Operational support

The [support guide](docs/support.md) covers the stored-signal canary, Git-reconciled owner/default alert routes using existing Secrets, verified backend scrapes and capacity dashboards, private local support inventories, and catalog-owner debug requests. Canary generation and tenant alert ownership require explicit opt-in; no receiver credentials or environment approvals are invented. Run `make support-test` for native canary, notification and exporter checks.

Upgrading to 0.6 restores safe scraped metrics that the previous combined cardinality rule dropped. Convert custom rules to one forbidden label per rule and review the restored metric volume. The guide includes migration and independent health-monitoring requirements.

## Configuration and deferred AWS qualification

Start with the [setup options](docs/setup-options.md) for local/OIDC access, existing TLS ingress or proxies, external mesh ownership, privacy policy, service graphs, independent cells and recovery choices. Release 0.7 adds validated security profiles, opt-in native-tested redaction and pre-tail service graphs, an offline cell budget preview, and `install.py --render-only` with no AWS/Kubernetes calls. Settings flow through the same regional installer and GitOps compiler.

All real AWS work is collected in the [26-gate qualification batch](docs/live-qualification.md), with an offline evidence ledger and native/external backup setup. Leave live gates pending until the later account rehearsal. Existing resources, IdP/mesh ownership, credential creation, restore execution and real notification tests are never performed by local validation. Full data-plane TLS/authenticated tenancy still requires the documented per-hop integration and acceptance work.
