# Network visibility and continuous CPU profiling

These are two independently selectable integrations. **Cilium Hubble** observes network traffic. **Grafana Alloy's eBPF profiler** collects CPU stack samples and writes them to a regional **Pyroscope 2.3.1** backend. Neither feature is enabled by default. Hubble is not a CPU profiler, and packet flows are not complete application dependency traces.

## Cilium ownership and EKS networking

The observability release never installs or adopts Cilium, restarts application pods, disables the AWS policy agent, or replaces a CNI. Reuse an existing, qualified Cilium installation by setting `network_policy_provider: cilium` in the platform contract and `tuning.hubble.enabled: true`. Preflight reads the Cilium DaemonSet and ConfigMap and rejects incomplete readiness, disabled policy enforcement, AWS network-policy enforcement that is active or not explicitly disabled, or missing Hubble metrics on port 9965. These checks are prerequisites, not proof that every existing pod has a Cilium endpoint or that policies work.

`integrations/cilium/Chart.lock` pins the separate reference chart at 1.20.2. `cilium-values.yaml` is a reviewed **AWS VPC CNI chaining** starting point for the cluster networking owner, not an installer input. `values.yaml` wraps those same settings for offline dependency rendering. Before any networking rollout:

1. Inventory current Helm ownership, CNI version, node OS/kernel, security groups for pods, pod network policies, API-server reachability and rollback procedure.
2. Review chaining against the pinned Cilium documentation. AWS retains ENI address management; Cilium supplies policy enforcement. Transition AWS network-policy enforcement deliberately, never by running two policy engines accidentally.
3. Stage new nodes and drain/recreate workloads gradually. Chaining does not attach to already-running pods; verify CiliumEndpoint coverage, including critical system workloads. Do not restart a quorum's members together.
4. Run Cilium connectivity and NetworkPolicy allow/deny tests, host-network tests, DNS, EKS control-plane webhooks and telemetry intake tests in each AZ. Verify surviving traffic after one AZ is unavailable.
5. Enable the bounded Hubble metrics set, relay and UI. Keep relay/UI private; expose through the environment's authenticated access path or a temporary local port forward. Review certificates and chart ownership separately.

The scrape pool discovers node InternalIPs on 9965, which requires access to that port in node security groups and any Cilium host firewall. The existing observability NetworkPolicy admits only that additional VPC port. Host-network traffic enforcement varies by CNI; a NetworkPolicy manifest alone is not proof of host isolation.

The Hubble scrape allowlist includes flow, drop, TCP and ICMP counters, with 5,000 samples per node/scrape by default (`tuning.hubble.sampleLimit`). Flow contexts use workload names; IP, DNS-query, HTTP-path and per-pod context expansion are not enabled. Exceeding Prometheus sample limits fails the entire scrape; the network dashboard shows exporter reachability. Hubble UI provides live service/network maps. AWS chaining has limitations for L7 policy; no HTTP latency or application-call graph is promised by this profile.

## Enable regional profiles

For managed AWS services, set Terraform `enable_profiling=true`, review the plan and use its updated output. This adds a protected regional S3 bucket and a dedicated `observability/pyroscope` Pod Identity association. Existing roles are reused without policy mutation; review `required_role_policies` yourself. With software-only installation, add the existing bucket under `buckets.pyroscope`, role under `role_arns.pyroscope`, association, and `profiling: {enabled: true}` to the platform contract. Preflight verifies region and association.

```yaml
profiling:
  enabled: true
# This block is inside the nonsecret platform contract:
tuning:
  profiling:
    namespaces: [applications]
    sampleRate: 19
    collectIntervalSeconds: 30
  pyroscope:
    pyroscope:
      structuredConfig:
        limits:
          retention_period: 168h
          ingestion_rate_mb: 4
          ingestion_burst_size_mb: 8
```

Select pods with **both** `observability/profile=true` and a valid `observability/service` label, in the configured namespaces. Namespace owners must create the namespaces before Helm installs the narrowly scoped discovery Roles. Node agents discover only pods on their own node. Collection is **19 CPU samples/second**, not 19% of requests. The configurable frequency range is 1–99 Hz; increasing it increases overhead. Profiles are sent every 30 seconds, with three retries. No durable profile queue is promised: node failure, memory pressure, bounded retries and backend rejection can lose samples.

