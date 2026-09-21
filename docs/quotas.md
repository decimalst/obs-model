# Quotas, sampling, and temporary debug collection

## Log admission contract

The key is `namespace/service`, taken from Kubernetes `observability/service` metadata. Missing service labels share the namespace's `unclassified` budget. A forged `service_key` inside application JSON cannot change this identity. The admission service validates key length/characters and hashes the key before storing it in Valkey.

The node agent shares read time across files, reading at most 2 KiB from a file before switching; it does not prioritize the oldest backlog. Individual and merged log lines are limited to 32 KiB, with oversized lines discarded. This also leaves headroom for JSON escaping below the admission service's 256 KiB serialized-record limit. Before JSON parsing, the node agent limits each service to 500 events/s on that node. At the regional layer, one atomic script spends four token buckets: total events, total serialized bytes, debug events, and debug bytes. All severity classes spend total tokens. There is no error exception. Debug traffic must have tokens in all four buckets. Other services have independent state. The proxy permits at most 512 events, 2 MiB/request, and 64 concurrent requests/process.

Default rates/bursts are in `charts/platform/values.yaml`. A full service bucket permits at most `burst + rate × elapsed` admission over an interval during steady operation. Blue/green replicas and HPA replicas share state rather than getting new buckets. Clock calculations use the Valkey server's time. Idle keys expire only after every bucket can fully refill.

Valkey errors/OOM return HTTP 503 and cause upstream retry/backpressure. Throttle drops return 200 so a flood is not retried. The node's 512 MiB disk queue is finite: when blocked long enough, kubelet log rotation can discard uncollected data. A failed gateway delivery may consume tokens before a retry; intentional retry-related loss and at-least-once duplicates are possible. Quota values are deployment policy; keep them identical across concurrent revisions. Change rates in a coordinated maintenance operation, then verify the shared-budget test and live drop rate. Asynchronous Valkey failover can briefly restore stale tokens; this is not a strict financial accounting mechanism.

Limits protect downstream processing/storage, not an arbitrarily large hostile attack on the agents or admission endpoint. Only trusted Vector agents may address the log path. Stable service labels and quota policy are operator-controlled. Multi-tenant identity/authentication and per-service contractual overrides require additional controls.

## Debug grants

Grant command:

```sh
python3 scripts/debug_grants.py grant --namespace applications --kind deployment --name checkout --hours 8
```

The CLI annotates the workload's **metadata**, not its pod template; applications are not restarted. It patches existing, controller-owned pods. The optional `debug-grants` chart reconciles grants onto new/replacement pods once per minute, with a namespace-only Role and no workload write permission. Ownership uses UIDs/owner references, not a mutable label selector. Patches test resource versions so concurrent changes are not silently overwritten.

Two annotations are copied:

- `observability.good-olly.io/debug-from`: UTC RFC3339 issue time.
- `observability.good-olly.io/debug-until`: UTC RFC3339 deadline.

Vector requires `from <= now < until` and `0 < until - from <= 24h`. Missing, malformed, expired, future-issued, and overlong grants fail closed. Collector evaluation is the enforcement mechanism; the reconciler's availability is not required for expiry. Keep node clocks synchronized. Workload annotations can remain as audit metadata after expiration; pod annotations are cleared on reconciliation. Kubernetes audit logging records grants and revocations.

The pipeline understands structured JSON `level` or `severity`. It uppercases these values; DEBUG/TRACE require a grant. Unstructured lines are treated as INFO; provide a tested parser before relying on debug filtering for a different log format. This permission cannot make an application emit logs it did not produce. Avoid logging secrets at any level; this baseline does not include a general-purpose PII detector.

The granted debug stream is 100% eligible **within** its rate/byte sub-budget; it has no unbounded bypass. Basic aggregate counts reflect retained/admitted data and must not replace unsampled application success/error counters.

## Traces and metrics

The trace sampler retains up to 50% of its span budget for errors, 25% for slow traces, and 25% for baseline candidates sampled at 10%. The final percentage depends on trace composition and traffic volume. The 2,000 spans/s ceiling is per sampler (6,000 across the initial fixed pool); it is not a regional shared quota. Trace caches/queues and memory limits bound collector memory but do not provide durable trace processing.

Metrics are not randomly sampled: scrape every 30 seconds, apply per-target sample/label limits, and enforce Mimir ingestion/active-series quotas. That preserves ordinary counter/rate meaning. Never add temporary grant IDs, request IDs, or debug timestamps as metric labels. The current single trusted `platform` metrics tenant does not isolate each service's metric allowance.
