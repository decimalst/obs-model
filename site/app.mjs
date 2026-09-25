import {
  defaults,
  operators,
  compileRequest,
  validateService,
  zipFiles,
  existingOptions,
  validateRequest,
  validateExistingCoordinates,
} from "./model.mjs";
import { createQualification } from "./operations.mjs";
import { iamPlan } from "./iam.mjs";
const $ = (selector) => document.querySelector(selector),
  $$ = (selector) => [...document.querySelectorAll(selector)],
  form = $("#deployment-form");
let step = 0,
  files = {},
  existingExtras = {},
  catalog = [];
const list = (value) =>
  value
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
function save(name, content, type) {
  const url = URL.createObjectURL(new Blob([content], { type })),
    a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.hidden = true;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function view(name) {
  $$(".view").forEach((e) => (e.hidden = e.id !== name));
  $$(".nav").forEach((e) =>
    e.classList.toggle("active", e.dataset.view === name),
  );
  $("#breadcrumb").textContent = {
    configure: "DEPLOYMENT STUDIO",
    catalog: "SERVICE CATALOG",
    service: "CREATE A SERVICE",
  }[name];
}
$$("[data-view]").forEach((e) =>
  e.addEventListener("click", () => view(e.dataset.view)),
);
$$("[data-open-service]").forEach((e) =>
  e.addEventListener("click", () => view("service")),
);
// All advanced controls edit structured state; imports keep optional fields intact.
const advancedKeys = [
  "startupGates",
  "securityProfile",
  "privacy",
  "serviceGraph",
  "isolation",
  "qualification",
  "iam",
];
let advanced = Object.fromEntries(
  advancedKeys.map((key) => [key, structuredClone(defaults[key])]),
);
const at = (path) =>
  path.split(".").reduce((value, key) => value?.[key], advanced);
function put(path, value) {
  const keys = path.split("."),
    leaf = keys.pop();
  let target = advanced;
  for (const key of keys) target = target[key] ??= {};
  if (value === undefined) delete target[leaf];
  else target[leaf] = value;
}
function textElement(tag, text, className) {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}
function group(parent, title, help, { details = false, when } = {}) {
  const node = document.createElement(details ? "details" : "section");
  node.className = "option-group";
  node.append(textElement(details ? "summary" : "h2", title));
  if (help) node.append(textElement("p", help, "hint"));
  if (when) node.dataset.when = when;
  parent.append(node);
  return node;
}
function grid(parent) {
  const node = document.createElement("div");
  node.className = "field-grid";
  parent.append(node);
  return node;
}
function field(parent, path, title, options = {}) {
  const {
    type = "text",
    help,
    choices,
    optional = false,
    nullable = false,
    min,
    max,
    step: increment,
    placeholder,
    wide = false,
    onChange,
    readOnly = false,
  } = options;
  const label = document.createElement("label");
  if (wide) label.className = "wide";
  const input = document.createElement(
    choices
      ? "select"
      : type === "list" || type === "numbers"
        ? "textarea"
        : "input",
  );
  input.name = path;
  input.dataset.advanced = path;
  input.dataset.kind = type;
  if (choices)
    for (const [value, name] of choices) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = name;
      input.append(option);
    }
  else if (["list", "numbers"].includes(type)) input.rows = 2;
  else input.type = type;
  if (min !== undefined) input.min = min;
  if (max !== undefined) input.max = max;
  if (increment !== undefined) input.step = increment;
  if (placeholder) input.placeholder = placeholder;
  if (readOnly) input.readOnly = true;
  if (type === "checkbox") {
    label.className = "toggle";
    const span = textElement("span", title);
    if (help) span.append(textElement("small", help));
    label.append(input, span);
  } else {
    label.append(document.createTextNode(title), input);
    if (help) label.append(textElement("small", help));
  }
  input.addEventListener(
    type === "checkbox" || choices ? "change" : "input",
    () => {
      let value;
      if (type === "checkbox") value = input.checked;
      else if (["list", "numbers"].includes(type)) {
        value = input.value
          .split(/[,\n]/)
          .map((item) => item.trim())
          .filter(Boolean);
        if (type === "numbers") value = value.map(Number);
        if (optional && !value.length) value = undefined;
      } else if (type === "number")
        value = input.value === "" && nullable ? null : Number(input.value);
      else
        value =
          input.value.trim() || (nullable ? null : optional ? undefined : "");
      if (onChange) onChange(value);
      else put(path, value);
      update();
    },
  );
  parent.append(label);
  return input;
}
function advancedValues() {
  for (const input of $$("[data-advanced]")) {
    const value = at(input.dataset.advanced);
    if (input.type === "checkbox") input.checked = value ?? false;
    else input.value = Array.isArray(value) ? value.join(", ") : (value ?? "");
  }
  $("#security-enabled").checked = advanced.securityProfile !== null;
  $("#qualification-enabled").checked = advanced.qualification !== null;
  syncIAMChoices();
}
function enable(parent, id, title, help, callback) {
  const label = document.createElement("label");
  label.className = "toggle";
  const input = document.createElement("input");
  input.type = "checkbox";
  input.id = id;
  input.name = id;
  const span = textElement("span", title);
  span.append(textElement("small", help));
  label.append(input, span);
  parent.append(label);
  input.addEventListener("change", () => {
    try {
      callback(input.checked);
      advancedValues();
      update();
    } catch (error) {
      advancedValues();
      $("#deployment-error").textContent = error.message;
    }
  });
}
function securityDefaults() {
  return {
    version: 1,
    serverSecretName: "grafana-server",
    access: { mode: "port-forward", url: "http://localhost:3000" },
    authentication: { mode: "local" },
    internalTransport: { mode: "cluster-network" },
  };
}
function oauthDefaults() {
  return {
    mode: "oauth",
    secretName: "grafana-oauth",
    authURL: "",
    tokenURL: "",
    userInfoURL: "",
    scopes: ["openid", "profile", "email"],
    groupsClaim: "groups",
    roles: { Viewer: ["observability-viewers"] },
  };
}
function buildIAM() {
  $("#iam-admin-fields").append($("#admin-field"));
  for (const name of ["mimirRole", "tempoRole", "pyroscopeRole"])
    $("#iam-existing-fields").append(form.elements[name].closest("label"));
  for (const [name, title, hint] of [
    [
      "flinkRole",
      "Existing Flink Pod Identity role ARN (optional)",
      "Supply together with its regional bucket only when preparing the separately deployed Flink job.",
    ],
    [
      "flinkBucket",
      "Existing Flink checkpoint bucket (optional)",
      "Flink jobs and their Kubernetes service account are a separate reviewed integration.",
    ],
  ]) {
    const label = document.createElement("label");
    const input = document.createElement("input");
    input.name = name;
    label.append(
      document.createTextNode(title),
      input,
      textElement("small", hint),
    );
    $("#iam-existing-fields").append(label);
  }
  field(
    $("#iam-runner-fields"),
    "iam.terraformRoleArn",
    "Existing Terraform provisioning role to assume (optional)",
    {
      nullable: true,
      help: "Blank uses the runner's existing AWS credential chain. An ARN adds provider assume_role only; it does not grant permission to assume it or grant Kubernetes access.",
      placeholder: "arn:aws:iam::123456789012:role/InfrastructureProvisioner",
    },
  );
  for (const [id, title] of [
    ["mimir", "Mimir metrics storage"],
    ["tempo", "Tempo trace storage"],
    ["flink", "Flink checkpoint foundation"],
    ["pyroscope", "Pyroscope profiles — only when enabled"],
  ]) {
    const row = group($("#iam-workload-choices"), title, "");
    row.dataset.iamRole = id;
    const label = document.createElement("label");
    const choice = document.createElement("select");
    choice.name = `iam-choice-${id}`;
    for (const [value, text] of [
      ["create", "Terraform creates a dedicated role"],
      ["reuse", "Reuse an externally managed role"],
    ]) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = text;
      choice.append(option);
    }
    label.append(document.createTextNode(`${title}: ownership`), choice);
    const reference = document.createElement("label");
    reference.className = "iam-reuse-field";
    const input = document.createElement("input");
    input.name = `iam-role-${id}`;
    input.placeholder = `arn:aws:iam::123456789012:role/existing-${id}`;
    reference.append(
      document.createTextNode(`Existing ${id} role ARN`),
      input,
      textElement(
        "small",
        "The owner retains trust and policies. Review required_role_policies from Terraform; a role ARN alone is not proof of access.",
      ),
    );
    row.append(label, reference);
    choice.addEventListener("change", () => {
      if (choice.value === "reuse")
        advanced.iam.existingWorkloadRoles[id] = input.value.trim();
      else delete advanced.iam.existingWorkloadRoles[id];
      syncIAMChoices();
      update();
    });
    input.addEventListener("input", () => {
      advanced.iam.existingWorkloadRoles[id] = input.value.trim();
      update();
    });
  }
  syncIAMChoices();
}
function syncIAMChoices() {
  for (const row of $$("[data-iam-role]")) {
    const id = row.dataset.iamRole;
    const reused = Object.hasOwn(advanced.iam.existingWorkloadRoles, id);
    row.querySelector("select").value = reused ? "reuse" : "create";
    row.querySelector("input").value =
      advanced.iam.existingWorkloadRoles[id] ?? "";
    row.querySelector(".iam-reuse-field").hidden = !reused;
  }
}
function updateIAMSummary() {
  const target = $("#iam-inventory");
  target.replaceChildren();
  try {
    const plan = iamPlan(request());
    $("#iam-summary").textContent =
      `${plan.summary.createdRoles} IAM roles to create · ${plan.summary.reusedRoles} existing runtime roles to reuse · ${plan.summary.createdAssociations} Pod Identity associations to create. ${plan.mode === "new" ? "Existing principals and reused roles remain externally owned." : "All AWS role and association ownership remains external."}`;
    const execution = group(
      target,
      "Deployment identity and account",
      plan.execution.mechanism,
    );
    execution.append(
      textElement(
        "p",
        `Deployment account: ${plan.accountId || "not yet established"}. ${plan.accountSource}.`,
        "hint",
      ),
    );
    if (plan.execution.roleArn)
      execution.append(
        textElement("p", plan.execution.roleArn, "iam-coordinate"),
      );
    for (const requirement of plan.execution.requirements)
      execution.append(textElement("p", requirement, "hint"));
    for (const role of plan.roles) {
      const card = document.createElement("details");
      card.className = "iam-role option-group";
      card.append(
        textElement(
          "summary",
          `${role.label} — ${role.ownership === "created" ? "Terraform creates" : "Externally owned"}`,
        ),
      );
      if (role.arn || role.namePrefix)
        card.append(
          textElement(
            "p",
            role.arn
              ? `Existing ARN: ${role.arn}`
              : `Created name prefix: ${role.namePrefix}`,
            "iam-coordinate",
          ),
        );
      for (const [title, value] of [
        ["Who can assume this role", role.trust],
        ["Permissions and resource scope", role.permissions],
        ["Pod Identity binding", role.association],
      ]) {
        if (value) {
          card.append(textElement("h3", title));
          card.append(
            textElement("pre", JSON.stringify(value, null, 2), "iam-policy"),
          );
        }
      }
      for (const note of role.notes || [])
        card.append(textElement("p", note, "hint"));
      target.append(card);
    }
    for (const [title, entries] of [
      ["EKS access grants (not IAM role creation)", plan.accessGrants],
      ["Owner prerequisites", plan.prerequisites],
      ["Scope and limits", plan.boundaries],
    ]) {
      if (!entries?.length) continue;
      const section = group(target, title, "");
      for (const item of entries)
        section.append(
          textElement(
            typeof item === "string" ? "p" : "pre",
            typeof item === "string" ? item : JSON.stringify(item, null, 2),
            typeof item === "string" ? "hint" : "iam-policy",
          ),
        );
    }
  } catch (error) {
    $("#iam-summary").textContent = error.message;
  }
}
function buildAdvanced() {
  const startup = group(
    $("#startup-controls"),
    "Wait for pipeline dependencies",
    "Before log admission and consumption start, gateways wait for Kafka topic readiness, writers wait for ClickHouse schema on every replica, and quota admission waits for the active gateway. Exhausted checks keep the workload blocked; Kubernetes may restart the check. These bounds apply to each init-container invocation, not the whole deployment.",
  );
  const startupFields = grid(startup);
  field(startupFields, "startupGates.attempts", "Maximum check attempts", {
    type: "number",
    min: 1,
    max: 360,
    step: 1,
    help: "1–360 attempts; default 120. Increasing this cannot repair a missing topic or schema.",
  });
  field(startupFields, "startupGates.delaySeconds", "Retry delay (seconds)", {
    type: "number",
    min: 1,
    max: 30,
    step: 1,
    help: "1–30 seconds between failed checks; default 5.",
  });
  field(
    startupFields,
    "startupGates.timeoutSeconds",
    "Request timeout (seconds)",
    {
      type: "number",
      min: 5,
      max: 60,
      step: 1,
      help: "5–60 seconds per request; default 20. Kafka and ClickHouse clients have a further 5-second forced-termination grace period.",
    },
  );
  const privacy = $("#privacy-controls");
  const logs = group(
    privacy,
    "Log privacy",
    "Filtering happens before outgoing log buffers and repeats at the gateway. Presets match common patterns; they cannot detect every sensitive value. Existing raw files and stored data are unchanged.",
  );
  field(logs, "privacy.logs.mode", "Log policy", {
    choices: [
      ["off", "Off — preserve log content"],
      ["redact", "Redact matching content"],
      ["drop", "Drop matching log entries"],
    ],
  });
  field(logs, "privacy.logs.patterns", "Enabled matching presets", {
    type: "list",
    help: "Comma-separated preset names: email, bearer, credential. Controls pattern families, not custom regular expressions.",
  });
  const traces = group(
    privacy,
    "Trace privacy",
    "Strict allowlisting is lossy: unwanted attributes, events, status text and scope details are removed; linked spans are dropped. This can change messaging visibility and RED coverage.",
  );
  field(traces, "privacy.traces.mode", "Trace policy", {
    choices: [
      ["off", "Off — preserve trace content"],
      ["allowlist", "Strict attribute allowlist"],
    ],
  });
  const traceFields = group(
    traces,
    "Trace allowlist details",
    "Required service and regional identity attributes must stay in the resource allowlist.",
    { details: true },
  );
  field(
    traceFields,
    "privacy.traces.keepSpanName",
    "Retain original span names",
    {
      type: "checkbox",
      help: "Names may contain sensitive or high-cardinality values. Off replaces them with a bounded name.",
    },
  );
  field(
    traceFields,
    "privacy.traces.allowedSpanAttributes",
    "Allowed span attributes",
    { type: "list", help: "Comma or newline separated attribute names." },
  );
  field(
    traceFields,
    "privacy.traces.allowedResourceAttributes",
    "Allowed resource attributes",
    {
      type: "list",
      help: "Keep service.name, service.namespace, cloud.region and k8s.cluster.name. Dropping attributes does not aggregate metrics.",
    },
  );

  const graph = group(
    $("#service-graph-controls"),
    "Service dependency graphs",
    "Pair client/server spans before tail sampling on the trace-affine sampler tier. SDK sampling, missing peers and queue loss still limit coverage; graph metrics are not complete request accounting.",
  );
  field(graph, "serviceGraph.enabled", "Generate service graph metrics", {
    type: "checkbox",
  });
  const graphFields = group(
    graph,
    "Graph scope and bounds",
    "Only listed service names enter the graph branch. Use names unique within this cell; namespaces do not disambiguate graph labels.",
    { when: "graph" },
  );
  field(graphFields, "serviceGraph.services", "Allowed service names", {
    type: "list",
    help: "1–64 exact service.name values, comma or newline separated. Both ends of an edge must be allowed.",
    placeholder: "checkout, payments",
  });
  const graphAdvanced = group(
    graphFields,
    "Pairing, memory and histogram settings",
    "Changing sampler membership requires a drain of in-flight traces and graph pairs. Histogram bucket changes need a reviewed migration window.",
    { details: true },
  );
  const gg = grid(graphAdvanced);
  field(gg, "serviceGraph.storeTTLSeconds", "Pairing TTL (seconds)", {
    type: "number",
    min: 2,
    max: 120,
  });
  field(gg, "serviceGraph.storeMaxItems", "Maximum pending pairs per sampler", {
    type: "number",
    min: 100,
    max: 100000,
  });
  field(
    gg,
    "serviceGraph.flushIntervalSeconds",
    "Metric flush interval (seconds)",
    { type: "number", min: 5, max: 60 },
  );
  field(gg, "serviceGraph.cacheLoopSeconds", "Pair cache sweep (seconds)", {
    type: "number",
    min: 15,
    max: 120,
  });
  field(
    graphAdvanced,
    "serviceGraph.histogramBucketsSeconds",
    "Latency histogram buckets (seconds)",
    {
      type: "numbers",
      help: "Positive, strictly increasing boundaries. Compute rates per writer, aggregate buckets, then calculate quantiles.",
    },
  );

  const security = group(
    $("#security-controls"),
    "Grafana access and identity",
    "Supply resource names and public endpoints only. Create Secrets through your own protected workflow; this studio never accepts client secrets, passwords or tokens.",
  );
  enable(
    security,
    "security-enabled",
    "Include an explicit security profile",
    "Off keeps the platform's base configuration. On generates a separate, reviewable profile and prerequisites.",
    (checked) => {
      advanced.securityProfile = checked ? securityDefaults() : null;
    },
  );
  const sf = group(
    security,
    "Existing resources",
    "The ingress controller, identity provider and mesh remain externally owned. No profile installs or adopts them.",
    { when: "security" },
  );
  field(
    sf,
    "securityProfile.serverSecretName",
    "Grafana encryption Secret name",
    {
      help: "Existing Secret in observability with key secret-key, shared by every Grafana replica. Keep the existing key when migrating.",
    },
  );
  field(sf, "securityProfile.access.mode", "Browser access", {
    choices: [
      ["port-forward", "Local port-forward"],
      ["ingress", "Existing Kubernetes ingress controller"],
      ["external-proxy", "Existing externally managed proxy"],
    ],
    onChange: (mode) => {
      const p = advanced.securityProfile;
      p.access =
        mode === "port-forward"
          ? { mode, url: "http://localhost:3000" }
          : {
              mode,
              url: p.access.url.startsWith("https:") ? p.access.url : "",
              proxyNamespace: p.access.proxyNamespace || "ingress-system",
              ...(mode === "ingress"
                ? {
                    ingressClassName: p.access.ingressClassName || "",
                    tlsSecretName: p.access.tlsSecretName || "grafana-tls",
                  }
                : {}),
            };
      if (mode === "port-forward") {
        p.authentication = { mode: "local" };
        p.internalTransport = { mode: "cluster-network" };
      }
      advancedValues();
    },
  });
  field(sf, "securityProfile.access.url", "Grafana root URL", {
    type: "url",
    help: "Local: http://localhost:3000. HTTPS: port 443, root path only, without credentials, query strings or fragments.",
    placeholder: "https://grafana.example.com",
  });
  const publicFields = group(
    sf,
    "HTTPS access",
    "Configure the existing proxy route and DNS separately. Direct ALB pod targets need a separately reviewed network policy.",
    { when: "public-access" },
  );
  field(
    publicFields,
    "securityProfile.access.proxyNamespace",
    "Existing proxy/controller namespace",
  );
  const ingress = grid(publicFields);
  ingress.dataset.when = "ingress";
  field(
    ingress,
    "securityProfile.access.ingressClassName",
    "Existing ingress class",
  );
  field(
    ingress,
    "securityProfile.access.tlsSecretName",
    "Existing TLS Secret name",
    {
      help: "In observability; provision and rotate the certificate externally.",
    },
  );
  field(publicFields, "securityProfile.authentication.mode", "Sign-in", {
    choices: [
      ["local", "Local Grafana authentication"],
      ["oauth", "OIDC / OAuth with explicit group roles"],
    ],
    onChange: (mode) => {
      advanced.securityProfile.authentication =
        mode === "local" ? { mode } : oauthDefaults();
      advancedValues();
    },
  });
  const auth = group(
    publicFields,
    "Identity provider settings",
    "Register the exact Grafana root URL plus /login/generic_oauth as the callback. PKCE is enabled and unmapped groups cannot sign in. Real provider claim and revocation behavior belongs in the later qualification batch.",
    { when: "oauth" },
  );
  field(
    auth,
    "securityProfile.authentication.secretName",
    "OAuth Secret name",
    {
      help: "Existing Secret keys: client-id and client-secret. Enter only its Kubernetes name.",
    },
  );
  field(
    auth,
    "securityProfile.authentication.authURL",
    "Authorization endpoint",
    { type: "url", placeholder: "https://identity.example.com/authorize" },
  );
  field(auth, "securityProfile.authentication.tokenURL", "Token endpoint", {
    type: "url",
    placeholder: "https://identity.example.com/oauth/token",
  });
  field(
    auth,
    "securityProfile.authentication.userInfoURL",
    "User info endpoint",
    { type: "url", placeholder: "https://identity.example.com/userinfo" },
  );
  field(auth, "securityProfile.authentication.scopes", "OAuth scopes", {
    type: "list",
    help: "Include openid. offline_access also requires refresh tokens.",
  });
  field(
    auth,
    "securityProfile.authentication.groupsClaim",
    "Groups claim path",
    {
      help: "A simple dot-separated claim path, such as groups or realm_access.roles; no expressions.",
    },
  );
  for (const role of ["Viewer", "Editor", "Admin"])
    field(
      auth,
      `securityProfile.authentication.roles.${role}`,
      `${role} groups`,
      {
        type: "list",
        optional: true,
        help: `${role === "Admin" ? "Grafana organization Admin, not server administrator. " : ""}Blank omits this role. A group may map to only one role.`,
      },
    );
  const authAdvanced = group(
    auth,
    "Identity provider options",
    "Secrets are resolved at runtime. A custom CA reference must already exist in observability.",
    { details: true },
  );
  field(
    authAdvanced,
    "securityProfile.authentication.useRefreshToken",
    "Use refresh tokens",
    {
      type: "checkbox",
      help: "Optional. Confirm the provider supports refresh tokens and review logout/revocation behavior.",
    },
  );
  field(
    authAdvanced,
    "securityProfile.authentication.caConfigMap",
    "Custom CA ConfigMap name (optional)",
    {
      optional: true,
      help: "Existing key ca.crt. Leave blank for the default trust store.",
    },
  );
  field(
    publicFields,
    "securityProfile.internalTransport.mode",
    "Proxy-to-Grafana transport",
    {
      choices: [
        ["cluster-network", "Cluster network — no added transport encryption"],
        ["existing-mesh", "Existing mesh owned by the networking team"],
      ],
      onChange: (mode) => {
        advanced.securityProfile.internalTransport =
          mode === "cluster-network"
            ? { mode }
            : { mode, provider: "istio", grafanaClientIdentities: [] };
        advancedValues();
      },
    },
  );
  const mesh = group(
    publicFields,
    "External mesh owner review",
    "Only Grafana receives injection settings. Generated policy examples require a separate review and apply by the mesh owner; they do not secure the other telemetry hops or authenticate tenants.",
    { when: "mesh" },
  );
  field(mesh, "securityProfile.internalTransport.provider", "Existing mesh", {
    choices: [
      ["istio", "Istio"],
      ["linkerd", "Linkerd"],
    ],
  });
  field(
    mesh,
    "securityProfile.internalTransport.grafanaClientIdentities",
    "Exact permitted proxy identities",
    {
      type: "list",
      help: "Istio: cluster.local/ns/ingress-system/sa/shared-ingress. Linkerd: shared-ingress.ingress-system.serviceaccount.identity.linkerd.cluster.local. No wildcards.",
    },
  );
  security.append(
    textElement(
      "p",
      "These profiles cover Grafana access. Internal OTLP/Arrow, Kafka, storage transport and authenticated intake still require the documented environment integrations.",
      "callout",
    ),
  );

  buildOperations();
  advancedValues();
}
function buildOperations() {
  const isolation = group(
    $("#isolation-controls"),
    "Regional isolation and budget review",
    "Preview declared service allocations and scoped ingestion budgets. This is not a measured capacity result, a dollar forecast or proof that backends are isolated.",
  );
  field(isolation, "isolation.enabled", "Include a cell plan", {
    type: "checkbox",
  });
  const fields = group(
    isolation,
    "Cell ownership",
    "One platform release per cluster. Isolated clusters increase cost and require separate roles, buckets, quota backends and database identities.",
    { when: "isolation" },
  );
  field(fields, "isolation.mode", "Isolation model", {
    choices: [
      ["shared", "Shared regional cell"],
      [
        "isolated-clusters",
        "Separate clusters with separately reviewed backends",
      ],
    ],
  });
  field(fields, "isolation.id", "This cell's stable ID");
  field(fields, "isolation.services", "Services assigned to this cell", {
    type: "list",
    placeholder: "applications/checkout, applications/payments",
    help: "Exact namespace/service identities; duplicates within one region are rejected by the offline planner.",
  });
  field(
    fields,
    "isolation.grafanaDatabaseIdentity",
    "Grafana database identity",
    {
      help: "Nonsecret host/database name. Required for isolated clusters. Separate database names can still share instance CPU.",
    },
  );
  const peers = group(
    fields,
    "Other regional cells",
    "Reference reviewed local platform contract files. This bundle does not create, copy or validate those other environments.",
    { when: "isolated" },
  );
  const rows = document.createElement("div");
  rows.id = "cell-rows";
  peers.append(rows);
  const add = textElement("button", "+ Add cell", "secondary");
  add.type = "button";
  add.id = "add-cell";
  add.addEventListener("click", () => {
    advanced.isolation.additionalCells.push({
      id: "",
      platform: "",
      services: [],
    });
    renderCells();
    update();
  });
  peers.append(add);
  renderCells();

  const qual = group(
    $("#qualification-controls"),
    "Deferred AWS qualification and recovery",
    "Collect prerequisites for the shared 26-gate catalog. All checks remain pending. Evidence is operator-attested and bound to a reviewed revision and exact configuration; the browser never contacts AWS, sends alerts or restores data.",
  );
  enable(
    qual,
    "qualification-enabled",
    "Include a qualification and recovery draft",
    "Set owners and recovery choices now. Fill final revision and artifact hashes after offline rendering and review.",
    (checked) => {
      advanced.qualification = checked ? createQualification(request()) : null;
    },
  );
  const qf = group(
    qual,
    "Run identity",
    "The selected deployment controls source region, cluster, installation mode, Hubble, profiling and service graph scope.",
    { when: "qualification" },
  );
  const ids = grid(qf);
  field(ids, "qualification.runId", "Unique qualification run ID");
  field(ids, "qualification.environment.accountId", "Source AWS account ID", {
    help: "Twelve digits. Confirm the deployment account; 000000000000 is an unresolved placeholder. A configured Terraform role or existing cluster ARN determines the account, never the administrator ARN.",
  });
  field(ids, "qualification.environment.context", "Exact Kubernetes context");
  field(
    ids,
    "qualification.environment.region",
    "Source region (from Environment)",
    { readOnly: true },
  );
  field(
    ids,
    "qualification.environment.cluster",
    "Source cluster (from Environment)",
    { readOnly: true },
  );
  field(ids, "qualification.environment.mode", "Installation scope (derived)", {
    readOnly: true,
  });
  const scope = group(
    qf,
    "Additional acceptance scope",
    "These flags include checks for externally configured features. Selecting a flag does not install Flink, reconcile GitOps or send a real notification.",
    { details: true },
  );
  field(
    scope,
    "qualification.features.flink",
    "Include Flink recovery checks",
    { type: "checkbox" },
  );
  field(
    scope,
    "qualification.features.gitops",
    "Include GitOps reconciliation checks",
    { type: "checkbox" },
  );
  field(
    scope,
    "qualification.features.externalNotificationDelivery",
    "Include a later real notification delivery gate",
    {
      type: "checkbox",
      help: "A real delivery test still needs explicit authorization when you execute the batch.",
    },
  );
  const recovery = group(
    qf,
    "Recovery owner and method",
    "Each stateful backend needs its own consistent recovery procedure. Choosing native means you supply backend-specific jobs; choosing external means you supply an existing provider/runbook.",
  );
  const rg = grid(recovery);
  field(rg, "qualification.recovery.backupMode", "Backup workflow", {
    choices: [
      ["native", "Native backend procedures"],
      ["external", "Existing external backup provider"],
    ],
  });
  field(rg, "qualification.recovery.owner", "Accountable recovery owner");
  field(
    rg,
    "qualification.recovery.providerRef",
    "Provider/runbook reference",
    {
      nullable: true,
      help: "Required for external mode. Use a nonsecret ticket or runbook identifier.",
    },
  );
  field(
    rg,
    "qualification.recovery.rpoMinutes",
    "Maximum data loss / RPO (minutes)",
    {
      type: "number",
      nullable: true,
      min: 0,
      max: 525600,
      help: "Blank remains undecided; recovery cannot pass until a numeric target is reviewed.",
    },
  );
  field(
    rg,
    "qualification.recovery.rtoMinutes",
    "Maximum recovery time / RTO (minutes)",
    {
      type: "number",
      nullable: true,
      min: 0,
      max: 525600,
      help: "A target, not a tested result.",
    },
  );
  const dest = group(
    recovery,
    "Isolated restore destination",
    "Restore into a different account, region or cluster identity. Before execution, confirm storage and external backends are genuinely separate from production.",
  );
  const dg = grid(dest);
  field(
    dg,
    "qualification.recovery.destination.accountId",
    "Restore account ID",
  );
  field(dg, "qualification.recovery.destination.region", "Restore region");
  field(dg, "qualification.recovery.destination.cluster", "Restore cluster");
  field(
    recovery,
    "qualification.recovery.crossRegion.enabled",
    "Plan a cross-region recovery rehearsal",
    {
      type: "checkbox",
      help: "Explicit opt-in only. Requires a different destination region, reviewed targets and an egress/storage cost assessment. This does not enable replication.",
    },
  );
  const cross = group(
    recovery,
    "Cross-region opt-in prerequisites",
    "Regional ingest and raw data storage stay local unless you separately implement and qualify a replication workflow.",
    { when: "cross-region" },
  );
  const cg = grid(cross);
  field(
    cg,
    "qualification.recovery.crossRegion.rpoMinutes",
    "Cross-region RPO (minutes)",
    { type: "number", nullable: true, min: 0, max: 525600 },
  );
  field(
    cg,
    "qualification.recovery.crossRegion.rtoMinutes",
    "Cross-region RTO (minutes)",
    { type: "number", nullable: true, min: 0, max: 525600 },
  );
  field(
    cross,
    "qualification.recovery.crossRegion.costAssessmentRef",
    "Reviewed cost assessment reference",
    {
      nullable: true,
      help: "Nonsecret document/ticket identifier covering replication, restore and ongoing egress.",
    },
  );
  const evidence = group(
    qf,
    "Revision and artifact binding",
    "Zero hashes are deliberate placeholders for a pending draft. Replace them with reviewed values after rendering; placeholders cannot qualify the environment identity gate.",
    { details: true },
  );
  field(
    evidence,
    "qualification.sourceRevision",
    "Reviewed source revision (40-character Git SHA)",
  );
  field(
    evidence,
    "qualification.deploymentArtifacts.platformContractSHA256",
    "Platform contract SHA-256",
  );
  field(
    evidence,
    "qualification.deploymentArtifacts.renderedValuesSHA256",
    "Rendered values SHA-256",
  );
  const tf = document.createElement("div");
  evidence.append(tf);
  field(
    tf,
    "qualification.deploymentArtifacts.terraformPlanSHA256",
    "Reviewed Terraform plan SHA-256",
    {
      nullable: true,
      help: "Existing-cluster software-only setup uses null and does not manage Terraform resources.",
    },
  );
}
function renderCells() {
  const rows = $("#cell-rows");
  rows.replaceChildren();
  advanced.isolation.additionalCells.forEach((cell, i) => {
    const row = group(
      rows,
      `Additional cell ${i + 1}`,
      "The referenced contract must exist relative to the generated cells file.",
    );
    const g = grid(row);
    field(g, `isolation.additionalCells.${i}.id`, "Stable cell ID");
    field(
      g,
      `isolation.additionalCells.${i}.platform`,
      "Local platform contract path",
      { placeholder: "../other-cell/platform.json" },
    );
    field(
      g,
      `isolation.additionalCells.${i}.grafanaDatabaseIdentity`,
      "Grafana database identity",
      { optional: true, placeholder: "database.example.internal/grafana" },
    );
    field(
      g,
      `isolation.additionalCells.${i}.services`,
      "Assigned namespace/services",
      { type: "list" },
    );
    const remove = textElement("button", "Remove this cell", "secondary");
    remove.type = "button";
    remove.addEventListener("click", () => {
      advanced.isolation.additionalCells.splice(i, 1);
      renderCells();
      update();
    });
    row.append(remove);
  });
  for (const input of rows.querySelectorAll("[data-advanced]")) {
    const value = at(input.dataset.advanced);
    input.value = Array.isArray(value) ? value.join(", ") : (value ?? "");
  }
}
function updateAdvanced() {
  const p = advanced.securityProfile,
    q = advanced.qualification;
  const conditions = {
    graph: advanced.serviceGraph.enabled,
    security: !!p,
    "public-access": !!p && p.access.mode !== "port-forward",
    ingress: p?.access.mode === "ingress",
    oauth: p?.authentication.mode === "oauth",
    mesh: p?.internalTransport.mode === "existing-mesh",
    isolation: advanced.isolation.enabled,
    isolated:
      advanced.isolation.mode === "isolated-clusters" ||
      advanced.isolation.additionalCells.length > 0,
    qualification: !!q,
    "cross-region": q?.recovery.crossRegion.enabled,
  };
  $$("[data-when]").forEach((node) => {
    node.hidden = !conditions[node.dataset.when];
  });
  if (q) {
    q.environment.region = form.elements.region.value.trim();
    q.environment.cluster = form.elements.name.value.trim();
    q.environment.mode =
      form.elements.mode.value === "new" ? "managed" : "software-only";
    q.features.hubble = form.elements.hubble.checked;
    q.features.profiling = form.elements.profiling.checked;
    q.features.serviceGraphs = advanced.serviceGraph.enabled;
    for (const key of ["region", "cluster", "mode"])
      form.elements[`qualification.environment.${key}`].value =
        q.environment[key];
  }
}

