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

## Pinned implementation references

- [Arrow receiver configuration, v0.148.0](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.148.0/receiver/otelarrowreceiver/config.go) and [Arrow exporter queue/protocol behavior](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.148.0/exporter/otelarrowexporter/README.md).
- [Trace load-balancing protocol support, v0.148.0](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.148.0/exporter/loadbalancingexporter/config.go).
- [Mimir 3.2.0 ingestion/query limits](https://github.com/grafana/mimir/blob/mimir-3.2.0/pkg/util/validation/limits.go).

The executable validations take precedence over mismatched prose documentation for the pinned release.
