# Architecture and boundaries

Each AWS region is an independent ingestion and storage domain. Reusing the region module does not share state, data-plane dependencies, or S3 buckets. AZ replication protects against a local failure; it is distinct from regional disaster recovery. Region loss leaves that region's historical telemetry unavailable until recovery. Route applications in a surviving region to that region's collectors. A global DNS failover or raw-data replica is deliberately not provisioned.

## Logs and noisy neighbors

Vector derives identity from Kubernetes metadata, applies an early per-service/node throttle before parsing application JSON, normalizes an allow-list of fields, evaluates debug grants, and buffers accepted logs on the node. It sends bounded NDJSON batches to the shared admission tier. That tier atomically spends event and byte tokens for each namespace/service in a regional Valkey primary, then forwards admitted records to Vector gateways. All levels, including ERROR/FATAL, spend the same total budget. DEBUG/TRACE additionally spend a smaller sub-budget.

Vector gateways acknowledge only after Kafka accepts writes with `acks=all`; Kafka uses three replicas and two in-sync replicas. A separate Vector consumer group writes to ClickHouse. Source offsets use end-to-end acknowledgements. ClickHouse uses a three-member Keeper quorum and two shards with three replicas each, with write quorum two. Distributed foreground inserts route records by cluster/service/event identity; public queries span all shards. Replicated materialized views generate per-minute service/severity counts. Delivery is at least once; retries can duplicate rows and event counts. The counts describe admitted telemetry, not original application error totals.

This adds a small Go admission service to the reference design because Vector's throttle state is local to a process. Local throttles alone do not enforce shared quotas when gateways scale. The quota store uses `noeviction`; exhausted memory fails admission closed rather than silently resetting tokens. The shared store itself requires capacity planning and is a regional dependency. Its failover is asynchronous and can restore slightly stale token state; a failover may admit an additional burst. This is traffic protection, not an exact billing ledger.

Applications live outside the trusted `observability` namespace. Network policies admit application traffic only to the node agents' OTLP ports; log gateways, Kafka, databases, and quota endpoints remain internal to administrators and collectors. Labels and grant annotations are platform-controlled metadata. This baseline is not hardened multi-tenant hosting: do not grant untrusted users pod creation/exec privileges inside the platform namespace or permission to forge service labels/debug grants. Plaintext intra-cluster OTLP/Kafka/ClickHouse is confined to that trust boundary. Install authenticated TLS ingress/service-mesh mTLS before admitting untrusted or external clients.

## Metrics and traces

OTel DaemonSets accept OTLP and collect kubelet node/pod/container metrics. The Operator's two Target Allocator replicas occupy different AZs and distribute annotated Prometheus targets across a three-replica scrape StatefulSet using consistent hashing. Discovery changes and collector failures can cause transient assignment changes; live acceptance must measure scrape gaps and duplicates during those transitions. Scrape limits cap samples/labels per target. Regional Mimir tenant limits cap ingestion and active series. These are **not** per-service metrics quotas; use separate trusted tenants or an authenticated metrics admission tier if that isolation is required.

Metrics gateways export Prometheus remote write to Mimir. Mimir 3 uses the same regional Kafka cluster's separate `mimir-ingest` topic. There is one partition for the initial one-ingester-per-zone layout. Scale partitions and ingesters deliberately together; partition count does not automatically follow pod HPA. Co-locating logs and Mimir Kafka saves a broker fleet but couples capacity. Hard log budgets bound this pressure; for stronger workload isolation, use a separate Mimir broker fleet.

A stateless routing tier hashes trace IDs to three stable sampler hostnames. Samplers keep traces in memory for 30 seconds and prefer errors, then slow traces, then 10% baseline candidates, within a 2,000-span/s ceiling per sampler. Composite allocations are 50% / 25% / 25%. Every span for a trace must reach the same sampler; consistent hostnames preserve ring membership during router blue-green changes. Samplers are not HPA targets. A sampler crash can lose in-flight traces; tail state is not durable. Cross-region traces are sampled independently and may be incomplete. SDK head sampling would pre-discard errors that tail sampling cannot recover.

## Availability and deployment strategy

