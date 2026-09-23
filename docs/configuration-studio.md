# Configuration studio and GitHub Pages

`site/` is a dependency-free, static configuration studio. It runs from a repository subpath, on GitHub Pages, or from a local HTTP server. It offers:

- A four-step deployment selector for new EKS or software-only use of an existing cluster.
- Regional/AZ identity, roles and external ownership, capacity/sampling choices, Hubble and CPU profiling options.
- A readable file preview and ZIP download containing actual Terraform JSON or an existing-platform contract, pipeline tuning, the original request and specific next steps.
- Private local catalog import/search and service registration/scaffold requests.

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

Coordinate import preserves optional cluster ARN, operator namespaces and Grafana database name/user. Capacity and visibility remain the explicit studio selections; coordinate import does not load arbitrary pipeline overrides. Review the generated files before use. Service generation also displays copyable JSON, and the offline compiler provides an export path when a browser restricts downloads.

Replica/shard/partition changes are not automatically safe against an existing deployment. Review the current Git state and migration guards; do not reduce storage shards or partitions via a downloaded replacement. Increasing log event limits leaves independent byte budgets in force. No cost estimate is fabricated; compare instance/storage sizing, profile overhead, retention and AZ traffic using real regional prices and traffic volumes.

## Publish the studio

The repository currently needs a GitHub remote before it can have a Pages URL. Once pushed to the chosen repository:

1. Enable Pages with **GitHub Actions** as its source. Protect the `github-pages` environment and restrict deployment to the reviewed default branch.
2. Run the **publish-configuration-studio** workflow manually. It tests the compiler, builds a fixed static allowlist and deploys only that artifact with a short-lived GitHub deployment identity.
3. Open the workflow's Pages URL and test both configuration modes, service generation and mobile layout. GitHub-hosted workflow execution remains a separate verification step from local tests.

The Pages builder copies only `index.html`, `style.css`, `app.mjs`, `model.mjs` and `.nojekyll`. It refuses a nonempty output directory, preventing stale private artifacts from being accidentally published. **Never change its artifact path to the repository root** or include `catalog/`, `environments/`, `build/` or Terraform outputs in the public deployment. Private imported catalog content remains in the browser session. A public configurator does not require publishing a private infrastructure repository's contents.

The artifact has no backend: repository creation intentionally runs later from an authenticated local CLI, after generated service files are reviewed and committed. An authenticated multi-user approval portal could be added separately; that would require a real identity/authorization design rather than embedding a token into a static page.

For advanced 0.7 options, use the downloaded configuration with [setup-options.md](setup-options.md): add a reviewed security profile and privacy/service-graph tuning through the installer or GitOps CLI. These choices remain outside the curated browser form and are not silently inferred from an imported coordinate file. Generated instructions now start with `--render-only`, which makes no AWS/Kubernetes calls. Keep all actual account tests in the deferred qualification batch.
