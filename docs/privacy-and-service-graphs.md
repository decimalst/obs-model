# Pipeline privacy and service dependency graphs

Both features are opt-in and configured in the regional tuning file passed to the installer or GitOps exporter. Their settings are checked by the installer and Helm schema. Commit policy changes through the shared infrastructure repository; validate and promote them using the normal regional release procedure. No credentials are needed for these features.

## Choose a privacy policy

The platform cannot determine which application fields your organization permits. Review representative synthetic records with application owners before enabling a policy. Privacy filtering does not replace removing secrets at their source.

| Setting | Behavior | Suitable starting point |
| --- | --- | --- |
| `privacy.logs.mode: off` | Existing normalization allowlist, no message scrubbing | Existing upstream sanitization policy |
| `privacy.logs.mode: redact` | Replace matching message substrings with `[REDACTED]` | Preserve surrounding troubleshooting context |
| `privacy.logs.mode: drop` | Discard an entire log event when its message matches | Reject messages carrying recognized sensitive content |
| `privacy.traces.mode: off` | Preserve the existing trace stream | Existing SDK privacy policy |
| `privacy.traces.mode: allowlist` | Retain only approved resource/span attributes, clear free text and events, reject spans containing links | Minimize exported trace payloads |

Example regional tuning:

```yaml
privacy:
  logs:
    mode: redact
    patterns: [email, bearer, credential]
  traces:
    mode: allowlist
    keepSpanName: false
    allowedSpanAttributes:
      - http.request.method
      - http.response.status_code
      - http.status_code
      - rpc.system
      - db.system
      - messaging.system
    allowedResourceAttributes:
      - service.name
      - service.namespace
      - service.instance.id
      - k8s.namespace.name
      - k8s.pod.uid
      - k8s.node.name
      - cloud.region
      - k8s.cluster.name
```

The `email`, `bearer`, and `credential` presets are independently selectable. The credential preset handles common password, secret, API key and access token assignments, including double-quoted JSON string values. Presets are reviewed VRL in `_privacy.tpl`; arbitrary executable expressions are not accepted as deployment values. Extend the preset implementation through a reviewed PR with positive and negative native fixtures when a new application format needs coverage.

Log privacy runs after identity normalization/debug expiry enforcement and before the Vector agent's disk-backed outgoing buffer. It also runs at the regional Vector gateway before Kafka. These steps retain only the normalized envelope fields (`cluster_name`, `event_id`, `timestamp`, `service_key`, `service_name`, `namespace`, `level`, `message`, `trace_id`; the temporary `debug_allowed` field is removed by the agent cleanup). Malformed/non-string messages fail closed. Event dropping acknowledges intentional loss and is not an outage retry. Runtime debug grants never bypass privacy policy.

Patterns inspect the message only. They are not a comprehensive PII detector: encoded tokens, novel formats, payment information, arbitrary identifiers, nested escaped strings, and sensitive identity fields can evade these presets. Matches can also be false positives. Raw container files and application SDK buffers still contain the application's original data. Keep credentials and personal information out of service names, namespace names, trace IDs, event IDs, metric labels and other retained identity fields.

Trace allowlisting runs at agent, router and sampler ingress, including the RED and service-graph branches. It:

- Keeps only `allowedSpanAttributes` and `allowedResourceAttributes` and clears instrumentation-scope attributes/name/version.
- Replaces span names unless `keepSpanName: true` is explicitly selected; clears status messages and trace state.
- Removes all span events. The pinned OTTL setter cannot clear links by assigning a literal empty array, so this policy **drops the entire span if it has links**, including messaging spans with causal links.
- Retains trace/span/parent IDs, kind, timing and status code. Allowlisted attribute **values** are retained as supplied, including complex values; review what the SDK puts in those fields. The schema prevents removing the resource identity keys required for routing/query usability, but does not authenticate those identities.

Setting `keepSpanName: true` is an explicit tradeoff for applications whose operation names are already scrubbed. It preserves names verbatim. There is no claim that this switch redacts raw URLs or SQL. Removing `http.route` from the attribute allowlist also removes the optional RED route dimension's source. Dropped linked spans do not contribute to RED or graphs, so neither represents complete request accounting under that policy.

OTTL errors propagate instead of allowing unprocessed spans through. An unavailable or incorrectly configured collector can still drop or backpressure telemetry; test the policy before rollout. Metrics, profiling labels, Hubble events, and preexisting stored data require their own application/data-governance policies.

## Enable bounded service graphs

The optional service-graph connector pairs observed client and server spans at the existing trace-ID-affine sampler tier **before** tail sampling. Node agents cannot reliably pair cross-node calls themselves. The original tail-sampled branch does not receive the graph branch's attribute removals.

```yaml
serviceGraph:
  enabled: true
  services: [checkout, payments, inventory]
  storeTTLSeconds: 30
  storeMaxItems: 10000
  flushIntervalSeconds: 15
  cacheLoopSeconds: 60
  histogramBucketsSeconds: [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10]
```

Use a finite reviewed list of actual SDK `service.name` values. Enabling with no names is rejected. The list allows at most 64 distinct names. Deploy-time overrides can change the pairing memory cap, pairing TTL, flush/cleanup intervals and latency buckets within schema bounds.

The connector's internal series map has no configurable hard series cap in the pinned version. The pipeline therefore rejects service names outside this explicit set, accepts only CLIENT/SERVER spans, removes other graph-branch attributes, and disables database-name inference and virtual nodes. No arbitrary dimensions, HTTP routes, database names, peer names, or payload-derived server identities can multiply graph series. With N approved services, the name-pair space is at most N² per collector; failure state doubles that grouping and histogram buckets multiply emitted time series. A 64-name list is a maximum, not a low-cost recommended initial size. Start with a small application domain and observe actual series and memory usage before expansion.

