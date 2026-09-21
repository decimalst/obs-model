# Kafka consumer capacity and backlog operations

The log topic has **36 partitions** by default, replication factor 3 and minimum ISR 2. Vector writers share the stable `clickhouse-writer-v1` consumer group. Each partition has one active owner within that group, so adding writers increases useful parallelism up to the partition count. Kafka partitions and ClickHouse shards are independent: any writer can send events to any storage shard through the Distributed table.

KEDA 2.20.2 queries Kafka directly and owns the writer HPA. It does not depend on Grafana/Mimir availability to read lag. The default is 3–12 writers, leaving headroom to raise the ceiling to 24 or 36 through a PR without changing the topic. All fields below are accepted by installer `--tuning`, the deployment contract's `tuning`, or regional HelmRelease values:

```yaml
scaling:
  logPartitions: 36
  writerReplicas: 3
writerAutoscaling:
  mode: kafka
  maxReplicas: 24
  lagThreshold: 5000
  pollingInterval: 15
  scaleDownStabilizationSeconds: 300
  scaleUpPodsPerMinute: 3
  scaleDownPodsPerMinute: 1
```

The Kafka lag target is 5,000 uncommitted records per desired consumer: 60,000 records requests roughly 12 consumers, and 120,000 requests roughly 24 if the ceiling permits. The HPA tolerances, partition ceiling, node readiness and configured scale-up limit govern actual convergence. The 15-second polling interval is not a promise of a 15-second deployment; active scaling also follows the Kubernetes HPA sync interval. Tune the record threshold using measured record size, processing rate and recovery objectives; lag is not bytes or oldest-record age.

The chart caps the effective maximum at `min(writerAutoscaling.maxReplicas, scaling.logPartitions)`. It allows at most three extra pods per minute, waits five minutes before scaling down, and removes at most one pod per minute. The group uses cooperative-sticky assignment and 60 seconds of termination grace. At least three consumers remain running. On repeated lag-query errors, KEDA's fallback keeps at least the current replica count or the configured minimum, whichever is higher; controller/HPA failure itself cannot guarantee scaling. A CPU HPA is emitted only for `mode: cpu`, never alongside the KEDA scaler. `autoscaling.enabled: false` disables both.

ClickHouse acknowledgements propagate back to Kafka source commits. Queued or in-flight data does not count as successfully consumed merely because Vector read it. A ClickHouse outage therefore leaves visible Kafka lag. Replay remains at least once and can duplicate inserts/counts after uncertain acknowledgements or abrupt termination. Do not change the consumer group to scale it: a new group would independently replay the topic.

## PR and recovery workflow

1. Check whether lag is growing across many partitions or concentrated in a few. Inspect `kubectl -n observability describe scaledobject vector-writer` and `kubectl -n observability describe hpa vector-writer-lag` for current metrics, scaling limits and errors. Query the Kafka consumer group with the pinned Kafka admin tools to inspect per-partition committed offsets, lag and owners. Check oldest queued timestamps, Kafka disk/retention, ClickHouse insert latency/quorum failures and available node capacity.
2. If consumers are the bottleneck, raise `writerAutoscaling.maxReplicas` in that region's release and merge the tested PR. Flux updates KEDA's desired ceiling; KEDA adjusts the Deployment as lag warrants. Verify assignments, lag slope, database latency, error/retry counters and node scale-up. More consumers can make an overloaded ClickHouse cluster worse; expand storage first when it is the bottleneck.
3. If the partition ceiling is reached, increase `scaling.logPartitions` in a separate reviewed PR before raising the ceiling. Strimzi reconciles the topic; verify its Ready status and actual partition count first. Existing records remain in their original partitions. Increasing partitions adds producer/consumer/broker overhead and does not redistribute historical backlog; one old hot partition still has only one consumer. Event-ID partitioning spreads new records, including those from a single busy service. It does not promise service ordering. CI rejects partition-count decreases.
4. If brokers are the bottleneck, add broker-only capacity and perform a reviewed replica reassignment; a partition count or pod limit change alone does not rebalance existing broker storage. Avoid changing consumer group protocol or partition assignment strategy casually; incompatible assignment protocols require a coordinated group restart.

During ClickHouse expansion/migration, `clickhouse.pauseWrites: true` sets zero writer replicas and pauses KEDA at zero. Wait for both controller reconciliation and consumer exit before touching topology. In CPU fallback mode the writer HPA is removed. Ingress continues to Kafka; retention (24 hours by default) and available disk must cover the maintenance window and subsequent recovery. Resume through a PR, then watch the backlog drain. See [ClickHouse expansion](clickhouse-sharding.md).

## Validation boundary

`make consumer-test` runs the production Vector consumer configuration against real Kafka and sharded ClickHouse containers. It verifies partition ownership, a durable backlog, group expansion/downscale, offset acknowledgements, and stored event identities/aggregate totals. Docker performs the replica changes explicitly. Pinned KEDA CRD validation, Helm invariants, controller ownership and Kafka network access rules are tested separately. A disposable EKS test must still prove that induced lag causes KEDA/HPA/node scaling, fallback on metric errors, a maintenance pause, and later scale-down. No consumer throughput or recovery-time guarantee is inferred from the small local fixture.

References: [KEDA Kafka scaler](https://keda.sh/docs/2.20/scalers/apache-kafka/), [ScaledObject controls and fallback](https://keda.sh/docs/2.20/reference/scaledobject-spec/), [pinned KEDA chart](https://github.com/kedacore/charts/releases/tag/v2.20.2).
