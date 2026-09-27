# Contributing

Start with the [architecture](docs/architecture.md), [repository instructions](AGENTS.md), and [validation record](docs/validation.md). Keep changes focused on a reproducible problem, with an example of the resulting behavior. Use synthetic service identities and data in examples and fixtures.

For bugs, include the affected commit, component versions, deployment mode, minimal reproduction, and expected versus actual behavior. Share a sanitized error summary rather than raw logs or deployment exports. Report vulnerabilities through the process in [SECURITY.md](SECURITY.md).

## Set up and validate

Install Python, Node.js/npm, Go, Terraform, Helm, kubectl, kubeconform, and Docker with a running engine. The [CI workflow](.github/workflows/ci.yaml) records the versions used for validation. Keep dependency lock files and pinned image versions in your change.

```sh
make setup deps
make check
make terraform-init terraform-test
make runtime-test
```

These checks may download dependencies and container images. Terraform tests use mocked AWS providers; they do not provision infrastructure. Local runtime and integration tests use disposable containers. Do not run cloud apply/destroy commands or contact real notification receivers as part of ordinary validation.

Run the suites relevant to the change as well:

| Change | Additional checks |
| --- | --- |
| Log transforms or quota service | `make image integration-test` |
| Arrow, trace routing, metric cardinality | `make collector-test` |
| ClickHouse topology | `make sharding-test` |
| Kafka consumers or scaling | `make consumer-test` |
| Log startup gates | `make startup-gate-test` |
| Mimir ingestion startup | `make mimir-startup-test` |
| RED metrics, rules, dashboards | `make red-test rules-test dashboard-test` |
| Studio, catalog, service scaffolding | `make studio-test service-test rules-test`; `make studio-terraform-test` for generated Terraform |
| Hubble or CPU profiling | `make profiling-test` |
| Canaries, notifications, backend scrapes | `make support-test` |
| Security profiles | `make security-test` |
| Privacy or service graphs | `make privacy-graph-test collector-test` |
| Qualification planning | `make qualification-test` |

The [testing guide](docs/testing.md) explains each suite's coverage and limits. Documentation-only changes need link and example checks; report other suites as not run rather than implying they passed. A rendered manifest, local fixture, or mocked Terraform test is not evidence of a live deployment.

## Prepare the pull request

Explain the trigger, changed behavior, and operational effect. Add representative input/output and failure cases where behavior changes. Record the checks run, their results, and anything not tested. Keep generated catalog artifacts in sync with their sources.

Preserve existing resource ownership and regional data boundaries. Describe quota scope explicitly. Include migration and recovery instructions for changes to stateful resources, partitions, shards, trace-sampler membership, credentials, or immutable settings. Use the [runbooks](docs/runbooks.md); do not treat quorum stores as stateless blue-green workloads.

Before sharing, review the staged diff for credentials, Terraform state/plan files, real account or service inventories, generated private configuration, and captured telemetry. Keep these out of commits and attachments. Public Studio builds must retain their explicit static asset allowlist.

Live AWS qualification is a separate, explicitly authorized exercise with a budget and verified teardown. Keep unexecuted gates pending in the [qualification catalog](docs/live-qualification.md), and bind any evidence to the exact configuration and revision tested.