Alloy 1.19.2 requires root, host PID access and eBPF/kernel access. The optional DaemonSet is privileged and mounts the host tracing filesystem read-only; it is restricted to Linux amd64 nodes. Select compatible kernel/runtime versions using the pinned upstream support requirements, qualify admission/Pod Security constraints, and measure overhead on representative applications. Profiling namespaces/workload labels are collection selection, **not a security boundary against a compromised privileged profiler**. No application source code or service-account secrets are mounted. Symbol names can reveal implementation details; review data classification and retention.

The Pyroscope datasource is provisioned into Grafana by a namespace-scoped datasource sidecar. Open Explore and select **Regional profiles** to inspect CPU flamegraphs by `service_name`, `service_namespace`, `cloud_region` and `k8s_cluster_name`. CPU profiles do not automatically acquire SDK trace IDs; automatic trace-to-profile correlation is not claimed.

## Backend scale, retention and availability

The optional chart uses v2 microservices: three distributors, query frontends/backends, segment writers and compaction workers; a three-member AZ-separated Raft metastore with retained encrypted PVCs; two copies of auxiliary services. All use the regional object store and Pod Identity. Metastore and segment-writer PVCs default to 50 GiB each. This is a substantial extra backend, not a free node-agent toggle: measure capacity and increase the underlying node pool limits when necessary.

The metastore stays at three fixed members. Scalable workers use hard AZ topology spread, allowing more than one replica per AZ. Change worker replica counts through reviewed `tuning.pyroscope.pyroscope.components.COMPONENT.replicaCount` overrides (up to 30); confirm S3 throughput, memory and downstream capacity. There is no automatic profile backlog scaler and no blue-green metastore. Quorum/retention changes require a dedicated migration and restore rehearsal; never shrink or delete the metastore as a stateless deployment.

Default ingestion is 4 MB/s per tenant with an 8 MB **per-distributor** burst. Profile size (4 MiB uncompressed), stack samples (16,000/profile), stack depth (256), sample labels (8), series labels (12) and label length (128) are bounded. They are not a v2 global series quota. The profiler does not forward pod UID, PID or arbitrary pod labels as profile dimensions. There is no per-service shared profile byte quota equivalent to the Vector log admission service; hostile or unusually busy profile producers need additional isolation.

**v2 retention uses `limits.retention_period: 168h`.** Metastore cleanup runs every minute, marks expired partitions, and compaction workers remove their data after grace periods. This is eventual deletion, not an exact seven-day deletion SLA. S3 noncurrent-version retention remains seven additional days. The v1 `compactor_blocks_retention_period` and `max_global_series_per_tenant` are deliberately absent: they would not implement the intended v2 controls.

`make profiling-test` uses the pinned Alloy validator (including rejection of unknown settings), starts Pyroscope's API target with the rendered v2 YAML and metastore flags, validates the Hubble collector config, and checks native Kubernetes schemas. It does **not** execute eBPF capture, S3/IAM writes, distributed profile ingestion, a Raft restore, or Cilium datapath enforcement. These remain live acceptance gates before production. Preserve a sample profile and query result, prove regional S3 writes, test 429 limits, disable/re-enable a selected workload, and test loss/return of each AZ.

Sources: [Cilium AWS chaining](https://docs.cilium.io/en/stable/installation/cni-chaining-aws-cni/), [Hubble metrics](https://docs.cilium.io/en/stable/observability/metrics/), [Alloy eBPF component](https://grafana.com/docs/alloy/latest/reference/components/pyroscope/pyroscope.ebpf/), [Pyroscope v2 metastore](https://grafana.com/docs/pyroscope/latest/reference-pyroscope-v2-architecture/components/metastore/), and [the pinned v2.3.1 retention implementation](https://github.com/grafana/pyroscope/blob/v2.3.1/pkg/metastore/index/cleaner/retention/retention.go). Check the pinned chart/source when current documentation differs.
