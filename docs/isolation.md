# Regional isolation and budget review

Choose the failure boundary before tuning rates. Shared log admission is enforced per namespace/service, including errors and temporary debug logs. Metrics, traces and profiles still share backend and collector resources within a cell. Sampling percentages, series limits and memory queues bound consumption but do not reserve a fair share for each service.

| Configuration | Setup | Suitable boundary |
|---|---|---|
| Shared regional cell | One platform installation; tune tenant, sampler and collector budgets through reviewed values | Trusted teams accepting a shared metrics/trace/profile failure domain; lowest baseline footprint |
| Separate clusters and backends per team or workload class | One existing or managed EKS installation per cell, separate state, buckets, roles, Grafana database and quota endpoint; route each workload to its own local collectors | Reduce contention between high-volume and latency-sensitive teams; higher baseline cost |
| Authenticated multi-tenant shared intake | Requires an independently managed identity-aware intake enforcing tenant authorization and quotas, plus backend tenant provisioning | A future integration; setting `X-Scope-OrgID` or trusting SDK service labels alone does not implement it |

The current installer supports **one platform per cluster**. Do not simulate isolation by installing a second release into another namespace: fixed names, trust boundaries and shared node agents are not designed for that. For isolated cells, applications must use the collectors belonging to their cell. Forwarding arbitrary services from shared producer clusters requires a separately qualified authenticated intake design. Public OTLP listeners are not created here.

## Preview configured budgets offline

```sh
.venv/bin/python scripts/cell_plan.py --config examples/cells.yaml --output build/cell-plan.json
```

The output distinguishes namespace/service log budgets, tenant metrics/Tempo budgets and per-pod tail budgets. It includes the consumer ceiling and write-shard count without claiming measured capacity or dollar savings. It makes no AWS/Kubernetes calls and refuses to overwrite an existing report. For production, replace the example platform reference with your nonsecret installation contract; `tuning` overrides flow into the preview.

For stronger separation, prepare independent platform contracts and use:

```yaml
version: 1
mode: isolated-clusters
cells:
  - id: critical-east
    platform: critical-platform.yaml
    grafanaDatabaseIdentity: critical-db.example.internal/grafana
    services: [payments/checkout]
  - id: batch-east
    platform: batch-platform.yaml
    grafanaDatabaseIdentity: batch-db.example.internal/grafana
    services: [analytics/importer]
```

Each isolated contract must include its exact cluster ARN. The planner rejects reused declared clusters, object buckets, workload roles, quota endpoints, Grafana database identities and duplicate service assignments in a region. With AWS secret seeding, the database identity is derived from the contract's host and database name; an optional `grafanaDatabaseIdentity` must match those coordinates. With externally owned Secrets, supply that identity explicitly because the offline planner cannot read the actual host. These checks detect configuration mistakes; they do not resolve DNS aliases or inspect actual IAM permissions, shared physical database capacity or network access. Use independent database instances and workload/node pools if those bottlenecks also need isolation. Keeping a common Grafana frontend or federated discovery requires explicitly qualified datasource access; it is not generated automatically.

Use either the new-region or existing-cluster Terraform path with a unique state key and platform name per cell, or software-only installation with separately owned infrastructure. Generate values using `install.py --render-only`, then review the diff and cell plan. Deploy later through the [deferred qualification batch](live-qualification.md). Never move an existing service by repointing its collectors mid-trace: drain SDK/exporter queues and trace state, retain old query access, and document the cutover and any telemetry gap. Historical data stays in its original regional backend.

For cost review, record node/pod counts, PVC and object retention, measured samples/spans/bytes per second, compression, cross-AZ replication and expected queries. Apply your account's actual regional prices separately. No generic collector throughput multiplier or invented AWS price is included. Cross-region replication remains opt-in with an explicit RPO and egress assessment.
