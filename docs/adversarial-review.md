# Adversarial design review

This review distinguishes tested fixes from deployment assumptions. Regional multi-AZ configuration is not evidence of surviving an actual AZ outage. No AWS environment or GitOps controller was contacted during this pass.

| Finding | Change / evidence | Remaining boundary |
|---|---|---|
| Applications could bypass collectors by reaching any internal OTLP listener | Client ingress now selects only `app=otel-agent`; manifest selector regression | Other additive NetworkPolicies can reopen access; verify the deployed CNI and policy set |
| Dropping identifier labels can collapse independent counters/histograms | Drop offending series/points in scrape, agent, gateway and Mimir paths; real collector rejection test | This is intentional loss. Fix SDK labels to recover safe aggregate metrics; never claim label removal performs aggregation |
| OTLP resource metadata creates cardinality via `target_info` | Disable target-info and blanket resource conversion; explicitly promote six Kubernetes/regional identity attributes | `service.instance.id`, pod UID or node name is required by default; application metrics must identify the actual writer |
| Backend rate limits alone do not bound series/query memory | Tenant and per-metric series caps, label count/length caps, query series/bytes/parallelism limits | Metrics and traces still share tenant budgets. Strong per-service fairness requires authenticated tenant separation or isolated cells |
| Large Helm integers rendered as scientific notation, preventing quota service startup | Force decimal integer environment values; test default and 10 MB/s custom budgets | Native config tests alone do not start every generated Kubernetes workload |
| Different blue/green quota policies can share a bucket inconsistently | Store policy with each bucket; reject mismatched policy without refreshing its TTL; real Valkey transition test | After promotion, retries buffer until old writers stop and the old bucket expires. Changes are not zero-backpressure |
| A backwards primary clock can mint tokens twice | Keep the bucket timestamp monotonic and extend expiry; actual Valkey regression | Asynchronous Valkey failover can lose recent tokens and temporarily over-admit. This is telemetry cost containment, not billing-grade accounting |
| Arrow docs differed from pinned implementation | Validate actual v0.148 receiver (`protocols.arrow`, separate OTLP HTTP receiver) and native queue units; real Arrow metrics/traces test | Components are beta; benchmark CPU/memory/bytes at your event sizes before claiming savings |
| Arrow substitution could lose trace affinity | Keep the load-balancing exporter's supported OTLP protocol and fixed sampler endpoints | Sampler membership changes require a coordinated drain; a sampler crash loses in-memory tail state |
| One fixed Kafka data pool and fixed writer count limit growth | Add broker-only pool, configurable partitions/scrape replicas and writer HPA bounded by log partitions | Adding brokers does not rebalance existing partitions automatically; controllers remain a fixed 3-member quorum |
| Manual dashboard/rule changes drift from deployment | Git-mounted content, Flux revision tracking/drift repair, alert behavior tests | Browser UX, live rule reload and live Flux reconciliation still require cluster acceptance |

## Default budget contract

All fields live in `charts/platform/values.yaml`; `examples/tuning.yaml` demonstrates deploy-time overrides. `install.py --tuning` validates before remote preflight; the selected policy is preserved in the generated deployment contract. Invalid percentages, debug budgets above total budgets, batch/queue inconsistencies, unknown tuning keys and unsafe scrape timings fail early. Direct Helm and GitOps use the same chart schema and cross-field guards.

- **Logs:** 500 events/s per service/node before parsing; shared namespace/service budget 1,000 events/s and 1 MiB/s, bursts 2,000/2 MiB; debug is a sub-budget of 100 events/s and 100 KiB/s. Every severity consumes the budget. Debug grants cannot exceed 24 hours. Buffers are finite; tuning them changes memory/disk demand.
- **Metrics:** scrape every 30s with 10s timeout, 10,000 samples/target/scrape; tenant 50,000 samples/s, 100,000 burst, 1M active series, 50,000 series/metric; 40 labels, names 128 and values 512 characters; 15d retention. At most 100,000 fetched series, 512 MiB fetched chunk bytes and 16 query splits in parallel. Reordering tolerance is 2m; this does not fix identity collisions.
- **Traces:** 10% baseline candidates; capped composite 2,000 spans/s per sampler, 50% error / 25% slow / 25% baseline allocation, 30s decision wait, 50,000 in-flight traces. Errors are preferred, not guaranteed. Tempo tenant ingress is 5 MB/s with 10 MB burst, 5 MB/trace and 72h retention.
- **Collector links:** Arrow uses 1 stream/exporter, 32,768 queued items, bounded receiver admission and Arrow buffers. OTLP fallback mode uses 256 queued requests. Queue units differ; these values are not interchangeable. Memory queues are not durable, retries stop after 5m, and a crash or overflow can lose telemetry.

The common forbidden metric labels include user/request/session/trace/span identifiers, raw URLs/path/query strings, SQL statements and exception messages/stack traces. Bounded route templates (`http.route`), method/status, histogram bucket boundaries and writer identity remain intact. Mimir's policy is also the source for edge filter generation, so deployment overrides cannot silently diverge. Unknown dynamic label names or metric names are contained by cardinality/length ceilings, not magically detected.

## Cost, security and failure limits

Data remains in its source region. Multi-region means independent regional cells and queries, not replicated consensus or transparent regional disaster recovery. Cross-AZ Kafka/storage replication and shared regional gateways still cost bandwidth. Arrow applies to agent→metrics gateway, agent→trace router and scrape pool→metrics gateway. SDK ingress, router→tail samplers and samplers→Tempo use OTLP; Mimir receives Prometheus remote write. A transport optimization does not itself prove lower AWS charges.

