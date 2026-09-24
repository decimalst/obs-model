# Validation record

Local validation performed on 2026-09-21–23, on macOS arm64 with Linux containers in Docker. This record distinguishes executed checks from deployment configuration and future acceptance work.

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

## 2026-09-23: service experience and eBPF integration (0.5)

Added independently opted-in Hubble and CPU profiling, regional profile storage/Pod Identity, v2 retention and bounded profile settings, a Git-owned service catalog/SLO compiler, a Go service scaffold with explicit publication, and the static configuration studio/Pages artifact workflow. Network ownership remains external; preflight now checks the actual AWS network-policy flag instead of treating the node-agent container's presence as proof of enforcement.

Executed local checks:

| Check | Result |
|---|---|
| `make check` | Pass: 72 Python tests, 6 Node tests, Go race tests/vet for quota and scaffold, strict Helm lint, Terraform formatting, Python/JavaScript formatting and shell syntax |
| Default native Kubernetes schemas | Pass: 386 rendered resources; 328 valid, 0 invalid, 0 errors, 58 skipped; custom-resource checks remain separate |
| `make terraform-test` | Pass: 19 mocked tests and module/root validation, including optional regional profile storage and external role ownership |
| `make studio-terraform-test` | Pass: generated new-cluster Terraform initialized with the committed provider lock and validated; existing-cluster output also passes the Python platform contract |
| `make runtime-test` | Pass: all 3 Vector and 5 collector configurations, Mimir's 9 targets, Tempo, and quota tests against live Valkey |
| `make collector-test red-test` | Pass: Arrow trace affinity/cardinality and pre-tail RED behavior, including two independent writers and 0% healthy trace retention |
| `make rules-test` | Pass: existing rules plus 6 generated catalog rules; healthy, idle, zero-error and sustained burn-rate cases |
| `make dashboard-test` | Pass: actual Grafana 12.3.1 loads the catalog and RED dashboard and reloads edited files |
| `make profiling-test` | Pass: native Alloy validation and malformed-config rejection, Pyroscope API startup with rendered configuration/metastore flags, Hubble collector syntax; optional platform plus Cilium reference total 263 rendered resources, 250 valid, 13 skipped, 0 errors |
| Cilium reference chart | Pass: pinned dependencies, rendered chaining/Hubble configuration and 40 native resources valid; no installation performed |
| Workflow syntax | Pass: actionlint; hosted runs have not occurred |
| Static studio | Pass: both deployment previews, service form and local catalog import/filter in the in-app browser; ZIP contents/CRC and Unicode round trip tested independently; browser download events did not report completion, so a successful browser file save is not asserted |

The final studio-only change preserving optional imported database/cluster settings reran its compiler, export, catalog and generated-Terraform checks. Kafka/ClickHouse sharding and consumer behavior were not changed or rerun in this pass; their earlier executed evidence remains above.

No cloud apply, CNI change, application publication or GitHub Pages deployment was performed. A GitHub remote is not configured in this checkout. Profile parser/API startup validation is not eBPF capture, Pod Identity, retention deletion, Raft failover or distributed-storage qualification; the live gates in `ebpf.md` remain required. No live profile datasource query, mobile browser acceptance, application image deployment or hosted Pages download is claimed.

## 2026-09-23: operational support (0.6)

Added opt-in exact stored-signal canaries, Git-reconciled owner/default alert routing, native backend scrapes and a support dashboard, read-only local support inventories, and protected owner-scoped debug requests. Native scraping uncovered a real empty-separator regression: healthy `up` metrics concealed dropped safe samples. The shared cardinality policy now uses one source label per drop rule; custom grouped policies require migration.

Executed local checks:

