# Configuration studio and GitHub Pages

`site/` is a dependency-free, static configuration studio. It runs from a repository subpath, on GitHub Pages, or from a local HTTP server. It offers:

- A six-step deployment selector for new EKS or software-only use of an existing cluster.
- Regional/AZ identity, roles and external ownership, capacity/sampling choices, Hubble and CPU profiling options.
- A readable file preview and ZIP download containing actual Terraform JSON or an existing-platform contract, pipeline tuning, the original request and specific next steps.
- Private local catalog import/search and service registration/scaffold requests.
- Editable security, privacy, service graph, cell planning and recovery/qualification options with inline setup guidance and conditional fields.
- Full request import/export to resume advanced selections without rebuilding them by hand.

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

The CLI uses the same compiler as the browser and refuses to overwrite an existing destination. Extract browser ZIPs to `environments/NAME/` inside this repository: the generated Terraform module source is relative to that location. New deployments require an encrypted regional Terraform state backend and an out-of-band sensitive quota password. After an authorized Terraform apply, export its platform output, run the read-only installer preflight, then follow the installation/GitOps runbook. Neither the configurator nor CI performs an apply.

Existing deployments enter their regional endpoints, bucket names, roles and controller ownership directly, or import a nonsecret platform contract as JSON with **all** operator install/reuse flags explicitly set. The studio uses existing Kubernetes Secrets and storage classes. The more general CLI installer still supports the other existing infrastructure/secret paths documented in `existing-clusters.md`. The studio is a curated subset, not an editor for every chart value; additional tuning is available through the validated pipeline override file.

Coordinate import preserves optional cluster ARN, operator namespaces and Grafana database name/user. It validates the complete coordinate contract before changing the form and rejects unsupported fields, including pipeline tuning or a security profile, instead of silently dropping them. To restore advanced choices, import the studio's `deployment-request.json`. Version-1 requests created before these additions receive the explicit disabled/default policies; explicitly supplied policies are never replaced with defaults. Service generation also displays copyable JSON, and the offline compiler provides an export path when a browser restricts downloads.

## Advanced selections and generated files

| Section | Editable settings | Output and setup boundary |
|---|---|---|
| Grafana access | Explicit profile opt-in; local port-forward, existing TLS ingress or external HTTPS proxy; URL, proxy namespace, ingress class and TLS Secret | `security-profile.json`; installer/GitOps use the existing profile compiler. Controllers and certificates stay externally owned. |
| Authentication | Local or OAuth/OIDC, existing client/server Secret names, authorization/token/userinfo URLs, scopes, group claim, Viewer/Editor/Admin groups, refresh tokens and optional CA ConfigMap | No credentials are accepted. Groups are explicit and unique; OAuth requires HTTPS and `openid`. Register the exact callback and qualify the actual provider later. |
| Mesh | Trusted cluster network or existing Istio/Linkerd with exact allowed proxy identities | Grafana-scoped owner integration only. Selecting a mesh does not install one or encrypt every pipeline hop. |
| Privacy | Logs off/redact/drop and selected email/bearer/credential patterns; traces off/allowlist, span-name handling, permitted span/resource attributes | Complete `privacy` settings in `tuning.json`; strict mode retains required regional identity, removes events and drops linked spans. Pattern matching is not comprehensive sensitive-data detection. |
| Service graph | Opt-in, allowed service names, pairing TTL/item ceiling, flush/cache intervals and histogram boundaries | Complete `serviceGraph` settings in `tuning.json`; bounded pre-tail metrics with independent writers. Names must be unique within a cell; stateful sampler drains still apply. |
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

The Pages builder copies only `index.html`, `style.css`, `app.mjs`, `model.mjs`, `policies.mjs`, `operations.mjs` and `.nojekyll`. It refuses a nonempty output directory, preventing stale private artifacts from being accidentally published. Tests verify the complete module graph exists inside that allowlist. **Never change its artifact path to the repository root** or include `catalog/`, `environments/`, `build/` or Terraform outputs in the public deployment. Private imported catalog content remains in the browser session. A public configurator does not require publishing a private infrastructure repository's contents.

The artifact has no backend: repository creation intentionally runs later from an authenticated local CLI, after generated service files are reviewed and committed. An authenticated multi-user approval portal could be added separately; that would require a real identity/authorization design rather than embedding a token into a static page.

The 0.8 studio exposes the 0.7 security/privacy/graph/operations contracts above. It remains a curated configurator, not an editor for every Helm value or an authenticated operations portal. Support receiver/canary settings, arbitrary collector transforms and additional tuning still use their validated repository files and [setup options](setup-options.md). Generated instructions start with `--render-only`, which makes no AWS/Kubernetes calls. Keep all actual account tests in the deferred qualification batch.
