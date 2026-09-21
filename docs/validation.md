# Validation record

Local validation performed on 2026-09-21, on macOS arm64 with Linux containers in Docker. This record distinguishes executed checks from deployment configuration and future acceptance work.

## Executed checks

| Check | Result |
|---|---|
| Terraform formatting and validation | Pass: both modules and all 3 roots, including existing EKS |
| Terraform mocked plan tests | Pass: 16 tests across the regional module, shared data-services module and existing-cluster root; includes role/association ownership, subnet/VPC/AZ selection and invalid inputs |
| Helm dependency resolution and strict lint | Pass: all 4 charts, with committed dependency locks |
| Python tests | Pass: 31 tests for grants, ownership, secret preservation, regional coordinates, schemas, routing, scheduling, network-policy contracts and existing-environment installation |
| Python lint/format, shell syntax, Go vet | Pass |
| Kubernetes native schemas | Pass: 350 rendered resources; 299 valid, 0 invalid, 0 errors, 51 skipped because native schemas are unavailable |
| Custom resources | Pass: all 11 platform CRs checked separately against the packaged pinned CRDs; optional Flink deployment also schema-validated |
| Vector configuration | Pass: all 3 configurations validated by Vector 0.54.0 |
| OpenTelemetry configuration | Pass: all 4 configurations validated by Collector Contrib 0.148.0 |
| Backend configuration | Pass: Tempo 2.9.0 verification and Mimir 3.2.0 validation for 9 distributed targets, including the chart's Alertmanager zone argument |
| Actual Vector transform tests | Pass: 8 scenarios covering errors, debug/trace defaults, valid/expired/malformed/future/overlong grants, trusted identity, and grant-field removal |
| Go tests with race detector and live Valkey | Pass: 5 top-level tests plus HTTP subcases; shared quotas, byte/debug budgets, malformed/oversized requests, saturation, store outage, and graceful in-flight shutdown |
| Workflow syntax | Pass: actionlint 1.7.11; hosted workflow execution has not occurred |
| Quota image build and log integration | Pass: the rebuilt image completed the Vector → admission → Vector → Kafka → Vector → ClickHouse/Keeper path, using the production SQL and log transforms |

The shared-quota test sends 200 error events concurrently through two admission instances and admits exactly 100 from their shared burst budget. An independent quiet service still receives admission. The test finishes within a refill interval and fails if it does not, so the expected count cannot hide a refilled budget.

Existing-environment tests use synthetic AWS/Kubernetes inventories and real Helm rendering. They verify read-only preflight behavior, wrong-cluster and cross-region rejection, mismatched Pod Identity associations, preservation of existing secrets, controller/StorageClass ownership, propagation of non-`a/b/c` AZs and custom storage classes, stale-overlay rejection, and that disabled controllers produce no resources or bootstrap CRD writes. Terraform role-reuse tests assert zero IAM role/policy resources for externally supplied roles. No existing EKS cluster was contacted or changed, and no live Terraform state migration was attempted.

The integration test stores exactly 5 flood/error records, 1 quiet-service record, and 1 valid debug record. It stores no expired debug record. The derived aggregate totals 7, and admission metrics report 7 accepted and 15 dropped. The topology has one broker, database, and Keeper; it verifies the log path, not multi-AZ failover. The final local quota image ID is `sha256:ee446527b4157306040d3e3a9757e09a7e5ee2e9846c192a3847bf9f18a0e2df` (a local arm64 image ID, not a published registry digest). Test containers and their network were removed.

Tool versions used: Terraform 1.15.8, AWS provider 6.66.0, Helm 4.2.3, Go 1.25.8, Python 3.14.7, kubeconform 0.7.0. Container and chart versions are recorded in their source manifests and lock files. The quota image's build and runtime bases are pinned by digest. Other version-tagged upstream images should also be resolved to immutable digests under a production release process.

## Scope and remaining acceptance

No AWS infrastructure has been applied and no repository or image has been published by this work. Local runtime tests use disposable containers. Native/CRD validation does not execute Kubernetes controllers, admission webhooks, scheduling, or cloud APIs. Mimir/Tempo configuration validation does not prove ingestion, queries, or S3 access.

The remaining acceptance gates are actual EKS deployment and operator reconciliation; IAM/Pod Identity and network policies; OTLP metrics/traces through to Grafana; live autoscaling and blue-green promotion/rollback; scrape assignment behavior during allocator changes; Valkey and three-AZ quorum failure; backup restore; regional recovery; and measured cost/throughput. See [testing.md](testing.md) for procedures. No measured production capacity, RPO, RTO, or dollar cost is claimed.

Flink is an optional operator/deployment integration with savepoint and regional checkpoint configuration. No arbitrary application job or connector is supplied; its runtime behavior still requires a real job and job-specific tests. The included ClickHouse materialized view implements basic derived log aggregates without Flink.