| Check | Result |
|---|---|
| `make check` | Pass: 82 Python tests, 6 Node tests, Go race tests/vet, strict Helm lint, formatting and shell syntax |
| Default native Kubernetes schemas | Pass: 390 rendered resources; 332 valid, 0 invalid, 0 errors, 58 skipped; packaged custom-resource checks run separately in runtime validation |
| `make terraform-test` | Pass: 19 mocked tests and all module/root validations; no real plan/apply |
| `make runtime-test` | Pass: Vector transforms/configuration, all collector configurations, Mimir targets, Tempo and quota behavior against live Valkey |
| `make support-test`: canary | Pass: exact log, metric marker and trace found through real local Vector, Arrow collectors, ClickHouse, Prometheus and Tempo; stale/wrong identities and partial OTLP ingestion have unit rejection tests |
| `make support-test`: canary account/runtime | Pass: the rendered read-only ClickHouse profile permits bounded reads, rejects writes and scan-limit overrides; pinned non-root/read-only support container starts without credentials when checks are disabled, returns ready and reports health as degraded |
| `make support-test`: alerts | Pass: Mimir 3.2.0 accepts generated tenant configuration and delivers owner/default fixture notifications to a disposable local receiver |
| `make support-test`: scrapes/dashboard queries | Pass: actual backend samples survive production scrape filters, Arrow and remote write; identifier-bearing samples including a semicolon-only value are rejected; support queries execute against stored metrics |
| `make support-test`: Kafka exporter | Pass: the pinned Strimzi exporter reports 13 queued records across 3 partitions and healthy replication against a real disposable broker |
| `make dashboard-test` | Pass: Grafana 12.3.1 loads the RED, catalog and support dashboards, prevents Viewer edits and reloads a changed provisioned file without restart; browser rendering remains separate |
| `make collector-test red-test rules-test` | Pass: Arrow/cardinality/48-trace affinity regression, pre-tail RED behavior, 22 rule definitions including the catalog fixture, and all four rule-test files |
| `make profiling-test` | Pass: native Alloy/Pyroscope configuration and Hubble scrape checks; optional manifests total 267 resources, 254 valid, 13 skipped, 0 invalid/errors |
| Workflow and chart packaging | Pass: actionlint and versioned chart packaging; Python bytecode is excluded from the Helm archive |

The support test uses a direct Vector-to-ClickHouse sink with synthetic Kubernetes metadata, single-node ClickHouse and Tempo, and Prometheus as the Mimir metrics-store substitute. It bypasses trace routing/tail sampling, which the separate collector suite exercises. Kafka exporter values are tested against a separate single-broker fixture; this does not prove Strimzi deployment or partition failover. Existing sharding/consumer behavior was not changed or rerun in this increment; earlier executed evidence remains above.

The native alert fixture uses HTTP only for its disposable local receiver; production receiver configuration requires HTTPS. It tests configuration acceptance and notification routing, not real credential delivery, receiver-side processing, resolution or paging. No external notifications, AWS changes, GitHub publication or hosted workflow runs occurred. Live EKS/operator reconciliation, end-to-end deployed canaries, independent health monitoring, external alert acceptance and recovery/HA rehearsals remain required. See [support.md](support.md) for enablement and migration steps and [platform-gaps.md](platform-gaps.md) for remaining work.

## 2026-09-23: setup choices, privacy, graphs and deferred qualification (0.7)

Added optional log redaction/drop and strict trace attribute allowlists, bounded observed service graphs before tail sampling, five Grafana access/security profiles, offline installation previews, regional cell budget review, recovery setup alternatives and one ordered 26-gate live acceptance batch. Selected profiles persist through the installation contract and GitOps compiler; externally owned ingress, Secrets, identity providers and mesh policies remain external. The final adversarial review also fixed generated Grafana overrides bypassing the selected login policy and cell labels concealing a known shared database.

Executed local checks:

