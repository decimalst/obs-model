# Service catalog and creation

The catalog is a Git-owned set of versioned service definitions, a generated Grafana service directory, an importable browser catalog, and an offline service scaffolder. It deliberately has no always-on portal database, GitHub token store or public write API.

## Register an existing service

Use the studio's **Create a service → Register existing** form, or copy `examples/service.json`. Set a stable namespace/service identity, owner team, description, repository, runbook, lifecycle, declared dependencies, profile selection and optional availability SLO. The schema rejects unknown fields, executable URL schemes, invalid identities, self-dependencies and invalid SLO settings. Filenames are administrative; namespace/name is the unique identity.

1. Add the definition to `catalog/services/NAME.json` through a pull request. Use repository branch protection and ownership review to authorize changes; the schema cannot prove that a contributor owns a service.
2. Run `make catalog`. Commit the definition and generated `catalog/index.json`, Grafana dashboard and Mimir rule file together. CI fails when generated files are stale. Regional Flux releases reconcile these same files on Git revisions.
3. Merge the SDK identity/labels into the application. `examples/sdk-identity.yaml` and the scaffolder's `onboarding.yaml` describe identity. Registration alone does not instrument or redeploy an existing application, modify a namespace or grant a quota exception.
4. The namespace owner labels the application namespace `observability/collect=true`. Add its name to `tuning.collectors.scrapeNamespaces` (retaining `observability`), and, if applicable, `tuning.profiling.namespaces`. Select pod profiling explicitly. Keep the SDK service name, pod `observability/service` label and catalog name identical.
5. Generate real traffic and verify a trace, a correlated log, request counters, RED metrics and optional CPU profile. Sampling and delivery losses must be considered before relying on span-derived SLOs.

Grafana's **Service catalog** links repositories, runbooks and the shared service RED dashboard. Use the RED filters for the intended region/cluster. Declared dependencies are metadata, not automatically inferred traffic. The public studio can import `catalog/index.json` locally and filter by service, namespace or owner. No private catalog content is included in its Pages artifact.

## Create a new application

The studio exports the same definition. Run:

```sh
.venv/bin/python scripts/service_catalog.py scaffold \
  --service checkout.json --output build/checkout
```

The output includes a Go HTTP service, pinned OpenTelemetry SDK dependencies and lock file, W3C propagation, OTLP HTTP traces, JSON logs carrying trace IDs, bounded direct request counters, health endpoints, graceful shutdown, a non-root image, unit tests, CI and a three-AZ application Helm chart. The application image must be built and pinned by digest before the chart renders. The chart uses rolling upgrades with zero unavailable pods and a PDB; application-specific blue-green behavior remains an application decision.

The starter's availability counter includes successful requests and server errors. It excludes health/metrics endpoints and unmatched routes. Test and extend that denominator when adding handlers; do not claim it is every request automatically. The template exports all server spans, while the platform applies its bounded downstream tail sampler. No raw URLs or request headers become dimensions.

Review and test the files, initialize a **separate** Git repository and commit them. Publication is an explicit second operation:

```sh
.venv/bin/python scripts/service_catalog.py publish \
  --directory build/checkout --repository YOUR-ORG/checkout \
  --confirm-publish YOUR-ORG/checkout
```

The service repository URL must match, the directory must be a clean standalone Git repository, and it must have no remotes. The command uses your authenticated `gh` account to create a **private** repository and push the reviewed commit. It never stages or commits unknown files automatically. The browser never receives a GitHub token and cannot publish by itself. If repository creation succeeds but a later push fails, inspect the new remote and resume with ordinary Git; don't rerun creation blindly.

## Availability SLOs and alert ownership

SLOs are off until a service owner opts in. The supported sources are the scaffold's `service_requests_total` counter contract or observed server spans (`platform_red_calls_total` before tail sampling). The latter is coverage-dependent; SDK sampling, retries, missing instrumentation and collector drops can bias it. Existing applications must emit the documented direct-counter name/labels or adapt their metrics with separately reviewed rules.

Generated rules calculate 5m, 30m, 1h and 6h error ratios by **rate per writer, then aggregate**. They preserve regional/cluster scope, service namespace/name and owner. Alerts use both windows: 14.4× budget burn over 1h and 5m, or 6× over 6h and 30m, each held for two minutes, calibrated to a 30-day objective. Zero traffic yields an absent ratio rather than fabricated success. These rules do not calculate a durable 30-day compliance report, latency objective or missing-telemetry alarm.

Owner/severity labels and runbook links are included. You still need an authenticated alert receiver, routing configuration and a tested notification path; no paging recipient or secret is invented. Use `docs/alerts.yaml` as the starting point and test an intentional fixture alert. Git approval is the current self-service policy boundary; a richer authorization portal and runtime quota/debug-grant approval are future work.

`make catalog studio-test service-test rules-test dashboard-test` checks definitions, generated content, safe local generation/publication preconditions, scaffold compilation/race tests, executable burn-alert cases and Grafana provisioning. A schema-valid catalog entry is not proof that the application is deployed or its owner can receive pages.
