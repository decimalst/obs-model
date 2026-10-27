// Read-only projection of the current request. No configuration, network or storage writes.
import { defaults, operators, validateRequest } from "./model.mjs";

const at = (value, path) => path.split(".").reduce((v, k) => v?.[k], value);
const canonical = (value) =>
  JSON.stringify(value, (_, v) =>
    v && typeof v === "object" && !Array.isArray(v)
      ? Object.fromEntries(
          Object.keys(v)
            .sort()
            .map((k) => [k, v[k]]),
        )
      : v,
  );
const number = (value) =>
  Number.isFinite(value) ? value.toLocaleString("en-US") : "Not set";

export function architectureModel(r, step = 0) {
  const changed = (...keys) =>
    keys.some((key) => canonical(at(r, key)) !== canonical(at(defaults, key)));
  const node = (id, title, lines, steps = [], keys = [], options = {}) => ({
    id,
    title,
    lines,
    steps,
    changed: changed(...keys),
    current: steps.includes(step),
    optional: false,
    enabled: true,
    external: false,
    ...options,
  });
  const existing = r.mode === "existing";
  const gate = `${number(r.startupGates.attempts)} attempts · ${number(r.startupGates.delaySeconds)}s delay · ${number(r.startupGates.timeoutSeconds)}s timeout`;
  const privacy = `Logs: ${r.privacy.logs.mode} · traces: ${r.privacy.traces.mode}`;
  const p = r.securityProfile;
  let validation =
    "Configuration valid for export; deployment and live qualification remain separate.";
  try {
    validateRequest(r);
  } catch (error) {
    validation = `Draft — ${error.message}`;
  }
  const lanes = [
    {
      id: "logs",
      title: "Logs",
      nodes: [
        node(
          "vector",
          "Vector agents",
          [
            "Node log files → local throttle",
            `Privacy: ${r.privacy.logs.mode} · debug expiry ≤24h`,
          ],
          [4],
          ["privacy.logs"],
        ),
        node(
          "quota",
          "Shared log admission",
          [
            `${number(r.logEventsPerSecond)} events/s · burst ${number(r.logEventsPerSecond * 2)}`,
            "Regional, per namespace/service · independent 1 MiB/s budget",
            "Atomic Valkey tokens → Vector gateway",
          ],
          [2, 5],
          ["logEventsPerSecond", "startupGates"],
        ),
        node(
          "kafka",
          "Kafka → Vector writers",
          [
            `${number(r.logPartitions)} log partitions · 3 replicas`,
            `Writer ceiling ${number(Math.min(r.writerMax, r.logPartitions))} · requested ${number(r.writerMax)}`,
            "Lag-based scaling; acknowledge after storage",
          ],
          [2, 5],
          ["logPartitions", "writerMax", "startupGates"],
        ),
        node(
          "clickhouse",
          "ClickHouse + Keeper",
          [
            `${number(r.clickhouseShards)} shards × 3 replicas = ${number(r.clickhouseShards * 3)} data replicas`,
            "Keeper ×3 · write quorum 2",
            "Schema hook + derived views; shard changes need migration review",
          ],
          [2],
          ["clickhouseShards"],
        ),
      ],
    },
    {
      id: "metrics",
      title: "Metrics",
      nodes: [
        node(
          "scrape",
          "OTel agents + scrape",
          [
            "OTLP / kubelet + annotated targets",
            "Scrape replicas ×3 · Target Allocator ×2",
            `Hubble: ${r.hubble ? "enabled from existing Cilium" : "off"}`,
          ],
          [3],
          ["hubble", "networkProvider"],
        ),
        node("metrics", "Metrics gateway", [
          "Arrow → Prometheus remote write",
          "SERVER-span RED branches before tail sampling",
          "Regional tenant limits; not a per-service metrics quota",
        ]),
        node("mimir", "Mimir ingestion + S3", [
          "Distributors → shared Kafka → ingesters",
          "Separate mimir-ingest topic: 1 partition",
          "Compactor · store gateways · regional buckets",
        ]),
        node("mimir-query", "Mimir query + rules", [
          "Querier · frontend · scheduler",
          "Ruler · Alertmanager · overrides exporter",
          "Dedicated rollout operator",
        ]),
      ],
    },
    {
      id: "traces",
      title: "Traces",
      nodes: [
        node(
          "otel",
          "Same OTel agents",
          [
            "Original OTLP trace branch",
            `Attribute policy: ${r.privacy.traces.mode}`,
          ],
          [4],
          ["privacy.traces"],
        ),
        node(
          "router",
          "Trace-ID router",
          [
            "Consistent hash → stable sampler names",
            "Trace affinity retained; stateless rollout",
            `Attribute policy: ${r.privacy.traces.mode}`,
          ],
          [4],
          ["privacy.traces"],
        ),
        node(
          "samplers",
          "Tail samplers ×3",
          [
            `${number(r.traceBaselinePercent)}% baseline candidates + errors / slow traces`,
            "30s in-memory state · no HPA",
            `Service graph: ${r.serviceGraph.enabled ? "on, separate pre-tail branch → metrics" : "off"}`,
          ],
          [2, 4],
          ["traceBaselinePercent", "serviceGraph", "privacy.traces"],
        ),
        node("tempo", "Tempo + S3", [
          "Distributor · AZ-separated ingesters",
          "Querier · query frontend · compactor",
          "Memcached · regional trace bucket",
        ]),
      ],
    },
    {
      id: "profiles",
      title: "CPU profiles · optional",
      nodes: [
        node(
          "alloy",
          "Alloy eBPF profiler",
          r.profiling
            ? [
                `${number(r.sampleRate)} Hz · privileged DaemonSet`,
                `Namespaces: ${r.profileNamespaces.join(", ") || "not selected"}`,
                "Explicit workload / node selection",
              ]
            : ["Off — no profiler deployed"],
          [3],
          [
            "profiling",
            ...(r.profiling ? ["sampleRate", "profileNamespaces"] : []),
          ],
          { optional: true, enabled: r.profiling },
        ),
        node(
          "pyroscope",
          "Pyroscope v2 + S3",
          r.profiling
            ? [
                "Distributor · segment writer · metastore",
                "Query frontend / backend · compaction",
                "Tenant settings · ad-hoc profiles · admin",
              ]
            : ["Off — no profile backend deployed"],
          [3],
          ["profiling"],
          { optional: true, enabled: r.profiling },
        ),
      ],
    },
  ];
  const foundation = [
    node(
      "grafana",
      "Grafana · all enabled signals",
      [
        "Queries ClickHouse / Mimir / Tempo" +
          (r.profiling ? " / Pyroscope" : ""),
        "Two replicas · external PostgreSQL",
        `${p ? p.access.mode : "port-forward"} · ${p ? p.authentication.mode : "local"} authentication`,
        "Grafana login does not authenticate ingestion",
      ],
      [3, 4],
      ["profiling", "securityProfile"],
    ),
    node(
      "controllers",
      "Controllers",
      operators.map(
        (name) =>
          `${name}: ${existing && !r.existing?.operators?.[name] ? "reuse" : "install"}`,
      ),
      [0],
      ["mode", "existing.operators"],
    ),
    node(
      "plumbing",
      "Kubernetes support objects",
      [
        "Services · ServiceAccounts / RBAC · ConfigMaps",
        "NetworkPolicies · PDBs · HPA / KEDA",
        `${existing ? "Reuse " + (r.existing?.storage_class || "selected StorageClass") : "Create encrypted gp3 StorageClass"} · PVC templates`,
        "Schema / test hooks · support diagnostics",
        "Stored-signal canary and notifications are separate opt-ins",
      ],
      [0],
      ["mode", "existing.storage_class"],
    ),
    node(
      "privacy",
      "Privacy + service graph",
      [
        privacy,
        ...(r.serviceGraph.enabled
          ? [
              `Graph allowlist: ${r.serviceGraph.services.join(", ") || "not selected"}`,
              `State: ${number(r.serviceGraph.storeMaxItems)} items / ${number(r.serviceGraph.storeTTLSeconds)}s TTL`,
            ]
          : ["Service graph off"]),
        "Lossy privacy controls; not guaranteed sensitive-data detection",
      ],
      [4],
      ["privacy", "serviceGraph"],
    ),
    node(
      "startup",
      "Startup checks",
      [
        gate,
        "Admission → gateway; gateway → Kafka; writer → schema",
        "Startup readiness is not continuous health or guaranteed delivery",
      ],
      [5],
      ["startupGates"],
    ),
    node(
      "aws",
      existing
        ? "Existing regional infrastructure"
        : "Terraform regional foundation",
      [
        r.region || "Region not selected",
        r.zones.length
          ? r.zones.join(" · ")
          : "Select three availability zones",
        `VPC: ${r.vpcCIDR || "not set"}`,
        "EKS / nodes · Valkey · PostgreSQL · regional S3",
        "Outside Helm ownership",
      ],
      [0],
      ["mode", "name", "region", "zones", "vpcCIDR", "existing"],
      { external: true },
    ),
    node(
      "identity",
      "IAM + external integrations",
      [
        existing
          ? "Reuse supplied workload roles and Pod Identity"
          : `${Object.keys(r.iam.existingWorkloadRoles).length} workload roles selected for reuse; remaining required roles provisioned by Terraform`,
        "IAM / Pod Identity associations are outside Helm",
        `Networking: ${r.networkProvider} · Cilium remains externally owned`,
        p?.internalTransport.mode === "existing-mesh"
          ? "Existing mesh: Grafana integration only"
          : "Cluster-network transport; no pipeline-wide mTLS implied",
        "Ingress controller / IdP / Flux remain external",
      ],
      [0, 1, 4],
      [
        "mode",
        "iam",
        "existing.role_arns",
        "networkProvider",
        "securityProfile",
      ],
      { external: true },
    ),
    node(
      "extensions",
      "Separate optional releases",
      [
        "Flink operator + application job: separately configured",
        "Debug-grant cleanup: per application namespace",
        "Runtime expiry is independent of cleanup",
      ],
      [],
      [],
      { optional: true, enabled: false },
    ),
    node(
      "plans",
      "Plans + review",
      [
        r.isolation.enabled
          ? `Isolation plan: ${r.isolation.mode} · ${number(r.isolation.additionalCells.length)} additional references`
          : "Isolation plan off",
        r.qualification
          ? "Qualification draft selected — pending, not executed"
          : "Qualification draft off",
        "Exports files only; no clusters, backups or jobs created",
      ],
      [5, 6],
      ["isolation", "qualification"],
    ),
  ];
  const nodes = [...lanes.flatMap((l) => l.nodes), ...foundation];
  return {
    name: r.name || "Unnamed deployment",
    region: r.region || "Region not selected",
    validation,
    lanes,
    foundation,
    changedCount: nodes.filter((n) => n.changed).length,
    summary: [
      lanes[0].nodes[1],
      lanes[0].nodes[3],
      lanes[2].nodes[2],
      lanes[3].nodes[0],
    ],
  };
}