| Check | Result |
|---|---|
| `make check` | Pass: 109 Python tests, 6 Node tests, quota/scaffold Go race tests and vet, strict Helm lint, formatting and shell syntax |
| Default native Kubernetes schemas | Pass: 390 rendered resources; 332 valid, 0 invalid, 0 errors, 58 skipped; packaged custom-resource validation remains separate |
| `make terraform-test` | Pass: 19 mocked tests and module/root validation; no live plan or apply |
| `make runtime-test` | Pass: all 3 Vector and 5 collector configurations, Vector grant boundaries, Mimir's 9 targets, Tempo and live local Valkey quota tests |
| `make collector-test red-test rules-test` | Pass: real Arrow/cardinality/writer checks and 48-trace affinity across 3 samplers; RED 8 calls/4 errors/2.62 histogram seconds with two writers and zero healthy trace retention; 22 rule definitions and all four behavior fixtures |
| `make integration-test` | Pass: production log path through Vector, shared quota, Kafka and ClickHouse; 5 flood errors, 1 quiet log, 1 granted debug log, no expired debug log and 7 derived events |
| `make dashboard-test` | Pass: Grafana loads RED/catalog/support dashboards, restricts Viewer edits and reloads provisioned files |
| `make privacy-graph-test` | Pass: native Vector redact/drop/safe/malformed/envelope cases at agent and gateway; collector strict trace sanitization and linked-span rejection; 48 client/server pairs and 24 failures through Arrow, trace-affine routing and 3 samplers, with bounded names, seconds histograms, independent writers and zero retained fixture traces |
| `make security-test` | Pass: Grafana 12.3.1 rejects anonymous/local-password access in the compiled OAuth profile, emits the exact HTTPS callback and S256 PKCE challenge, rejects a missing CA during a synthetic callback, and permits explicit local recovery |
| `make qualification-test` | Pass: 10 offline tests covering pending/skipped gates, evidence/configuration identity, deployment artifact hashes, sequence/dependency invalidation, append-only receipts, malformed timestamps, recovery bounds and cross-region opt-in |
| Setup and isolation | Pass: actual offline Helm preview with combined security/privacy/graph settings; persisted profile and Flux output; key-only read preflight; nine security override channels rejected; shared backend/assignment rejection and AWS-seeded database identity cross-checks |
| Workflow syntax and chart packaging | Pass: actionlint and versioned Helm package; no hosted workflow executed |

The final setup-only review fixes reran `make check qualification-test`; native pipeline behavior was unchanged by those fixes. The collector fixtures use Prometheus/file sinks instead of a deployed Mimir/Tempo/S3 topology. Service-graph pairing is demonstrated locally, not under Kubernetes membership changes or partial real SDK instrumentation. The privacy policy is explicitly lossy and pattern/allowlist based; it is not a universal sensitive-data detector. The security fixture uses synthetic credentials and a loopback callback failure: it does not authenticate with an actual identity provider, prove group authorization, validate browser TLS or execute mesh policy. Sharding, consumer scaling, eBPF capture and the full support acceptance fixtures were not changed or rerun in this increment; their earlier evidence and live limitations remain above.

**All live AWS creation and acceptance remains deferred.** No AWS/Kubernetes account was contacted, no real identity-provider or alert receiver was invoked, and no deployment, backup restore, cloud fault injection or publication occurred. The qualification utility only prepares and records operator attestations; it cannot run or independently witness the 26 gates. The example artifact hashes and environment identities are placeholders, not acceptance evidence. Full internal transport encryption, authenticated multi-tenant intake, actual restore execution and measured capacity/RPO/RTO still require the documented owner integrations and live rehearsal. Start with [setup-options.md](setup-options.md) and retain all unresolved live results in [live-qualification.md](live-qualification.md).

## 2026-09-23–24: interactive advanced configuration (studio 0.8)

The studio now exposes the 0.7 security, privacy, graph, isolation and recovery/qualification contracts in a six-step form with conditional fields and setup explanations. Request import/export preserves supported choices; unknown fields and inactive existing-cluster payloads fail validation. Downloads include selected profile/planning files and tailored instructions. New-cluster cell plans explicitly consume the installer-reviewed contract, preserving chosen limits instead of reporting raw Terraform defaults. The backend chart remains 0.7.0; this increment changes the studio and its tests/documentation.

| Check | Result |
|---|---|
| `make check` | Pass: 116 Python tests, 23 Node tests, Go race tests/vet, strict Helm lint, formatting and shell syntax |
| Native Kubernetes schemas | Pass: 390 resources; 332 valid, 58 skipped, 0 invalid/errors; custom-resource validation remains separate |
| `make terraform-test studio-terraform-test` | Pass: 19 mocked Terraform tests, module/root validation and actual generated Terraform initialized using the committed provider lock and validated; no real plan/apply |
| `make runtime-test security-test` | Pass: native Vector/collector/Mimir/Tempo configuration, local Valkey quota behavior and compiled Grafana OAuth/local-recovery fixture |
| `make privacy-graph-test collector-test` | Pass: native privacy fixtures, 48 graph pairs/24 failures, independent metric writers at zero retained fixture traces, and Arrow/cardinality/48-trace routing affinity across three samplers |
| Browser/backend contract parity | Pass: all five security profiles, complete privacy/graph defaults and opt-ins, three recovery setup alternatives, cell plans and nondefault new-region budgets consumed by the actual Python validators/planners |
| Browser interaction | Pass: new-region selections through OIDC/ingress/Istio, privacy and graphs into an eight-file preview; complete existing-cluster/Linkerd/remote-recovery/peer-cell request imported and reproduced with matching canonical contents; malformed and empty graph allowlists block export and corrected input restores it |
| Layout and keyboard | Pass: desktop visual inspection and 390px mobile-width inspection without page overflow; step navigation focuses its legend and request-import keyboard focus has a visible outline |
| Public artifact and syntax | Pass: complete static module graph in the explicit allowlist, unknown/credential field rejection, request compatibility/round trips, ZIP contents/CRC, actionlint and whitespace checks |