Names must be region-wide unique across namespaces/clusters if one Grafana service map is expected to distinguish all of them. Two namespaces both reporting `service.name: api` will merge at the service-name level; adding replicas does not fix that. Choose qualified SDK names such as `shop-checkout` and `finance-checkout` (and use those exact values in the allowlist), or operate separate regional/cluster-scoped query views. The graph does not silently rewrite application names because Grafana trace links need to match the original identity.

Each sampler emits cumulative metrics using its **pod UID as the metric writer**, plus region and cluster attributes. The graph's generated metrics use the configured Arrow/OTLP interconnect through the normal metrics gateway and cardinality policy. Compute rates per writer, then aggregate histogram buckets before computing quantiles. The Tempo datasource links its service map to the regional Mimir datasource.

Example verification queries in Grafana Explore:

```promql
sum by (cloud_region, k8s_cluster_name, client, server) (
  rate(traces_service_graph_request_total[5m])
)

sum by (client, server) (rate(traces_service_graph_request_failed_total[5m]))
/
clamp_min(sum by (client, server) (rate(traces_service_graph_request_total[5m])), 0.000001)

histogram_quantile(0.95,
  sum by (cloud_region, k8s_cluster_name, client, server, le) (
    rate(traces_service_graph_request_server_seconds_bucket[5m])
  )
)
```

Latency units are seconds, verified after the actual Prometheus remote-write export. Graph errors use span status codes; HTTP status alone is not promoted to an error in this branch. Configure instrumentation to set semantic error status. Pairs with a missing half or excessive inter-span arrival skew expire without producing an invented external service. Monitor collector connector dropped-span/expired-edge metrics through the existing collector scrape and compare observed versus expected call patterns. Store exhaustion intentionally loses graph pairings; it does not enlarge the cache or prove lossless tracing.

Graphs observe spans received by the collectors. SDK/head sampling, upstream throttling, privacy drops, overload and missing instrumentation can hide edges. Retries or duplicate span submissions can overcount edges; there is no global deduplication ledger. Messaging producer/consumer pairs, database virtual nodes, and cross-region trace joins are deliberately outside this bounded mode. Keep each regional trace complete locally; cross-region tracing remains a documented application/egress design choice, not implicit replication.

## Rollout, rollback and data migration

1. Add reviewed tuning and representative synthetic fixtures in a PR. Run `make privacy-graph-test collector-test red-test rules-test dashboard-test`; the general validation suite still applies. Start with a small service allowlist and off-to-redact or off-to-allowlist transitions in a preview environment.
2. Privacy changes alter collected data permanently. Review expected loss of span links/events/names and any RED impact. Rollback changes future collection only; it cannot restore redacted or dropped payloads.
3. Existing Vector disk buffers, Kafka records, Tempo blocks and ClickHouse rows are **not retroactively scrubbed**. The gateway protects newly admitted traffic, but old durable records need a separately reviewed retention/deletion/migration plan. Do not purge state automatically during an upgrade.
4. Samplers retain `OnDelete` updates and fixed trace-ID membership. Any graph/policy change in sampler configuration uses the trace-drain runbook; it is not an automatic blue-green rollout. When removing or replacing a sampler, quiesce routing and allow at least the greater of the tail decision wait and graph store TTL, plus queue export/retry allowance. If traces are still in flight, do not claim the drain completed. Scaling or DNS membership changes can split pairs and traces. Do not attach an HPA to the samplers.
5. Histogram bucket changes introduce a metric-schema transition. Drain and replace the fixed sampler set under maintenance, then allow at least the longest dashboard/rule rate window before treating quantiles as comparable. Mixed bucket layouts during a rolling transition can produce misleading aggregates.
6. Disabling graphs removes the branch from future collector configs. Existing Mimir time series remain until retention. Expanding the service list increases series cost; reducing it removes future observations only. No database schema, Kafka partition, or ClickHouse shard migration is performed.

## Deferred live AWS acceptance

These gates are intentionally queued for the user's later live account session; none require infrastructure creation during local development:

- Confirm the chosen privacy policy with owner-approved synthetic edge cases through node collection, quota gateway, Kafka, ClickHouse and Tempo. Check stored payloads and inspect the regional buffers/retention plan without sending real secrets.
- Confirm CNI/network-policy enforcement prevents clients from bypassing agents, identity/resource settings and privileged namespace boundaries. Collector allowlists do not authenticate SDK-supplied identities.
- Generate cross-node and cross-AZ client/server spans for each approved service pair; verify paired metrics and Grafana service map navigation using the same service names.
- Measure graph store occupancy/drop/expiry, collector memory/CPU, remote-write queue pressure and active series at expected peak load. Verify chosen TTL covers actual cross-node export skew.
- Drain/restart one fixed sampler and exercise the documented membership migration procedure. Record missing pairings, trace loss, recovery time and alert behavior; do not infer availability from local containers.
- Verify all object storage, telemetry and queries stay regional, and explicitly assess any cross-region application trace paths.

Local evidence: `scripts/privacy_graph_test.py` runs the pinned Vector 0.54.0 and Collector 0.148.0 binaries, then a real Arrow → trace-ID router → three samplers → Arrow → metrics gateway → Prometheus fixture. It proves sanitized safe/error cases, two-node pairing, zero tail retention with graph metrics still present, independent writers, original-trace preservation, unlisted-name rejection and exported metric units. This is not an EKS, S3, throughput or AZ-failure qualification.

Implementation references: [pinned service-graph connector](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/v0.148.0/connector/servicegraphconnector), [pinned span transform paths](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.148.0/pkg/ottl/contexts/ottlspan/README.md), [Vector remap behavior](https://vector.dev/docs/reference/configuration/transforms/remap/).
