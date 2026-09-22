# Validation record

Local validation performed on 2026-09-21–22, on macOS arm64 with Linux containers in Docker. This record distinguishes executed checks from deployment configuration and future acceptance work.

## Executed checks

| Check | Result |
|---|---|
| Terraform formatting and validation | Pass: both modules and all 3 roots, including existing EKS |
| Terraform mocked plan tests | Pass: 16 tests across the regional module, shared data-services module and existing-cluster root; includes role/association ownership, subnet/VPC/AZ selection and invalid inputs |
| Helm dependency resolution and strict lint | Pass: all 4 charts, with committed dependency locks |
| Python tests | Pass: 59 tests for grants, ownership, secret preservation, regional coordinates, schemas, routing, scheduling, network-policy contracts and existing-environment installation |
| Python lint/format, shell syntax, Go vet | Pass |
| Kubernetes native schemas | Pass: 387 rendered resources; 329 valid, 0 invalid, 0 errors, 58 skipped because native schemas are unavailable |
| Custom resources | Pass: all 12 platform CRs checked separately against the packaged pinned CRDs; optional Flink deployment also schema-validated |
| Vector configuration | Pass: all 3 configurations validated by Vector 0.54.0 |
| OpenTelemetry configuration | Pass: all 5 configurations (including the scrape pool) validated by Collector Contrib 0.148.0 |
| Backend configuration | Pass: Tempo 2.9.0 verification and Mimir 3.2.0 validation for 9 distributed targets, including the chart's Alertmanager zone argument |
| Actual Vector transform tests | Pass: 8 scenarios covering errors, debug/trace defaults, valid/expired/malformed/future/overlong grants, trusted identity, and grant-field removal |
| Go tests with race detector and live Valkey | Pass: 7 top-level tests plus HTTP subcases; shared quotas, byte/debug budgets, malformed/oversized requests, saturation, store outage, graceful in-flight shutdown, mixed-policy expiry and backwards-clock behavior |
| Workflow syntax | Pass: actionlint 1.7.11; hosted workflow execution has not occurred |
| Arrow / metrics / trace integration | Pass: real pinned collector containers transmit metrics and traces over Arrow (OTLP downgrade disabled); reject identifiers, raw URL/SQL attributes, excessive label count and missing writer identity; preserve two writer identities; keep 48 traces/96 error spans on consistent destinations across 3 production tail samplers |
| Alert and recording rule behavior | Pass: pinned Prometheus 3.5.0 validates 10 rules and evaluates sustained-overload/healthy platform fixtures plus RED aggregation, latency quantiles, healthy/idle/missing traffic and overflow fixtures |
| GitOps and content | Pass: Flux HelmRelease/Kustomization pinned schemas, revision tracking, scoped drift exceptions, operator exclusion, Kustomize builds, mounted Grafana/query content and Mimir rule source |
| Quota image build and log integration | Pass: the rebuilt image completed the Vector → admission → Vector → Kafka → Vector → ClickHouse/Keeper path, using the production SQL, log transforms and rendered quota integer environment values |
| Replicated ClickHouse sharding | Pass: 2 shards × 3 replicas; one hot service across both shards for two clusters; query/aggregate totals; quorum writes after replica loss; expansion to 3 shards without losing historical reads; legacy-schema rejection and failure on an unavailable whole shard |
| Kafka consumer capacity | Pass: 36 partitions; 12,000 queued records; consumer group 1 → 3 → 2; full partition assignment/commit; no commits during paused ClickHouse; 22,800 event identities and aggregate total across 2 shards |
| Scaling and migration safeguards | Pass: pinned KEDA schema, HA placement, namespace access, exclusive scaler ownership, partition caps, maintenance pause, existing adapter reuse/conflict rejection, explicit CPU fallback, shard/partition decrease rejection and prior-pause expansion checks |
| Service RED pipeline | Pass on 2026-09-22: two agent writers → Arrow → production metric gateway/remote write → Prometheus; 8 server calls, 4 errors, 8 histogram observations summing 2.62 seconds; excludes client/internal/missing-service spans, preserves original traces, survives 0% healthy tail retention, and exposes route-cardinality overflow |
| Grafana RED provisioning | Pass on 2026-09-22: Grafana 12.3.1 loads the provisioned dashboard and variables, restricts Viewer edits and reloads a changed file without restart; browser rendering and live datasource wiring are not asserted |