| Component | Strategy | Limitation |
|---|---|---|
| Log quota, Vector gateway, OTel router/metrics gateway | Argo blue-green, full replicas, preview readiness checks, manual promotion | Readiness does not prove data delivery; smoke checks precede promotion |
| Node agents | One-at-a-time DaemonSet rolling update | Node disk queues disappear if the node is lost |
| Tail samplers | Stable three-member StatefulSet, `OnDelete`, controlled one-at-a-time replacement | In-memory sampling decisions are lost on restart; no lossless upgrade claim |
| Kafka / Keeper / ClickHouse | Replication, PDBs, AZ placement, operator or ordered updates | Quorum must stay healthy; never clone a live ring as a blue-green database |
| Mimir / Tempo | Upstream distributed charts with explicit AZ selectors | Tempo chart marks zone-aware mode experimental; failure testing is required |
| Grafana | Two replicas, shared Multi-AZ PostgreSQL, rolling deployment | Database schema rollback compatibility must be checked; HA alert evaluation is disabled |
| EKS node groups | One group per AZ, cluster autoscaler, one node unavailable per group update | Capacity/EC2 quotas and spare headroom still matter |
| Controllers | Multiple replicas where the pinned controller supports leadership | Altinity operator remains a single controller; data plane survives its restart |

Grafana runs in each region and presents all three local signals. A single global browsing location can provision separate remote region data sources, but that requires private connectivity and authentication not configured here. It should issue bounded remote queries rather than moving all raw telemetry. A regional Grafana database is not replicated across regions; keep dashboards and alert rules in Git.

Flink is optional because a business-specific job is not inferable from the diagram. The default derived counts use ClickHouse materialized views, avoiding an idle Flink fleet. The extension supplies Kubernetes HA, regional checkpoints, savepoint upgrades, and a pinned operator, but requires an application image, job JAR, and connector semantics; see [Flink](flink.md).

## References used during implementation

- [Vector throttle scope and behavior](https://vector.dev/docs/reference/configuration/transforms/throttle/)
- [Vector end-to-end acknowledgements](https://vector.dev/docs/about/under-the-hood/architecture/end-to-end-acknowledgements/)
- [OTel gateway and trace-ID routing](https://opentelemetry.io/docs/collector/deploy/gateway/)
- [Pinned tail sampling processor](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/v0.148.0/processor/tailsamplingprocessor)
- [Target Allocator](https://github.com/open-telemetry/opentelemetry-operator/blob/main/docs/target-allocator/README.md)
- [Mimir production Helm deployment](https://grafana.com/docs/helm-charts/mimir-distributed/latest/run-production-environment-with-helm/)
- [Argo blue-green behavior](https://argoproj.github.io/argo-rollouts/features/bluegreen/)
- [EKS supported Kubernetes versions](https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions.html)

The diagram was treated as reference data, not as deployment instructions. See `Chart.lock`, Terraform provider lock files, and the pinned container versions for the exact implementation inputs.

## Reconciliation and interconnects

[GitOps](gitops.md) makes this repository the desired state for pipeline configuration and Grafana/Mimir content. [The adversarial review](adversarial-review.md) records fixed flaws and explicit limits. [Scaling operations](scaling.md) distinguishes elastic tiers from quorum, partition and trace-state migrations. Compatible agent/scrape links use OTel Arrow by default; trace-aware routing and Tempo export retain OTLP.

## Service RED metrics

The agent's OTLP receiver fans out into the original trace path and a separate span-metrics branch before tail sampling. The branch selects server spans, normalizes only its copy, bounds aggregation, and gives generated metrics a distinct agent writer identity. It rejoins the Arrow metrics transport and existing Mimir policy. Direct application metrics retain their own writer identities. See [RED semantics and cardinality scope](service-red.md) and [remaining capabilities](platform-gaps.md).

## Optional eBPF visibility and service experience

The regional cell can add Alloy eBPF CPU collection and Pyroscope v2 storage, under independent profile ingestion/retention limits. Terraform owns only newly requested storage/roles; an existing cluster's Cilium remains externally owned. Hubble network counters join the regional scrape pipeline and have a dedicated Grafana dashboard. See [exact ownership, privilege, HA and qualification contracts](ebpf.md).

A Git-owned service catalog supplies stable service identity, ownership, runbooks, optional availability SLOs and application scaffolds. A static configuration studio generates reviewable regional configuration and service definitions locally, without cloud credentials or deployment privileges. Catalog/configuration definitions are desired state; live readiness, ownership authority and telemetry completeness are not inferred from their existence.
