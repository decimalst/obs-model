# Validation record

This dated record covers local validation beginning on 2026-09-21 and the first authorized disposable AWS run on 2026-09-25. Local checks used macOS arm64 with Linux containers in Docker. Each section describes its own execution window: earlier statements that AWS work was deferred are historical, not the current status. The live run and its remaining limits are recorded at the end.

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

## Initial offline scope and remaining acceptance (2026-09-21–23)

At this initial milestone, no AWS infrastructure had been applied and no repository or image had been published by this work. Local runtime tests use disposable containers. Native/CRD validation does not execute Kubernetes controllers, admission webhooks, scheduling, or cloud APIs. Mimir/Tempo configuration validation does not prove ingestion, queries, or S3 access. The collector test substitutes file exporters for those backend sinks; it proves transport, filtering, affinity and tail decisions, not storage integration for metrics/traces.

The acceptance gates still pending at that milestone included KEDA/HPA scaling from induced lag, scaler fallback and maintenance pause; Helm schema-hook execution and operator-driven shard expansion; live Flux reconciliation/drift correction, Grafana dashboard/query rendering and Mimir rule-file reload; actual EKS deployment and operator reconciliation; IAM/Pod Identity and network policies; OTLP metrics/traces through to Grafana; live autoscaling and blue-green promotion/rollback; scrape assignment behavior during allocator changes; Valkey and three-AZ quorum failure; backup restore; regional recovery; and measured cost/throughput. See [testing.md](testing.md) for procedures. No measured production capacity, RPO, RTO, or dollar cost is claimed.

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

During this 0.5 increment, no cloud apply, CNI change, application publication or GitHub Pages deployment was performed. A GitHub remote was not configured in the checkout at that time. Profile parser/API startup validation is not eBPF capture, Pod Identity, retention deletion, Raft failover or distributed-storage qualification; the live gates in `ebpf.md` remain required. No live profile datasource query, mobile browser acceptance, application image deployment or hosted Pages download is claimed.

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

The native alert fixture uses HTTP only for its disposable local receiver; production receiver configuration requires HTTPS. It tests configuration acceptance and notification routing, not real credential delivery, receiver-side processing, resolution or paging. During this 0.6 increment, no external notifications, AWS changes, GitHub publication or hosted workflow runs occurred. Live EKS/operator reconciliation and deployed canaries were still pending at that milestone; the limited 2026-09-25 results appear below. Independent health monitoring, external alert acceptance and recovery/HA rehearsals remain required. See [support.md](support.md) for enablement and migration steps and [platform-gaps.md](platform-gaps.md) for remaining work.

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

**At the end of this 0.7 increment, all live AWS creation and acceptance remained deferred.** During that increment, no AWS/Kubernetes account was contacted, no real identity-provider or alert receiver was invoked, and no deployment, backup restore, cloud fault injection or publication occurred. The qualification utility only prepares and records operator attestations; it cannot run or independently witness the 26 gates. The example artifact hashes and environment identities are placeholders, not acceptance evidence. Full internal transport encryption, authenticated multi-tenant intake, actual restore execution and measured capacity/RPO/RTO still require the documented owner integrations and live rehearsal. Start with [setup-options.md](setup-options.md) and retain all unresolved live results in [live-qualification.md](live-qualification.md).

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

The public build contains only the static allowlist, now including `iam.mjs`. Tests used synthetic identities and verify declared policies and generated artifacts, not effective AWS authorization. At this 0.9 milestone, live STS/Pod Identity credential delivery, policy evaluation including SCPs/permission boundaries, KMS behavior, S3 operations, private EKS connectivity and service-linked-role creation were deferred. No cloud resources, identity grants, deployments, external notifications or publication occurred during that increment. The later live run below covers only a subset of those checks. Pipeline behavior was unchanged; the broader sharding/consumer, RED/rules, eBPF, security and support integration fixtures were not rerun in this IAM increment.

## 2026-09-25: Mimir locality limitation identified

Static review of the pinned Mimir 6.2.0 Helm chart found that ingester Kafka rack hints use logical rollout zones (`a`, `b`, `c`), while Strimzi advertises full AWS availability-zone names. The existing configuration therefore does not establish same-AZ reads. The cost documentation now records this limitation and an operational correction/verification task. The stable StatefulSet zone names and PVC identities must remain intact. No runtime configuration change or read-locality validation was performed for this finding.