The platform namespace is a trusted administrative boundary. Workload/service labels and debug grants require RBAC ownership; namespace users who can forge identities can evade workload quotas. There is no authentication-based hostile multi-tenant isolation. Internal Kafka/OTLP/ClickHouse links remain plaintext unless a service mesh or equivalent TLS/auth layer is installed. Per-service quotas do not guarantee fair CPU, disk reads, network connections or request slots under arbitrary adversarial load. Retain node read fairness and request caps, test real workloads, and isolate services/tenants where stronger guarantees are needed.

ClickHouse now defaults to **two shards with three replicas each**, with configurable append-only expansion and a Kafka-backed write pause. Historical rows remain on their original shards; expansion does not rebalance them. See [sharding](clickhouse-sharding.md). More replicas alone improve availability/read capacity, not write throughput. Kafka/ClickHouse at-least-once delivery can duplicate logs and derived counts. Local integration tests use reduced storage topology; provisioned AWS capacity, multi-AZ failure, restore, network policy enforcement and live Flux behavior require the acceptance exercises in `docs/scaling.md` and `docs/testing.md`.

Kafka consumers now scale from committed-offset lag through KEDA, with 36 partitions and a separately configurable ceiling. CPU-only scaling could miss an I/O-bound backlog. Event-ID keys avoid concentrating a single service on one partition. Scaling is bounded by partitions and downstream capacity; it cannot parallelize an old hot partition or repair an unavailable ClickHouse shard. KEDA and CPU HPA ownership are mutually exclusive, and database maintenance pauses the scaler. See [consumer operations](kafka-consumers.md).

## Service experience and eBPF review (0.5)

| Finding | Change / evidence | Remaining boundary |
|---|---|---|
| An installed AWS policy sidecar does not prove enforcement is enabled | Read-only preflight requires its actual enable flag and a ready DaemonSet; Cilium reuse rejects concurrent AWS enforcement and checks Cilium policy/Hubble settings | CNI chaining and policy migration remain owned by the networking team; no automatic CNI replacement |
| A profiling configuration can parse while using retention controls from an older architecture | Use Pyroscope v2 `retention_period`, check native configuration/API startup and pin the chart | Actual cleanup, S3 access and quorum recovery require live qualification |
| Required AZ anti-affinity on every profiling worker would prevent scaling past three pods | Reserve strict anti-affinity for the fixed three-member metastore; use AZ topology spread for scalable workers | Reviewed replica changes still require capacity and failover testing |
| Namespace and workload selection can be mistaken for a security boundary around privileged profiling | Namespace-scoped discovery, explicit workload labels and bounded frequency/retries; opt-in installation | Root/host-PID agent can observe the host; selection is not hostile-tenant isolation; use dedicated nodes where required |
| Profile limits could be mistaken for the shared log admission budget | Document per-distributor ingress limits and label/profile bounds; omit unsupported v1 series-limit settings | No shared per-service profile quota or proven global v2 series ceiling is claimed |
| Static Pages publication could disclose private catalogs or carry a privileged GitHub token | Build a fixed static file allowlist, keep imports local, and require a separate reviewed CLI publication step | Authentication, team authorization and multi-user approval workflows are separate platform work |
| A displayed SLO could imply alert delivery is configured | Generate behavior-tested burn-rate rules only for opted-in services and distinguish direct application metrics from observed spans | Owners must configure/test alert routes and instrument the declared metric contract |

## Operational support review (0.6)

| Finding | Change / evidence | Remaining boundary |
|---|---|---|
| Healthy scrape targets can conceal wholesale metric loss | Replace the empty-separator combined drop rule with one forbidden label per rule; native scraping through Arrow and remote write proves safe samples survive and identifier samples are rejected | Restored samples increase ingestion volume; migrate custom grouped rules and review capacity |
| A canary could accept stale data or partial ingestion | Require exact fresh log markers, metric values and trace IDs; fail rejected OTLP signals and stale worker health | Sampling budgets can legitimately drop synthetic traces; an independent monitor must poll each pod during a Mimir outage |
| Query-level scan limits conflict with a production read-only profile | Give the canary a separate existing Secret and a dedicated read-only profile with server-enforced limits; the pinned database rejects writes and limit overrides in tests | The account is still a regional log reader, and internal transport needs TLS/authentication hardening |
| A routing reconciler could take over an existing tenant or disclose receiver credentials | Require explicit tenant ownership; mount existing Secrets, reject redirects and keep rendered configuration out of logs/bundles; actual Mimir delivers owner/default alerts locally | Review the full-tenant handover and real receiver acceptance; disabling reconciliation does not remove stored routes |
| A support archive could copy secrets through pod specs or error messages | Project approved status fields and omit annotations, logs, specs and error bodies; test forbidden-field exclusion | Resource names and topology remain internal information requiring review before sharing |
| A debug form could impersonate another owner or target | Default-deny exact actor/region/cluster/namespace bindings, catalog ownership, workload-label verification and scoped pod reconciliation | The protected workflow supplies identity; CLI arguments are not authentication, and IAM/RBAC plus durable audit retention remain operator responsibilities |

## Pinned implementation references

- [Arrow receiver configuration, v0.148.0](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.148.0/receiver/otelarrowreceiver/config.go) and [Arrow exporter queue/protocol behavior](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.148.0/exporter/otelarrowexporter/README.md).
- [Trace load-balancing protocol support, v0.148.0](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.148.0/exporter/loadbalancingexporter/config.go).
- [Mimir 3.2.0 ingestion/query limits](https://github.com/grafana/mimir/blob/mimir-3.2.0/pkg/util/validation/limits.go).

The executable validations take precedence over mismatched prose documentation for the pinned release.
