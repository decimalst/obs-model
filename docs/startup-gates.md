# Log pipeline startup gates

The chart holds log admission and consumption until their immediate dependencies exist. This protects already opted-in namespaces during first installation; an installer-only sequence cannot protect those clients or repeat reliably under GitOps.

| Workload | Required before its main process starts |
|---|---|
| Vector gateway | The actual Kafka `logs` topic has exactly the configured partition count. Every partition has a leader, three assigned replicas, and at least two distinct in-sync replicas including its leader. The pinned Kafka CLI performs metadata reads; it never creates topics. |
| Vector writer | All expected ClickHouse shard × replica endpoints expose the six correctly typed `observability` schema objects: `logs_local`, `log_events_local`, `log_events_mv`, `logs`, `log_events_shards`, and `log_events`. The query uses `clusterAllReplicas` with unavailable-shard skipping disabled. |
| Log quota | The active Vector gateway Service returns HTTP 200 from `/health`. Waiting here avoids spending quota tokens while the gateway is still waiting for Kafka. Preview Services are not used. |

Kafka can retain accepted messages while writers wait for ClickHouse. The stable `clickhouse-writer-v1` consumer group, Kafka acknowledgements and ClickHouse completion acknowledgements are unchanged. No gate adopts controllers, changes Service selectors, creates a consumer group, grants Kubernetes API access, or reads the Secret API. The writer gate uses its existing mounted credential through the ClickHouse client configuration, without putting its value in arguments or logs.

The gates run as nonroot init containers with a read-only root filesystem and bounded temporary storage. Each invocation has a finite retry count and request timeout. Exhaustion exits with failure; Kubernetes may restart the init container, keeping ingestion or consumption blocked. Init logs report only the gate and retry count. Raw database/client error output is suppressed.

## Configuration and deployment

Helm values and a region's GitOps `HelmRelease.spec.values` accept:

```yaml
startupGates:
  attempts: 120
  delaySeconds: 5
  timeoutSeconds: 20
images:
  kafkaCli: apache/kafka:4.2.1
```

The same `startupGates` object is accepted in a Python installer/compiler `--tuning` file, including partial overrides; omitted controls retain chart defaults. The public configuration studio's Operations step exposes these three controls and preserves them in request imports/exports and generated `tuning.json`. Partial imported settings fill the omitted defaults, while unknown fields or values outside the bounds are rejected. Image selection remains outside tuning and the studio controls. Keep selected tuning with the deployment's reviewed configuration.

The chart schema bounds attempts to 1–360, delay to 1–30 seconds, and each request timeout to 5–60 seconds. Kafka and ClickHouse clients receive a forced termination after five additional seconds if they do not honor the normal timeout. With defaults, an invocation can take approximately 50 minutes if every request consumes its timeout, or up to approximately 60 minutes if every client needs that forced termination; fast failures retry for approximately 10 minutes. A Helm operation can time out earlier while init containers remain blocked. Increasing these bounds does not repair a missing topic, incomplete migration or insufficient broker quorum. Existing alternate registry images must retain the pinned clients and tools used by these checks.

Preserve the supported installer's platform deployment without `--wait`, and Flux's platform `install.disableWait: true` / `upgrade.disableWait: true`. The schema Job runs as a `post-install,post-upgrade` hook. Making Helm wait for initialized writers **before** running that hook creates a dependency cycle. Hooks must remain enabled. Operators and the schema hook keep their own readiness checks and deadlines. Schema ownership stays with the existing hook; writer init containers never execute DDL. Deployments with `clickhouse.schemaJob: false` must establish the required schema through their explicit external procedure.

Before enabling new client namespaces, run fresh stored-signal canaries after the platform is ready. For namespaces already collecting, agents use their existing finite disk buffers while the quota Service has no ready endpoints. Monitor buffer utilization, source-file rotation and the startup gate status; no unbounded retention is implied.

## Boundary and qualification

The disposable AWS bootstrap exposed real synthetic-log loss: gateways encountered `UnknownTopicOrPartition` before Kafka provisioning completed, and writers encountered non-retriable insert errors before schema creation. Later fresh canary success establishes current delivery; it does not recover or erase those earlier drops.

These gates address initial dependency ordering and apply again when a new Pod starts. They do not continuously certify backend health, prevent all runtime data loss, refund quota tokens after downstream failures, or make source-file collection transactional. A backend can fail after an init check passes. Stateful migrations still require the documented write pause and roll-forward procedure.

Offline checks:

```sh
.venv/bin/python -m unittest tests.test_startup_gates -v
make startup-gate-test
```

The second command creates and cleans up disposable **local Docker** fixtures: three Kafka brokers and a two-shard ClickHouse fixture. It exercises the actual pinned CLI images and nonroot/read-only init settings. This is not AWS qualification. A new cold AWS installation with pre-labelled client namespaces must still prove that gates block before topic/schema readiness, fresh markers reach storage after release, all accepted markers are accounted for, and the supported Helm/Flux hook sequence has no deadlock. The already-running lab is not silently changed to claim that result.

References: [Helm hook ordering](https://helm.sh/docs/topics/charts_hooks/), [Flux Helm wait controls](https://fluxcd.io/flux/components/helm/helmreleases/). Vector's pinned ClickHouse healthcheck runs only `SELECT 1`, so process health alone cannot establish schema readiness: [Vector 0.54 source](https://github.com/vectordotdev/vector/blob/v0.54.0/src/sinks/clickhouse/config.rs).
