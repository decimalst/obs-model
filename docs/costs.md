# Cost and capacity model

The cost objective is regional locality with explicit telemetry ceilings, not the smallest possible single-node bill. The HA baseline in each region includes six `m7i.xlarge` nodes, three NAT gateways, replicated gp3 volumes, Multi-AZ PostgreSQL, and three Valkey nodes. A second active region roughly repeats that fixed floor. No dollar estimate is supplied without current AWS prices, account discounts, workload volumes, and a target region inventory.

## Egress choices

- Agents, admission, Kafka, ClickHouse, Mimir, Tempo, and S3 remain local to the application region.
- There is no cross-region S3/Kafka replication, peering, or automatic bulk ingestion failover.
- Each AZ has a local NAT path. A single shared NAT would lower its fixed fee but introduce a failure dependency and cross-AZ egress.
- S3 gateway endpoints keep bulk object traffic off NAT. Public registry image pulls still traverse NAT; a regional ECR mirror can reduce repeat pulls and external dependencies.
- Kafka advertises racks and Mimir ingester rack hints align with AZ names, reducing avoidable cross-AZ reads. HA replication still transfers data between AZs and is not free.
- Remote Grafana queries, if later configured, transfer selected results; cap query ranges and avoid full raw-log exports.

## Volume equations

For each service, maximum steady admitted bytes/day is `bytes_per_second × 86,400`, plus its burst allowance. The default 1 MiB/s is approximately 84.4 GiB/day per continuously saturated service **before compression**. This is a protective ceiling, not an economical average target. Reduce it for ordinary services based on measured traffic; do not assume 1,000 services can each saturate this allowance on the initial fleet.

Kafka disk budget must cover aggregate admitted compressed bytes × retention × replication factor, plus segment/index overhead and headroom. With three 100 GiB broker volumes and RF=3, the useful replicated log capacity is much less than 300 GiB. Mimir shares these brokers and consumes part of the same capacity. Alert well before disk exhaustion and scale storage/retention against actual compression. ClickHouse's three 100 GiB replicas similarly do not provide 300 GiB of unique retained log capacity.

Sizing is iterative: set expected services/events/bytes, measure compression and average spans per trace, run controlled load tests, then set ingestion budgets and node/storage maxima. The configured initial storage sizes are examples, not guarantees that a saturated fleet fits seven days of retention.

## Retention and cleanup

Backend compactors manage active object retention. Terraform only removes old object versions and incomplete uploads, avoiding deletion of live Mimir/Tempo blocks based on age alone. S3 versioning has a seven-day noncurrent tail, so deletion does not immediately eliminate every stored byte. ClickHouse TTL cleanup is asynchronous during merges. Kafka retention is segment-based, not an exact per-record deadline.

Trace sampling exports 10% baseline candidates plus bounded priority traffic; it does not avoid the cost of receiving all spans needed for tail decisions. Filter or head-sample at the SDK only with an explicit acceptance that later errors cannot be recovered. Debug grants time-bound exceptional collection and remain inside the service's total quota.

Use the HPA and cluster-autoscaler maxima as cost ceilings, monitor pending pods and ingestion refusal, and budget transient blue-green capacity. Neither an HPA nor a token bucket is a replacement for capacity planning.