The last accessibility-only changes passed JavaScript syntax/format checks and direct browser verification. Compiler/ZIP contents were tested locally; successful file saving by every browser/download policy is not asserted. The local preview used synthetic endpoints and account identifiers; no actual identity-provider, AWS/Kubernetes, alert receiver or remote repository was contacted. GitHub Pages publication and hosted workflow execution remain deferred. The full log storage/sharding/consumer, RED/rules, eBPF and support fixtures were unchanged and were not rerun in this studio increment; their earlier results remain above. Rendering the setup options does not qualify the underlying cloud integrations.

## 2026-09-24: explicit IAM configuration and ownership (studio 0.9)

Added a dedicated AWS IAM step separating Terraform execution, the existing EKS administrator and runtime workload roles. Every bundle includes an IAM inventory and setup guide. New deployments can assume an existing provisioning role and reuse selected storage roles without taking over their policies; software-only deployments create no AWS IAM resources. The inventory describes the exact current trust/actions/resources and namespace/service-account associations, including account-scoped Pod Identity trust and the CNI policy on the node role. Qualification account selection no longer infers the deployment account from an administrator ARN. Backend resource behavior and chart version remain unchanged.

| Check | Result |
|---|---|
| `make check` | Pass: 116 Python tests, 32 Node tests, Go race tests/vet, strict Helm lint, formatting and shell syntax |
| Native Kubernetes schemas | Pass: 390 resources; 332 valid, 58 skipped, 0 invalid/errors; custom-resource validation remains separate |
| `make terraform-test` | Pass: 23 mocked tests plus module/root validation. Added assertions for role trust, managed-policy attachments, EKS access, exact S3 permissions, component associations and partial external-role ownership. One test uses a mocked apply to resolve mock bucket ARNs; no real AWS plan/apply occurred. |
| `make studio-terraform-test` | Pass: default generated Terraform and an assumed-provisioner/all-storage-role-reuse configuration both initialized with the committed provider lock and validated |
| `make runtime-test` | Pass: pinned Vector, collector, Mimir and Tempo configuration/transform checks plus quota behavior against disposable local Valkey |
| IAM/compiler contracts | Pass: default 7-role/5-association inventory, optional profiling, distinct reused-ARN counts, same-account restrictions, cross-account administrator separation, optional existing Flink role/bucket pair, older-request defaults and complete request round trips |
| Browser interaction | Pass: default 7 roles and 8 with profiling; Mimir reuse changes the count and exports the selected provider/module arguments; wrong-account role blocks download; imported existing-cluster contract shows zero created IAM resources and preserves optional Flink coordinates; switching modes displays the export boundary and restores new-cluster choices |
| Browser layout and final controls | Pass: desktop review and 390px mobile role-policy inspection without page overflow; step navigation focuses the legend; no browser error/warning messages during final interaction. Final mode-notice/copy changes reran `make studio-test` and direct browser checks. |

The public build contains only the static allowlist, now including `iam.mjs`. Tests used synthetic identities and verify declared policies and generated artifacts, not effective AWS authorization. Live STS/Pod Identity credential delivery, policy evaluation including SCPs/permission boundaries, KMS behavior, S3 operations, private EKS connectivity and service-linked-role creation remain in the deferred AWS qualification batch. No cloud resources, identity grants, deployments, external notifications or publication occurred. Pipeline behavior was unchanged; the broader sharding/consumer, RED/rules, eBPF, security and support integration fixtures were not rerun in this IAM increment.
