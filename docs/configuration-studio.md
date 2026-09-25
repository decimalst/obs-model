# Configuration studio and GitHub Pages

`site/` is a dependency-free, static configuration studio. It runs from a repository subpath, on GitHub Pages, or from a local HTTP server. It offers:

- A seven-step deployment selector: Environment, AWS IAM, Capacity, Visibility, Privacy & access, Operations, and Review.
- Regional/AZ identity, roles and external ownership, capacity/sampling choices, Hubble and CPU profiling options.
- A readable file preview and ZIP download containing actual Terraform JSON or an existing-platform contract, pipeline tuning, the original request and specific next steps.
- Private local catalog import/search and service registration/scaffold requests.
- Editable security, privacy, service graph, cell planning and recovery/qualification options with inline setup guidance and conditional fields.
- Full request import/export to resume advanced selections without rebuilding them by hand.
- A role-by-role IAM review with created/reused ownership, trust, permissions, service-account bindings and separate provisioning/EKS administration choices.

No secrets, account credentials, AWS queries or GitHub tokens are needed in the browser. It does not upload form data, store it in localStorage, automatically guess account AZ availability, apply infrastructure or publish an application. Closing/reloading the page loses unsaved selections; download the request to retain them. The only downloaded dependency is the site's own static code. Catalog text uses textContent; Grafana HTML is escaped; the page has a restrictive CSP and blocks form submissions.

## Preview and export

```sh
python3 -m http.server 8765 --bind 127.0.0.1 --directory site
# Open http://127.0.0.1:8765
```

For a repeatable offline CLI export, use Node 24:

```sh
node scripts/configure.mjs deployment-request.json environments/olly-production
```

The CLI uses the same compiler as the browser and refuses to overwrite an existing destination. Extract browser ZIPs to `environments/NAME/` inside this repository: the generated Terraform module source is relative to that location. New deployments require an encrypted regional Terraform state backend and an out-of-band sensitive quota password. After an authorized Terraform apply, export its platform output, run the read-only installer preflight, then follow the installation/GitOps runbook. The configurator never deploys. CI's Terraform plan/apply tests use a mocked AWS provider and create no cloud resources.

Existing deployments enter their regional endpoints, bucket names, roles and controller ownership directly, or import a nonsecret platform contract as JSON with **all** operator install/reuse flags explicitly set. The studio uses existing Kubernetes Secrets and storage classes. The more general CLI installer still supports the other existing infrastructure/secret paths documented in `existing-clusters.md`. The studio is a curated subset, not an editor for every chart value; additional tuning is available through the validated pipeline override file.

Coordinate import preserves optional cluster ARN, operator namespaces, Grafana database name/user and Flink bucket/role coordinates. It validates the complete coordinate contract before changing the form and rejects unsupported fields, including pipeline tuning or a security profile, instead of silently dropping them. To restore advanced choices, import the studio's `deployment-request.json`. Version-1 requests created before these additions receive the explicit disabled/default policies; explicitly supplied policies are never replaced with defaults. Service generation also displays copyable JSON, and the offline compiler provides an export path when a browser restricts downloads.

## AWS IAM step

Review AWS identities immediately after selecting the environment. The step separates three decisions for a new cluster:

1. **Terraform provisioning identity:** keep your existing AWS profile/SSO/CI credentials, or provide `iam.terraformRoleArn` for the generated AWS provider to assume an already existing role. `null` uses the credential chain. This does not create the role, configure Terraform state-backend credentials, or change AWS CLI/kubeconfig identity.
2. **EKS administrator principal:** supply the existing role that should receive a cluster-scoped EKS `AmazonEKSClusterAdminPolicy` access association. The role is not created and receives no AWS infrastructure permissions through that grant. The Terraform principal and EKS administrator can be the same deliberately selected role, but these are separate privileges; the administrator ARN is not used to infer the deployment account.
3. **Workload role ownership:** create Mimir, Tempo and Flink roles by default, or provide any subset through `iam.existingWorkloadRoles`. Optional Pyroscope reuse becomes available with CPU profiling. Supplied role trust/permissions stay external; Terraform still creates the new cluster's Pod Identity associations.

