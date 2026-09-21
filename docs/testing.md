# Test strategy and production acceptance

## Reproducible local checks

`make check` runs Terraform formatting, Helm lint, Python lint/format, Go vet/race tests, debug-grant boundary/ownership tests, Helm safety invariants, strict Kubernetes native schemas, and custom-resource validation against the exact pinned CRDs. Kubernetes schema checks intentionally skip CRs; the separate Python validator must cover every application CR kind. Upstream YAML merge expansion is explicit in `bootstrap_crds.py`.

`make terraform-init terraform-test` uses mocked AWS provider data and plan-only Terraform tests. The tests verify three AZs/NAT/node groups, private EKS, protected encrypted data, S3 gateway routing, and reject two-AZ, mixed-region, missing-tag, and undersized-node inputs. They do not validate account quotas, AMI availability, real IAM evaluation, or actual resource creation.

The same target checks the existing-cluster root and shared data-services module: role reuse must create no IAM roles/policies, externally owned associations must remain unmanaged, and existing subnet/VPC/AZ coordinates must be preserved. Installer tests feed synthetic inventories into read-only preflight and use real Helm rendering to verify controller exclusion, existing storage propagation, ownership checks, context identity, secret preservation and stale-overlay rejection. The deployment command must never run after failed preflight or with `--check-only`.

`make runtime-test` executes all three Vector and four OTel collector configurations with the real pinned images, verifies Tempo configuration, and validates Mimir configuration for its nine distributed targets. Vector additionally executes actual transforms for ordinary errors, default-denied debug/trace, valid grants, expiry, malformed/overlong/future grants, identity spoofing, and removal of grant fields. Go tests use a real temporary Valkey instance to verify concurrent independent admission instances share one error budget, other services remain independent, and debug/byte sub-budgets work. HTTP tests cover unavailable quota state, malformed identity/JSON, oversized batches, concurrency saturation, and completion of an in-flight request during shutdown.

`make image integration-test` builds the quota image and runs a complete isolated log path: a substituted local Vector HTTP source feeds the production identity/normalization/grant/throttle transforms, the quota service, a Vector gateway, Kafka, a Vector consumer, and ClickHouse/Keeper. It asserts stored flood/error counts, an unaffected quiet service, one permitted debug event, no expired debug event, and the derived aggregate count. This uses one broker/database/Keeper and does not model AZ failure. Test resources are removed in a `finally` block.

The optional Flink chart/CR can be rendered and schema-validated using a dummy job image for validation. A real job must supply separate job/connector tests. Readiness and schema checks are not substitutes for a live traffic test.

## Required acceptance in a disposable AWS environment

Do not label the platform production-validated until these tests run on the intended versions and topology, with evidence retained:

1. Provision each region from its own state and generated coordinates. Validate EKS access, Pod Identity credentials, encrypted EBS, S3 policies, RDS/Valkey authentication, autoscaler scale-up, pod placement across physical AZs, and network policy enforcement.
2. Deploy a JSON log canary with the platform service label. Send a sustained noisy service next to a quiet service. Measure admitted/dropped events and bytes against `burst + rate × time`, before/during/after HPA and blue-green. Confirm no severity bypass, no pod-label identity forgery, and no direct application access to Kafka/ClickHouse/quota backends.
3. Grant debug for a short test duration. Verify logs arrive while valid, stop at expiry with the controller disabled, and resume only after a new explicit grant. Test new pods during a grant, revoked grants, unauthorized annotation edits, and clock skew.
4. Emit a known OTLP counter and trace from a canary, query Mimir/Tempo and Grafana, and verify cross-signal `service.name`/`trace_id` correlation. Verify a single assigned scrape per target, bounded series/sample limits, and late-span/sampling behavior.
5. Stop one Kafka broker/Keeper/ClickHouse replica at a time and verify quorum, durability, readable data, and recovery. Then exercise an AZ outage in an isolated test environment. PDBs govern voluntary evictions; they do not guarantee involuntary failures are harmless. Test EBS attachment constraints after AZ loss.
6. Promote each stateless tier during traffic; prove old/new connections drain, Kafka consumer offsets commit, error/drop counters stay within expectations, and rollback restores the old image. Tail-sampler tests must quantify incomplete/lost traces during replacement rather than assume zero loss.
7. Exercise a controlled Valkey failover and network timeout. Confirm 503/backpressure, bounded burst overshoot from stale replicated state, queue recovery, and admitted duplicate/loss semantics. Fill a bounded buffer in a disposable node to measure rotation loss.
8. Restore snapshots/PVCs/object data and restore a region. State the measured RPO/RTO and costs. Cross-region raw replication is absent, so a surviving region cannot magically reconstruct the failed region's history.

Capture manifests, immutable image IDs, timings, query results, queue/drop metrics, and cost/throughput measurements. Record failures as failures. Do not weaken quotas or skip unavailable replicas just to make an acceptance run pass.