At the time of this review, native Mimir configuration checks used `-print.config`; those checks did not start ingester modules, discover Kafka group coordinators, or prove readiness. The later live run below establishes successful startup and fresh metric storage for that run. It does not establish same-AZ Kafka read locality.


## 2026-09-25: authorized disposable AWS deployment

The dedicated `olly-lab-0925a` run used one empty AWS account in `us-east-2`, an explicitly selected identity, a private EKS endpoint and private SSM runner. It applied the regional platform, published a disposable amd64 quota image to its private lab ECR repository, and exercised real stored signals. This is a bounded deployment test, not production certification or completion of the deferred qualification batch.

| Executed live check | Result and scope |
|---|---|
| Terraform deployment and convergence | 121 Terraform resources applied. A subsequent plan reported no changes. This does not cover existing-cluster adoption or state migration. |
| Worker and database placement | Six `m7i.xlarge` workers across three AZs. ClickHouse had two actual write shards, each with three replicas separated across the three AZs. |
| Writes across shards | The same synthetic flood service stored 171 distinct records on one shard and 192 on the other: 363 total. Replicas were not counted as extra write shards. |
| Fresh stored signals | Both support replicas completed fresh exact log, metric and trace checks before and after the bounded noisy-neighbor exercise, and after the later collection pause was reversed. These are stored-signal checks against ClickHouse, Mimir and Tempo, not only readiness responses. |
| Noisy-neighbor path through node agents | Of 3,000 offered ERROR records, 363 distinct flood records reached storage; all 30 offered quiet-service INFO records reached storage. Counts remained stable for a 20-second observation. Quota drops increased with no observed quota unavailability, and the temporary producer Jobs were deleted. Aggregate quota counters included other services, so this test alone does not prove a shared per-service burst bound. |
| Isolated shared-quota burst | An initial attempt was inconclusive because background traffic contaminated aggregate counters. During a separate approximately 73-second lab-only pause of the six Vector node agents, the helper sent the same fresh synthetic service directly to all three active quota replicas: 100 records each, 300 total, without retry. The measured request window was 0.222171 seconds; its conservative global bound was 46. Exactly 40 were admitted, 260 dropped, and all 40 distinct accepted records reached storage; no quota unavailability was observed. All six agents were restored and both canary replicas again found fresh complete signals. This supplemental test explicitly bypasses node-agent collection. |
| Pod Identity allow/deny | A temporary Job using the existing Mimir service account received Pod Identity credentials, returned HTTP 200 for `ListBucket` on its assigned Mimir bucket and HTTP 403 on the unrelated lab artifact bucket. The Job was deleted. This does not prove object operations, every runtime role, KMS permissions or the complete effective IAM policy. |
| Rollout Service ownership | After the Helm 4 client-side-apply correction, all eight active/preview rollout Service hashes and endpoint selections were verified. This checks controller-owned routing preservation; no real promotion or rollback was exercised. |
| Cleanup complete | All 26 inventoried CSI data volumes, totaling 1,156 GiB, were verified deleted. Terraform destroyed all 121 managed resources. The final independent inventory at 17:58:22 UTC found zero remaining lab clusters, active instances, volumes, EC2/RDS/cache snapshots, retained database backups, EIPs, NAT gateways, interfaces, VPCs, databases, caches, load balancers, buckets, ECR repositories, log groups, IAM roles/policies or Secrets. The final database snapshot and disposable quota Secret were explicitly removed; the automated snapshot disappeared afterward. The dedicated state bucket, including all state/lock versions, was deleted last after preserving and verifying empty remote state at serial 16. Existing SSO and AWS service-linked roles were left intact. |

The shared-burst bound was `floor(40 + 20 × elapsedSeconds) + 2`, using the reviewed 20 EPS / 40-event burst policy and a two-event measurement margin. It was strictly below the 120 events that three independent 40-event bursts could admit. This is evidence for one stable, isolated shared-service burst, not sustained-load capacity, failover correctness or production-wide fairness. Stopping node agents was a temporary measurement isolation step in this disposable lab; it is not a recommended production acceptance procedure.

### Bootstrap finding and subsequent local correction