The default new-region plan creates **seven IAM roles**, or **eight with profiling**, minus supplied workload roles. Expand each inventory entry to review the actual service trust, inline permission actions or named AWS managed policies, resource scope and namespace/service-account binding. Names ending in a dash are Terraform prefixes with generated final suffixes. The Flink bucket/role/association scaffold is included while its job remains disabled. Current Pod Identity trust is account-scoped, and node roles currently include the CNI policy; these limits are stated in the review rather than hidden behind an ownership selector.

In software-only mode, the studio creates **no IAM roles, policies, EKS access entries or Pod Identity associations**. Enter the externally owned Mimir/Tempo roles and optional profiling role in `existing.role_arns`. Optional Flink role and bucket coordinates must be supplied together; they preserve a future job's references without enabling the job. Cluster infrastructure identities, EKS access, capacity-controller identities and Pod Identity agent readiness remain the cluster owner's responsibility. The separate existing-EKS Terraform data-services path remains a CLI option.

Every deployment bundle includes `IAM.md` for the owner handoff and `IAM-PLAN.json` for structured review, in addition to the full request. The plan records creation/reuse counts, role permissions/trust, associations, access grants and external prerequisites. Unknown account values stay explicit placeholders until the real Terraform identity/cluster is established. A saved Terraform plan and the eventual live credential/permission checks remain authoritative. See the [complete IAM inventory and setup guide](iam.md).

Imports preserve both IAM fields, reject unsupported role keys or inactive new-cluster IAM settings in software-only mode, and check direct Pod Identity account consistency when enough coordinates are supplied. Switching to software-only keeps new-cluster IAM choices in the current browser session and shows a notice that they are excluded from the active software-only export; switch back to save the new-cluster configuration separately. Disabling profiling with a selected new-cluster Pyroscope reuse role blocks export until the choice is resolved. Existing role/association ownership changes after deployment require a separately reviewed Terraform state migration.

## Advanced selections and generated files

| Section | Editable settings | Output and setup boundary |
|---|---|---|
| AWS IAM | Existing Terraform assume-role ARN, EKS administrator ARN, per-workload create/reuse choices and software-only role references | `IAM.md` and `IAM-PLAN.json`; new-region provider/module settings or existing `platform.json`. No role is created by the browser; state credentials and external roles remain owner-managed. |
| Grafana access | Explicit profile opt-in; local port-forward, existing TLS ingress or external HTTPS proxy; URL, proxy namespace, ingress class and TLS Secret | `security-profile.json`; installer/GitOps use the existing profile compiler. Controllers and certificates stay externally owned. |
| Authentication | Local or OAuth/OIDC, existing client/server Secret names, authorization/token/userinfo URLs, scopes, group claim, Viewer/Editor/Admin groups, refresh tokens and optional CA ConfigMap | No credentials are accepted. Groups are explicit and unique; OAuth requires HTTPS and `openid`. Register the exact callback and qualify the actual provider later. |
| Mesh | Trusted cluster network or existing Istio/Linkerd with exact allowed proxy identities | Grafana-scoped owner integration only. Selecting a mesh does not install one or encrypt every pipeline hop. |
| Privacy | Logs off/redact/drop and selected email/bearer/credential patterns; traces off/allowlist, span-name handling, permitted span/resource attributes | Complete `privacy` settings in `tuning.json`; strict mode retains required regional identity, removes events and drops linked spans. Pattern matching is not comprehensive sensitive-data detection. |
| Service graph | Opt-in, allowed service names, pairing TTL/item ceiling, flush/cache intervals and histogram boundaries | Complete `serviceGraph` settings in `tuning.json`; bounded pre-tail metrics with independent writers. Names must be unique within a cell; stateful sampler drains still apply. |
| Startup dependencies | Check attempts (1–360; default 120), retry delay (1–30 seconds; default 5), request timeout (5–60 seconds; default 20) | Complete `startupGates` settings in `tuning.json`; Operations controls gate Kafka topic/ISR readiness, all-replica ClickHouse schema and active gateway readiness. Bounds apply per init-container invocation; Kubernetes can retry failed invocations. Partial imports retain omitted defaults. See [startup gates](startup-gates.md) for hook ordering and client termination grace periods. |
| Cells and budgets | Optional shared/isolated-cluster plan, cell ID, service assignments, Grafana database identity and additional reviewed local platform references | `cells.json`; additional rows reference separate deployments, not automatic provisioning. Existing isolated cells require the exact cluster ARN. New regions need actual Terraform output before planning. |
| Recovery | Native/external backup ownership, provider runbook reference, nullable reviewed RPO/RTO, separate restore account/region/cluster, explicit cross-region option and cost assessment | `qualification.json` plus `OPERATIONS.md`; no backup system, restore job, replication or cloud resources are created. Blank objectives remain pending inputs. |
| Deferred acceptance | Run ID, context/account, reviewed commit and artifact hashes, future Flink/GitOps/notification scope | The 26-gate batch stays offline and unexecuted. Environment region/cluster/install mode and Hubble/profiling/graph flags follow the actual deployment selections. Other feature flags choose future test scope only. |

