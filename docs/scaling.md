# Capacity and routine operations

Use `resources.agents`, `gateways`, `samplers`, `writers`, `quota`, `scrape`, `targetAllocator`, `kafka`, `clickhouse` and `keeper` to size custom components without editing templates. Requests cannot exceed limits. Native Mimir/Tempo/Grafana chart values retain their resource controls.

Scaling is explicit at each bottleneck. Stateless tiers are elastic; persistent placement and trace state need controlled changes. Tune with measured queue occupancy, consumer lag, drop/refusal counts, memory and storage headroom, not CPU alone. CPU HPA requires the preflight-verified metrics API and node capacity. Kafka writers use KEDA consumer lag by default; CPU scaling remains an explicit compatibility fallback. Neither can fix an overloaded downstream store by itself.

| Component | Scaling control | Operational procedure |
|---|---|---|
| Vector/OTel node agents | DaemonSets follow nodes; log service/node throttle | Scale EKS groups; remember the independent shared service quota still applies |
| Shared log admission, Vector gateways, OTel routers/metrics | HPA 3–12, `autoscaling.maxReplicas`/`targetCPU` | Preview then promote; monitor refused data, buffer occupancy and dependency latency |
| Vector Kafka writers | KEDA lag scaling 3–12; `scaling.writerReplicas`, `writerAutoscaling.maxReplicas`/`lagThreshold` | Ceiling is capped at the partition count; see [consumer operations](kafka-consumers.md). Maintains one consumer group and committed offsets |
| Scrape pool | `scaling.scrapeReplicas`; two target allocators | Target allocation redistributes ownership; allow at least one scrape interval and inspect target duplication/gaps |
| Tail samplers | Fixed three, `maxTraces`, decision window and span budgets | Drain or provision another isolated regional cell. Do not change the consistent-hash membership under active traces |
| Kafka | Fixed 3 combined quorum members; `scaling.extraKafkaBrokers` in multiples of 3 | Add broker-only capacity, rebalance partitions with a reviewed reassignment, verify rack placement/ISR, then remove old capacity if needed |
| Kafka log topic | `scaling.logPartitions` (36) | Increasing partitions can change keyed ordering; review consumers and lag. Never decrease partitions in place |
| Mimir ingest storage | `scaling.metricsPartitions` and `mimir.ingester.replicas` | At least three ingester replicas per partition (one per AZ). Add ingesters and activate partition ownership before increasing topic partitions; use staged PRs and Mimir's partition-ring procedure |
| Mimir distributors/queriers/frontends/rulers/store gateways | Native pinned dependency values | Increase stateless replica/resources independently; for ingesters/stores preserve AZ replication and PVC ownership |
| Tempo | Native distributor/querier/frontend resources/replicas; ingester ring/PVCs | Stateless expansion is routine. Ingester expansion/removal needs the Tempo ring and graceful shutdown procedure |
| ClickHouse | `clickhouse.shards` (2 by default), 3 replicas/shard, `resources.clickhouse`, storage | Pause Kafka writers, append shards through a reviewed PR, verify topology/schema, then resume. Distributed tables route writes and query all shards; see [sharding](clickhouse-sharding.md) |
| Keeper / Valkey / Grafana PostgreSQL | Quorums/managed AWS services; Terraform node/storage sizing | Retain quorum/AZ guarantees; use AWS/operator procedures and test failover/backups |
| Grafana | Two replicas on regional PostgreSQL; native resources | File provisioning keeps content identical; use a regional ingress and separate database capacity sizing |

The initial one-partition Mimir ingest topic is intentionally a small starting topology. The schema checks prevent requesting more partitions than the ingester replica count can cover, but cannot prove the live partition ring has finished activating. Never automate that migration from a single replica-count edit.

Terraform infrastructure changes remain a PR→reviewed plan→apply operation against the existing regional state. Use an OIDC-authenticated CI runner inside network reach of the private EKS API for automated plans/applies; do not put long-lived AWS keys into this repository. Deployment-specific role ARN, backend and approval environment must be configured before wiring that runner. The included CI does validation without AWS access.

## Operating checklist

1. Establish a load baseline and worst-case error/debug storm, including a quiet neighboring service. Observe shared drops and node fairness.
2. Size blue/green capacity for both generations. Set a cost ceiling on HPA and node groups. Autoscaling cannot help if AZ placement or PVCs cannot schedule.
3. Publish the desired values/content through a reviewed PR, inspect Flux readiness and Argo progress separately, and run the regional smoke test. Flux Helm success deliberately does not mean every paused rollout is healthy.
4. For a quota-policy change, the new pods may return 503 until the old policy drains and its bucket TTL expires (`max(burst/rate)+1s`; clock rollback can extend it). Agent buffers and retries absorb the transition within their limits. Do not purge active quota keys to force it.
5. For sampler upgrades, follow the drain procedure in `runbooks.md`. For broker/disk/shard/schema changes, record forward and rollback/migration steps in the PR. A Git revert does not recreate deleted data.
6. Before production acceptance, test one-AZ loss, Valkey and PostgreSQL failover, broker replacement, sampler loss, buffer exhaustion, Grafana/ruler file reload, Flux drift correction and backup restoration. Record measured loss, recovery time and regional costs.

When upgrading from the first quota image that predates policy fingerprints, roll out the new image with unchanged budgets first. Change budgets in a subsequent PR after every gateway runs the new code; the older Lua script cannot enforce a fingerprint it does not know about.

RED aggregation lives on the node agents and scales with node count. Its `spanMetrics.aggregationCardinalityLimit` is per resource group and `resourceMetricsCacheSize` is per agent; the exported series footprint multiplies by active writers and histogram buckets. These are not global tenant quotas. Changing route dimensions, bucket counts or service churn requires memory/CPU/series measurements; see [RED controls](service-red.md). Node agents roll without overlapping collectors on the same node; restarted metric streams need warmup before rates are reliable.