The first bootstrap lost synthetic logs. Vector gateways encountered `UnknownTopicOrPartition` before the Kafka topic existed; writers encountered non-retriable insert errors before the ClickHouse schema was created. Fresh successful canaries later in the run do not recover or erase those initial losses.

The resulting [startup gates](startup-gates.md) were implemented after live acceptance and were **not deployed or qualified in a new cold AWS installation**. The Kafka gate checks the actual topic, exact partition count, leaders and ISR; the writer checks every expected replica for the complete typed schema; the quota gate waits for the active gateway. Local native fixtures verified the pinned Kafka and ClickHouse clients, missing/undersized topics, missing replicas/materialized views and wrong table engines. The six targeted unit tests and local `make runtime-test` passed. The final `make check` passed 189 Python tests, 34 Node tests, chart/code checks and native schema validation; the latter reported 392 resources, 334 valid, 58 skipped and no invalid resources/errors. Workflow syntax also passed actionlint. The final `make startup-gate-test` passed with the actual native clients and cleaned up its Docker resources. `make consumer-test` passed 36-partition backlog, consumer resizing, storage-outage acknowledgement and 22,800-record delivery checks. Its reduced one-replica fixture now explicitly preserves a quorum-one override across re-renders; production quorum remains two. `make sharding-test` then passed with three replicas per shard and production quorum two, including replica loss, expansion and unavailable-shard failures. These are local Docker tests, not live scaling/fault qualification. A cold start with pre-labelled collecting namespaces, retained accepted markers and the supported Helm/Flux hook sequence remains pending. The gates do not promise lossless runtime operation.

### Evidence provenance and unresolved qualification

The following private aggregate reports were read only through approved fields. Their hashes identify the exact files used for this summary; raw plans, state, tfvars, Secrets, logs and pod specifications are not included. The reports remain private and are not part of the static site bundle.

| Report | Execution window (UTC) | SHA-256 |
|---|---|---|
| `lab-acceptance-01.json` | 17:19:24–17:27:10 | `1cb981adff7c8880bf79c986909d8fbf178b31af73e7f1d55caa4bb077d63e17` |
| `lab-shared-quota-02.json` | 17:32:47–17:33:36 | `70e741fb79bb15818561249b8423e8243d0d680a1a14e41a7112aadf704a0ab4` |
| `lab-pod-identity-01.json` | 17:31:11–17:31:29 | `ce009afe0d3b44d8208e82a335bcfebc27f71b800a8b22a472907d7996e1de0e` |
| `lab-recovery-canary.json` | Restored 17:33:41; verified by 17:35:22 | `1a43f48e9182dd2a2e073ebc904737eaa0c842dcabf4f977c4b18d0a12edc752` |
| `residual-inventory.json` | Final cleanup verification 17:58:22 | `911ee54b40bc72b65f756773ade21e5fbbb4a4c9e8c2249e2103e3fb2e5c0ac7` |

The three acceptance helpers deliberately retain `qualificationGatePassed: false` even when their bounded subchecks pass. The recovery-canary report describes resuming collection after a short pause, not disaster recovery. These reports and the operator's placement/convergence/cleanup observations are scoped to this lab and its deployed configuration. Installation fixes were made during the run; the results do not qualify every intervening or subsequently changed source revision. Hashes detect changes to the report files, not an independent witness of AWS execution. Formal acceptance still requires the exact source/chart/image/configuration identities and approved evidence to be bound to an operator attestation in [live qualification](live-qualification.md); no gate is promoted automatically by this narrative.

No AZ or regional fault, isolated database restore, distributed-backend DR, real blue-green promotion/rollback, induced-lag autoscaling, partition/shard expansion, full IAM authorization matrix, or production SSO/network-security qualification was performed in this run. Live expiry/debug and metric-cardinality adversarial cases, partial-ingestion rejection, Valkey outage behavior, external alert receipt and optional eBPF/profiling/Flink workloads also remain unqualified by this run. Earlier local fixtures are still local evidence. No measured production capacity, RPO, RTO, cross-AZ savings or actual billed cost is claimed.

The estimated fixed baseline was approximately $1.79/hour plus the private runner and usage-dependent charges, as described in [the lab cost envelope](aws-lab.md#cost-envelope). The run lasted approximately 102 minutes from bootstrap to final cleanup verification and is expected to remain below the user's $100 target; actual billing has not been verified. This estimate is neither a billing result nor a hard cap.