The shared-quota test sends 200 error events concurrently through two admission instances and admits exactly 100 from their shared burst budget. An independent quiet service still receives admission. The test finishes within a refill interval and fails if it does not, so the expected count cannot hide a refilled budget.

Existing-environment tests use synthetic AWS/Kubernetes inventories and real Helm rendering. They verify read-only preflight behavior, wrong-cluster and cross-region rejection, mismatched Pod Identity associations, preservation of existing secrets, controller/StorageClass ownership, propagation of non-`a/b/c` AZs and custom storage classes, stale-overlay rejection, and that disabled controllers produce no resources or bootstrap CRD writes. Terraform role-reuse tests assert zero IAM role/policy resources for externally supplied roles. No existing EKS cluster was contacted or changed, and no live Terraform state migration was attempted.

The integration test stores exactly 5 flood/error records, 1 quiet-service record, and 1 valid debug record. It stores no expired debug record. The derived aggregate totals 7, and admission metrics report 7 accepted and 15 dropped. The topology has one broker, database, and Keeper; it verifies the log path, not multi-AZ failover. The final local quota image ID is `sha256:746b256a24a47de2cc961c0c6e5125b1b899226c6b74f22d193073331d810d16` (a local arm64 image ID, not a published registry digest). Test containers and their network were removed.

The sharding fixture uses one Keeper and up to nine ClickHouse data containers. The consumer fixture uses one Kafka broker, one Keeper and two single-replica ClickHouse shards; Docker changes the writer replica count explicitly. These tests prove storage/consumer behavior under controlled local conditions, not AZ failover or live KEDA/HPA decisions. No loss or duplicates occurred in the measured fixtures; delivery remains at least once.

Tool versions used: Terraform 1.15.8, AWS provider 6.66.0, Helm 4.2.3, Go 1.25.8, Python 3.14.7, kubeconform 0.7.0. Container and chart versions are recorded in their source manifests and lock files. The quota image's build and runtime bases are pinned by digest. Other version-tagged upstream images should also be resolved to immutable digests under a production release process.

The 2026-09-22 RED work reran Terraform validation/16 mocked tests, all chart/unit/native configuration checks, Arrow/trace-affinity integration and rule fixtures. The storage/quota image-build and Kafka/ClickHouse behavioral results above remain the executed 2026-09-21 evidence; those components were not changed by RED generation. The RED test substitutes Prometheus for Mimir and files for retained traces; the Grafana test verifies provisioning with placeholder datasources. Spanmetrics is alpha in the pinned collector release and needs version-specific requalification.

## Scope and remaining acceptance

No AWS infrastructure has been applied and no repository or image has been published by this work. Local runtime tests use disposable containers. Native/CRD validation does not execute Kubernetes controllers, admission webhooks, scheduling, or cloud APIs. Mimir/Tempo configuration validation does not prove ingestion, queries, or S3 access. The collector test substitutes file exporters for those backend sinks; it proves transport, filtering, affinity and tail decisions, not storage integration for metrics/traces.

The remaining acceptance gates include KEDA/HPA scaling from induced lag, scaler fallback and maintenance pause; Helm schema-hook execution and operator-driven shard expansion; live Flux reconciliation/drift correction, Grafana dashboard/query rendering and Mimir rule-file reload; actual EKS deployment and operator reconciliation; IAM/Pod Identity and network policies; OTLP metrics/traces through to Grafana; live autoscaling and blue-green promotion/rollback; scrape assignment behavior during allocator changes; Valkey and three-AZ quorum failure; backup restore; regional recovery; and measured cost/throughput. See [testing.md](testing.md) for procedures. No measured production capacity, RPO, RTO, or dollar cost is claimed.

Flink is an optional operator/deployment integration with savepoint and regional checkpoint configuration. No arbitrary application job or connector is supplied; its runtime behavior still requires a real job and job-specific tests. The included ClickHouse materialized view implements basic derived log aggregates without Flink.
