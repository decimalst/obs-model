# ClickHouse sharding and expansion

Fresh installs now create **two write shards, each with three replicas**, for six ClickHouse data pods. Replicas of each shard are spread across three AZs. Increase `clickhouse.shards` at deploy time or through a regional GitOps release. The Keeper quorum remains three members. Replication protects copies of a shard; additional shards add write/storage capacity.

```yaml
clickhouse:
  shards: 2
  pauseWrites: false
```

The minimum supported production layout is two shards × three replicas. The default doubles ClickHouse data pods and PVC capacity compared with 0.2: six 100 GiB PVCs instead of three. Tune `resources.clickhouse` and `logs.clickhouseStorage` for your workload; no measured throughput or AWS price is implied. All shards stay in the originating region.

## How data is distributed

Agents attach the configured source `cluster_name` and a generated `event_id` before buffering. Application JSON cannot override those fields. Kafka partitions by event ID so one busy service can use multiple writer consumers. Logs no longer promise Kafka ordering within a service; query timestamps and event IDs explicitly.

Writers insert into `observability.logs`, a Distributed table using `cityHash64(cluster_name, service_key, event_id)`. Each record goes to one shard and its replicated local table. This deliberately spreads a single hot cluster/service across shards. Hashing only the cluster name would place that entire cluster on one shard and retain its write ceiling. Source cluster remains the first local sorting key and a query/filter field.

- `logs_local`: replicated storage on each shard.
- `logs`: one query/write entry point spanning every shard.
- `log_events_local`: replicated per-shard aggregates, maintained by a materialized view on `logs_local`.
- `log_events_shards`: internal Distributed aggregate source.
- `log_events`: public view that sums across shards, keeping cluster identity separate.

Grafana and the Vector writer keep the same public table names. Remote queries authenticate with the operator's cluster-secret reference; secret values are not embedded in Git. Distributed inserts run in the foreground and require two replica acknowledgements on the destination shard. An unavailable shard causes errors/backpressure instead of successful acknowledgements or silently partial queries. Delivery remains at least once; an uncertain response, replay or rebatch can duplicate records/counts. Event IDs are routing identities, not an exactly-once guarantee.

For stronger isolation, deploy an independent regional platform per EKS cluster or tenant using separate deployment contracts and Terraform state. A `cluster_name` column is attribution, not authentication or a tenant security boundary. This chart's agents collect from their own cluster; a shared cross-cluster intake requires explicitly authenticated connectivity and trusted source identity. Existing per-service quotas remain scoped to namespace/service within that platform cell.

## Safe expansion through three PRs

1. Set `clickhouse.pauseWrites: true`. Wait for Flux to reconcile and the writer Deployment to reach zero pods. KEDA is annotated with `paused-replicas: "0"` (the CPU fallback HPA is removed); agents/gateways keep filling the durable Kafka stream. Watch Kafka disk, lag and retention so the maintenance window stays within available capacity. Keep the source cluster running and allow in-flight inserts to finish.
2. Increase `clickhouse.shards` and leave writes paused. Shard names are stable ordinal strings; new shards are appended. The operator provisions replicas and copies schemas (`schemaPolicy: All`). The post-upgrade schema Job checks topology and applies idempotent DDL; the Helm release fails if it cannot establish the schema. Wait for the ClickHouseInstallation to report completion and verify **every** server reports the new shard/replica topology and all local tables. Run read/count checks through both writer and reader accounts. CI rejects shard reductions and increases without a preceding paused configuration; Git history cannot prove a live pause, so verify reconciliation before merging the next PR.
3. Set `pauseWrites: false`. Writers and their scaler resume, consume the Kafka backlog and spread new rows over the expanded topology. Monitor lag, insert latency, failed quorums, disk and row distribution per shard. All rows written before expansion remain queryable through the distributed tables.

Do not change the default shard count globally when only one region needs capacity; edit that region's release values. Coordinate schema/quota/collector changes separately from topology changes. GitOps's scoped replica-drift exception is retained for the writer during a pause, so do not manually scale it up; Helm applies zero replicas and KEDA enforces the pause; verify that its generated HPA is paused/removed and no consumers remain.

**Historical data is not automatically rebalanced.** Old shards retain their rows; new writes are spread across all shards. With seven-day TTLs, the old imbalance ages out over the retention window. If old shards need immediate relief, use a separately tested, partition-bounded copy/verification/deletion procedure. Never reduce shard count or delete a shard/PVC to force balancing. The PR guard blocks that operation; retained PVCs alone do not make unreadable data part of a query.

## Upgrade from the pre-0.3 single-shard schema

The new schema Job refuses a public `logs` table whose engine is not Distributed, or a `log_events` table whose engine is not View. `CREATE IF NOT EXISTS` must never conceal the old schema. Fresh installations need no manual migration.

For an existing 0.2 installation, use a maintenance window and take tested backups first:

1. Suspend its GitOps release. Stop the Kafka writer consumer cleanly and prevent its HPA from restarting it. Keep ingress in Kafka; verify retention and capacity cover the migration. Record Kafka offsets and old row/aggregate counts. Quiesce mutations or other external writers too.
2. On the original ClickHouse cluster, rename `logs`, `log_events` and `log_events_mv` to unique `*_legacy` names using reviewed `RENAME TABLE ... ON CLUSTER logs` commands. Retain every original table/PVC. Detach the legacy materialized view after the rename. Verify the names on all replicas before proceeding.
3. Apply 0.3 with `pauseWrites: true`. Let the schema hook create the new local/distributed tables and let the operator finish provisioning the second shard. Check the schema and replica state on every node.
4. Backfill from **one original replica**, exactly once, into the new Distributed `observability.logs`. Supply the original source cluster and a new UUID for each legacy record:

   ```sql
   INSERT INTO observability.logs
   SELECT 'YOUR_SOURCE_CLUSTER', generateUUIDv4(), timestamp,
          service_key, service_name, namespace, level, message, trace_id
   FROM observability.logs_legacy;
   ```

   Do not run this query `ON CLUSTER` or read all legacy replicas: that would multiply the data. For large datasets, backfill bounded immutable time partitions and keep a completion ledger; never blindly retry an uncertain completed insert. The new local materialized view rebuilds aggregates, so do not separately copy the old aggregate rows.
5. Verify per-cluster/day/service row counts and sums, quorum health, and Grafana reads. Resume the new writer at its recorded consumer offsets and then resume GitOps with `pauseWrites: false`. Keep legacy data until acceptance and backup retention permit explicit retirement.

This migration procedure is deliberately operator-controlled and has not been executed against an AWS cluster. Before resuming writes, rollback can restore the retained legacy names/configuration. After new writes enter the sharded schema, a chart rollback alone cannot merge that new data back into the old shard; perform a planned reverse migration or restore instead.

## Evidence and limits

`make sharding-test` runs real ClickHouse 25.8.33.6 servers: two shards × three replicas, hot-service distribution for two source clusters, aggregate/query correctness, writes after one replica is stopped, expansion to three shards, legacy-schema rejection, and failed reads/writes when an entire destination shard is unavailable. Local Keeper is one test instance. This proves storage behavior, not live operator reconciliation, AZ scheduling, cloud failover or a throughput number.

References: [ClickHouse Distributed engine, foreground inserts and cluster authentication](https://clickhouse.com/docs/engines/table-engines/special/distributed), [pinned operator schema policies and shard anti-affinity](https://github.com/Altinity/clickhouse-operator/blob/0.27.3/docs/chi-examples/99-clickhouseinstallation-max.yaml).
