# __SERVICE_NAME__

Owner: __OWNER__. Namespace: __NAMESPACE__.

A small Go HTTP application with bounded request counters, JSON logs with trace IDs, W3C trace-context propagation, OTLP HTTP export, health endpoints and graceful shutdown. CPU profiling is opt-in. The request counter covers successful and server-error application responses; health, metrics and unmatched routes are excluded from the availability SLO denominator. Expand and test this contract when adding handlers. Trace export samples every request before the platform tail sampler; never accept this scaffold unchanged as an authorization boundary.

1. Run `go test -race ./...` and `go vet ./...`.
2. Review service.json, then copy it to the shared infrastructure repository's catalog/services/. Run `make catalog`, and open a pull request for ownership/SLO review.
3. Review and initialize this directory as a separate Git repository. Commit the files. From the infrastructure repository, run `scripts/service_catalog.py publish --directory PATH --repository OWNER/NAME --confirm-publish OWNER/NAME` with its Python environment. The URL must match service.json; publication creates a **private** repository using your authenticated GitHub CLI account. Nothing publishes automatically.
4. Build and explicitly publish the application image. Set `chart/values.yaml` image to its digest. Render the chart, review it, and add a release to your application GitOps repository.
5. The namespace owner must label the namespace `observability/collect=true`, and add it to the platform scrape/profile namespace allowlists. This chart never adopts a namespace or its existing policies. Review network access to the OTel agent, metric scrapers, and required dependencies.
6. Send a request; check the service RED dashboard, application request counter, ClickHouse log trace_id, Tempo trace and (if opted in) Pyroscope flamegraph. No telemetry is proof of health only when coverage has been independently established.

`onboarding.yaml` is a merge guide for existing deployments, not an apply-ready Kubernetes resource. It uses the same service identity as the generated application chart. Secrets stay in external secret stores, never in the catalog.