Review all files in the final step before downloading. `README.md` includes the selected policy summary and an exact installer command with the selected security file. Existing-mode `platform.json` also persists that profile and tuning. The request round trip preserves advanced lists, optional fields, peer-cell references and recovery targets. The static page never reads the referenced private platform files, queries the IdP, inspects cloud resources or records passing acceptance receipts.

Source/account IDs and artifact hashes are nonsecret references, but the bundle can reveal internal topology. Store it in the private infrastructure repository or approved configuration system. Zero commit/artifact hashes are explicitly unfinished draft values, not an accepted deployment. Existing EKS in this studio means **software-only** ownership in qualification; the separate Terraform existing-cluster path remains available through the CLI.

Replica/shard/partition changes are not automatically safe against an existing deployment. Review the current Git state and migration guards; do not reduce storage shards or partitions via a downloaded replacement. Increasing log event limits leaves independent byte budgets in force. No cost estimate is fabricated; compare instance/storage sizing, profile overhead, retention and AZ traffic using real regional prices and traffic volumes.

## Publish the studio

The repository currently needs a GitHub remote before it can have a Pages URL. Once pushed to the chosen repository:

1. Enable Pages with **GitHub Actions** as its source. Protect the `github-pages` environment and restrict deployment to the reviewed default branch.
2. Run the **publish-configuration-studio** workflow manually. It tests the compiler, builds a fixed static allowlist and deploys only that artifact with a short-lived GitHub deployment identity.
3. Open the workflow's Pages URL and test both configuration modes, service generation and mobile layout. GitHub-hosted workflow execution remains a separate verification step from local tests.

The Pages builder copies only `index.html`, `style.css`, `app.mjs`, `model.mjs`, `iam.mjs`, `policies.mjs`, `operations.mjs` and `.nojekyll`. It refuses a nonempty output directory, preventing stale private artifacts from being accidentally published. Tests verify the complete module graph exists inside that allowlist. **Never change its artifact path to the repository root** or include `catalog/`, `environments/`, `build/`, downloaded IAM plans or Terraform outputs in the public deployment. Private imported catalog content remains in the browser session. A public configurator does not require publishing a private infrastructure repository's contents.

The artifact has no backend: repository creation intentionally runs later from an authenticated local CLI, after generated service files are reviewed and committed. An authenticated multi-user approval portal could be added separately; that would require a real identity/authorization design rather than embedding a token into a static page.

The 0.9 studio adds explicit IAM setup and review to the security/privacy/graph/operations contracts. It remains a curated configurator, not an editor for every Helm value or an authenticated operations portal. Support receiver/canary settings, arbitrary collector transforms and additional tuning still use their validated repository files and [setup options](setup-options.md). Generated instructions start with `--render-only`, which makes no AWS/Kubernetes calls. Keep all actual account tests in the deferred qualification batch.
