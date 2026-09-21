# Optional Flink extension

Default derived per-minute counts run as replicated ClickHouse materialized views. They represent admitted logs and share the pipeline's at-least-once duplicate semantics. No separate Flink cluster is charged for that simple aggregation.

For a custom streaming job, install the optional pinned controller after cert-manager:

```sh
helm dependency build charts/flink-operator
helm upgrade --install flink-operator charts/flink-operator -n observability-system --wait
```

Enable `flink.enabled` and provide an immutable application image, `jarURI`, and the regional Flink bucket output. The image must include the job JAR, compatible Kafka/ClickHouse connectors, and the S3 filesystem plugin compatible with Flink 2.1. Credentials use the `flink` Pod Identity service account. The template provides two JobManagers, Kubernetes HA metadata, regional S3 checkpoints/savepoints, and savepoint upgrade mode.

The job is application-specific and is **not supplied or executed** by this repository. The checkpoint mode alone does not guarantee exactly-once ClickHouse writes; implement and test sink idempotency/transactions explicitly. A blue-green pair consuming the same business stream can duplicate outputs, so use savepoint-based controlled upgrades.

Use [the Flink operator release notes](https://flink.apache.org/2026/02/15/apache-flink-kubernetes-operator-1.14.0-release-announcement/) and [job lifecycle documentation](https://nightlies.apache.org/flink/flink-kubernetes-operator-docs-main/docs/managing/job-management/) when selecting job artifacts and planning upgrades.