const element = (tag, text, className) => {
  const e = document.createElement(tag);
  if (text !== undefined) e.textContent = text;
  if (className) e.className = className;
  return e;
};

export function renderArchitecture(root, summary, model, onStep) {
  function card(node) {
    const card = element("li", undefined, "architecture-node");
    card.dataset.node = node.id;
    for (const key of ["changed", "current", "optional", "enabled", "external"])
      card.dataset[key] = String(node[key]);
    card.append(element("h4", node.title));
    const badges = [
      node.changed && "Changed",
      node.current && "Current step",
      node.optional && (node.enabled ? "Optional · on" : "Optional · off"),
      node.external && "External",
    ].filter(Boolean);
    if (badges.length)
      card.append(element("p", badges.join(" · "), "architecture-badges"));
    for (const line of node.lines) card.append(element("p", line));
    if (node.steps.length) {
      const button = element(
        "button",
        `Edit in step ${node.steps[0] + 1} →`,
        "architecture-edit",
      );
      button.type = "button";
      button.addEventListener("click", () => onStep(node.steps[0]));
      card.append(button);
    }
    return card;
  }
  root.replaceChildren();
  for (const lane of model.lanes) {
    const section = element("section");
    section.append(element("h3", lane.title));
    const list = element("ol", undefined, "architecture-flow");
    list.setAttribute("aria-label", `${lane.title} data flow`);
    list.append(...lane.nodes.map(card));
    section.append(list);
    root.append(section);
  }
  root.append(element("h3", "Query, control plane and ownership"));
  const support = element("ul", undefined, "architecture-foundation");
  support.append(...model.foundation.map(card));
  root.append(support);
  summary.replaceChildren();
  for (const node of model.summary) {
    const row = element("div", undefined, "architecture-summary-row");
    row.dataset.changed = String(node.changed);
    row.append(element("strong", node.title), element("span", node.lines[0]));
    if (node.changed) row.append(element("small", "Changed from defaults"));
    summary.append(row);
  }
}