function request() {
  const data = new FormData(form),
    r = structuredClone(defaults);
  for (const k of Object.keys(r)) {
    if (["version", "existing", ...advancedKeys].includes(k)) continue;
    if (["hubble", "profiling"].includes(k)) r[k] = data.has(k);
    else if (["zones", "profileNamespaces"].includes(k))
      r[k] = list(data.get(k));
    else if (typeof defaults[k] === "number") r[k] = Number(data.get(k));
    else if (data.has(k)) r[k] = data.get(k).trim();
  }
  if (r.mode === "existing") {
    for (const [control, key] of Object.entries({
      clusterARN: "cluster_arn",
      operatorNamespaces: "operator_namespaces",
      grafanaDBName: "grafana_db_name",
      grafanaDBUser: "grafana_db_user",
    })) {
      const value = data.get(control).trim();
      if (value)
        existingExtras[key] =
          key === "operator_namespaces" ? list(value) : value;
      else delete existingExtras[key];
    }
    r.existing = {
      ...existingExtras,
      region: r.region,
      cluster_name: r.name,
      vpc_cidr: r.vpcCIDR,
      availability_zones: r.zones,
      quota_redis_host: data.get("quotaRedis").trim(),
      secret_source: "existing",
      storage_class: data.get("storageClass").trim(),
      create_storage_class: false,
      role_arns: {
        mimir: data.get("mimirRole").trim(),
        tempo: data.get("tempoRole").trim(),
        ...(data.get("flinkRole").trim()
          ? { flink: data.get("flinkRole").trim() }
          : {}),
        ...(data.get("pyroscopeRole").trim()
          ? { pyroscope: data.get("pyroscopeRole").trim() }
          : {}),
      },
      buckets: {
        "mimir-blocks": data.get("mimirBlocks").trim(),
        "mimir-ruler": data.get("mimirRuler").trim(),
        "mimir-alertmanager": data.get("mimirAlertmanager").trim(),
        tempo: data.get("tempoBucket").trim(),
        ...(data.get("flinkBucket").trim()
          ? { flink: data.get("flinkBucket").trim() }
          : {}),
        ...(data.get("pyroscopeBucket").trim()
          ? { pyroscope: data.get("pyroscopeBucket").trim() }
          : {}),
      },
      operators: Object.fromEntries(
        operators.map((k) => [k, data.has("install-" + k)]),
      ),
    };
  }
  return {
    ...r,
    ...structuredClone(advanced),
    iam:
      r.mode === "new"
        ? structuredClone(advanced.iam)
        : structuredClone(defaults.iam),
  };
}
function showFile(name) {
  $("#preview").textContent = files[name];
  $$("#file-tabs button").forEach((b) =>
    b.classList.toggle("selected", b.textContent === name),
  );
}
function update() {
  updateAdvanced();
  const existing = new FormData(form).get("mode") === "existing";
  $("#existing-fields").hidden = !existing;
  $("#admin-field").hidden = existing;
  $("#iam-new-controls").hidden = existing;
  $("#iam-existing-controls").hidden = !existing;
  $("#iam-mode-note").hidden =
    !existing ||
    (!advanced.iam.terraformRoleArn &&
      Object.keys(advanced.iam.existingWorkloadRoles).length === 0);
  $("#deployment-error").textContent = "";
  if (step === 1 || step === 6) updateIAMSummary();
  if (step === 6) {
    try {
      const r = request();
      files = compileRequest(r);
      $("#review-status").textContent =
        `${Object.keys(files).length} reviewable files · No infrastructure created. ${r.securityProfile ? "Existing security resources need owner review. " : ""}${r.qualification ? "Qualification remains pending; complete artifact hashes and execute the deferred batch later." : "Live AWS validation remains deferred; enable a qualification draft in Operations to collect its prerequisites."}`;
      $("#file-tabs").replaceChildren();
      for (const name of Object.keys(files)) {
        const b = document.createElement("button");
        b.type = "button";
        b.textContent = name;
        b.addEventListener("click", () => showFile(name));
        $("#file-tabs").append(b);
      }
      showFile(
        Object.keys(files).find(
          (k) => k.includes("platform") || k.includes("main.tf"),
        ) || "tuning.json",
      );
      $("#download").disabled = false;
    } catch (e) {
      files = {};
      $("#review-status").textContent =
        "Configuration needs attention. No files are ready for download yet.";
      $("#preview").textContent =
        "Resolve the configuration issue below to generate your files.";
      $("#file-tabs").replaceChildren();
      $("#deployment-error").textContent = e.message;
      $("#download").disabled = true;
    }
  }
}
function setStep(n, moveFocus = true) {
  step = n;
  $$("[data-page]").forEach((e) => (e.hidden = +e.dataset.page !== n));
  $$("[data-step]").forEach((e) => {
    e.classList.toggle("selected", +e.dataset.step === n);
    e.setAttribute("aria-current", +e.dataset.step === n ? "step" : "false");
  });
  $("#back").hidden = n === 0;
  $("#next").hidden = n === 6;
  update();
  if (moveFocus) {
    const legend = $(`[data-page="${n}"] legend`);
    legend.tabIndex = -1;
    legend.focus();
  }
}
$$("[data-step]").forEach((e) =>
  e.addEventListener("click", () => setStep(+e.dataset.step)),
);
$("#next").addEventListener("click", () => setStep(Math.min(6, step + 1)));
$("#back").addEventListener("click", () => setStep(Math.max(0, step - 1)));
form.addEventListener("input", update);
form.addEventListener("change", update);
form.addEventListener("submit", (e) => e.preventDefault());
$("#download").addEventListener("click", () => {
  try {
    const r = request();
    save(r.name + ".zip", zipFiles(compileRequest(r)), "application/zip");
  } catch (e) {
    $("#deployment-error").textContent = e.message;
  }
});
function parseJSON(text) {
  try {
    return JSON.parse(text);
  } catch {
    throw new Error(
      "Use valid JSON. Never include passwords, tokens or secret values.",
    );
  }
}
function setExisting(e) {
  for (const [k, v] of Object.entries({
    name: e.cluster_name,
    region: e.region,
    vpcCIDR: e.vpc_cidr,
    zones: e.availability_zones.join(", "),
    quotaRedis: e.quota_redis_host,
    storageClass: e.storage_class,
    mimirRole: e.role_arns?.mimir,
    tempoRole: e.role_arns?.tempo,
    flinkRole: e.role_arns?.flink,
    pyroscopeRole: e.role_arns?.pyroscope,
    mimirBlocks: e.buckets?.["mimir-blocks"],
    mimirRuler: e.buckets?.["mimir-ruler"],
    mimirAlertmanager: e.buckets?.["mimir-alertmanager"],
    tempoBucket: e.buckets?.tempo,
    flinkBucket: e.buckets?.flink,
    pyroscopeBucket: e.buckets?.pyroscope,
    clusterARN: e.cluster_arn,
    operatorNamespaces: e.operator_namespaces?.join(", "),
    grafanaDBName: e.grafana_db_name,
    grafanaDBUser: e.grafana_db_user,
  }))
    form.elements[k].value = v || "";
  for (const k of operators)
    form.elements["install-" + k].checked = e.operators[k];
  existingExtras = existingOptions(e);
}
$("#import-existing").addEventListener("click", () => {
  try {
    const e = validateExistingCoordinates(
      parseJSON(form.elements.existing.value),
    );
    setExisting(e);
    form.elements.mode.value = "existing";
    update();
  } catch (e) {
    $("#deployment-error").textContent = e.message;
  }
});
function restoreRequest(value) {
  const r = validateRequest(value);
  // Complete validation precedes all DOM mutation so failed imports preserve the form.
  for (const [key, value] of Object.entries(r)) {
    if (["version", "existing", ...advancedKeys].includes(key)) continue;
    const control = form.elements[key];
    if (!control) continue;
    if (typeof value === "boolean") control.checked = value;
    else control.value = Array.isArray(value) ? value.join(", ") : value;
  }
  existingExtras = {};
  if (r.existing) setExisting(r.existing);
  advanced = Object.fromEntries(
    advancedKeys.map((key) => [key, structuredClone(r[key])]),
  );
  renderCells();
  advancedValues();
  update();
}
$("#request-import").addEventListener("change", async (event) => {
  try {
    const file = event.target.files[0];
    if (!file) return;
    if (file.size > 1024 * 1024)
      throw new Error("Deployment request exceeds the 1 MiB browser limit");
    restoreRequest(parseJSON(await file.text()));
  } catch (error) {
    $("#deployment-error").textContent = error.message;
  } finally {
    event.target.value = "";
  }
});
$("#request-export").addEventListener("click", () => {
  try {
    const r = validateRequest(request());
    save(
      "deployment-request.json",
      JSON.stringify(r, null, 2) + "\n",
      "application/json",
    );
    $("#deployment-error").textContent = "";
  } catch (error) {
    $("#deployment-error").textContent = error.message;
  }
});
function showCatalog() {
  const target = $("#catalog-list"),
    query = $("#catalog-search").value.toLowerCase();
  target.replaceChildren();
  for (const s of catalog.filter((s) =>
    [s.name, s.namespace, s.owner, s.description]
      .join(" ")
      .toLowerCase()
      .includes(query),
  )) {
    const card = document.createElement("article");
    card.className = "service-card";
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = s.lifecycle;
    const title = document.createElement("h2");
    title.textContent = s.name;
    const owner = document.createElement("p");
    owner.textContent = `${s.namespace} · ${s.owner}`;
    const desc = document.createElement("p");
    desc.textContent = s.description;
    card.append(tag, title, owner, desc);
    for (const [text, url] of [
      ["Repository ↗", s.repository],
      ["Runbook ↗", s.runbook],
    ]) {
      const a = document.createElement("a");
      a.textContent = text;
      a.href = url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      card.append(a);
    }
    target.append(card);
  }
  if (!target.children.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = catalog.length
      ? "No services match your search."
      : "No services loaded. Import catalog/index.json or register your first service.";
    target.append(empty);
  }
}
$("#catalog-import").addEventListener("change", async (e) => {
  try {
    const file = e.target.files[0];
    if (!file) return;
    if (file.size > 2 * 1024 * 1024)
      throw new Error("Catalog exceeds the 2 MiB browser limit");
    const data = JSON.parse(await file.text());
    if (
      data.apiVersion !== "observability/v1" ||
      !Array.isArray(data.services) ||
      data.services.length > 1000
    )
      throw new Error("Expected a versioned catalog with up to 1,000 services");
    const services = data.services.map(validateService);
    if (
      new Set(services.map((s) => s.namespace + "/" + s.name)).size !==
      services.length
    )
      throw new Error("Duplicate service identity");
    catalog = services;
    $("#catalog-error").textContent = "";
    showCatalog();
  } catch (e) {
    $("#catalog-error").textContent = e.message;
  }
});
$("#catalog-search").addEventListener("input", showCatalog);
$("#service-form").addEventListener("submit", (e) => {
  e.preventDefault();
  try {
    const d = new FormData(e.target),
      s = validateService({
        apiVersion: "observability/v1",
        name: d.get("name").trim(),
        namespace: d.get("namespace").trim(),
        owner: d.get("owner").trim(),
        description: d.get("description").trim(),
        repository: d.get("repository").trim(),
        runbook: d.get("runbook").trim(),
        lifecycle: d.get("lifecycle"),
        profiling: d.has("profiling"),
        dependencies: list(d.get("dependencies")),
        slo: {
          enabled: d.has("sloEnabled"),
          target: Number(d.get("sloTarget")),
          source: d.get("sloSource"),
        },
      });
    save(
      s.name + ".json",
      JSON.stringify(s, null, 2) + "\n",
      "application/json",
    );
    $("#service-error").textContent = "";
    $("#service-preview").hidden = false;
    $("#service-preview").textContent = JSON.stringify(s, null, 2);
    $("#service-next").hidden = false;
    $("#service-next").textContent =
      d.get("creation") === "scaffold"
        ? `In the infra repository, generate the app locally:\n.venv/bin/python scripts/service_catalog.py scaffold --service ${s.name}.json --output build/${s.name}\n\nReview and test the result. Publishing requires a separate explicit command described in the generated README.`
        : `Copy ${s.name}.json into catalog/services/, run make catalog, and open a pull request. Use the SDK identity example and namespace allowlists to onboard telemetry. Registration alone does not alter your application.`;
  } catch (e) {
    $("#service-error").textContent = e.message;
  }
});
buildIAM();
buildAdvanced();
setStep(0, false);
