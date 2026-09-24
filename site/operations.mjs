// Offline setup documents only. No cloud, Kubernetes, filesystem or network access.
export const operationsDefaults = Object.freeze({
  isolation: {
    enabled: false,
    mode: "shared",
    id: "regional-shared",
    services: [],
    grafanaDatabaseIdentity: "",
    additionalCells: [],
  },
  qualification: null,
});
const json = (value) => JSON.stringify(value, null, 2) + "\n";
const reference = /^[a-zA-Z0-9][a-zA-Z0-9_.:/-]*$/;
const region = /^[a-z]{2}(-gov)?-[a-z]+-[0-9]+$/;
const cluster = /^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$/;
const account = /^[0-9]{12}$/;
const cellId = /^[a-z][a-z0-9-]{1,62}$/;
const service = /^[a-z][a-z0-9-]{1,62}\/[a-z][a-z0-9-]{1,62}$/;
const database = /^[a-zA-Z0-9.-]+\/[a-zA-Z0-9_-]+$/;
const zeroDigest = "0".repeat(64);
const currentPlatformFile = (r) =>
  r.mode === "new" ? "platform.reviewed.json" : "platform.json";
function insist(condition, message) {
  if (!condition) throw new Error(message);
}
function fields(value, required, optional, label) {
  insist(
    value && typeof value === "object" && !Array.isArray(value),
    `${label} must be an object`,
  );
  insist(
    Object.keys(value).every((key) =>
      [...required, ...optional].includes(key),
    ) && required.every((key) => Object.hasOwn(value, key)),
    `${label} contains missing or unsupported fields; never include credentials`,
  );
}
function text(value, expression, label, max = 200) {
  insist(
    typeof value === "string" &&
      value.length > 0 &&
      value.length <= max &&
      expression.test(value),
    `Set a valid nonsecret ${label}`,
  );
}
function optionalReference(value, label) {
  if (value !== null) text(value, reference, label);
}
function minutes(value, label) {
  insist(
    value === null ||
      (Number.isInteger(value) && value >= 0 && value <= 525600),
    `${label} must be null or an integer from 0 to 525600 minutes`,
  );
}
function identity(value, label, source = false) {
  fields(
    value,
    [
      "accountId",
      "region",
      "cluster",
      ...(source ? ["mode", "context", "namespace"] : []),
    ],
    [],
    label,
  );
  text(value.accountId, account, `${label} account ID`);
  text(value.region, region, `${label} region`);
  text(value.cluster, cluster, `${label} cluster`);
  if (source) {
    insist(
      ["managed", "existing", "software-only"].includes(value.mode),
      "Choose a qualification installation mode",
    );
    text(value.context, reference, "qualification context");
    insist(
      value.namespace === "observability",
      "Qualification namespace must be observability",
    );
  }
}
function localPlatformPath(value) {
  text(
    value,
    /^[A-Za-z0-9_./-]+\.(json|yaml|yml)$/,
    "local platform file",
    500,
  );
  insist(
    !value.startsWith("/") && !value.includes("//"),
    "Platform references must be relative local files",
  );
  const parts = [];
  for (const part of value.split("/")) {
    if (part === ".") continue;
    if (part === ".." && parts.length && parts.at(-1) !== "..") parts.pop();
    else parts.push(part);
  }
  return parts.join("/");
}
function services(value, required) {
  insist(
    Array.isArray(value) &&
      value.length >= (required ? 1 : 0) &&
      value.length <= 1000 &&
      new Set(value).size === value.length &&
      value.every((entry) => typeof entry === "string" && service.test(entry)),
    "Choose unique namespace/service assignments (1–1000 for an enabled cell)",
  );
}
function selectedClusterAccount(r) {
  const arn = r.existing?.cluster_arn;
  if (typeof arn !== "string") return null;
  const match = /^arn:aws:eks:([^:]+):(\d{12}):cluster\/(.+)$/.exec(arn);
  insist(
    match && match[1] === r.region && match[3] === r.name,
    "Cluster ARN must match the selected region and cluster",
  );
  return match[2];
}
function isolationConfig(r) {
  insist(["new", "existing"].includes(r.mode), "Choose new or existing EKS");
  text(r.name, /^[a-z][a-z0-9-]{1,22}[a-z0-9]$/, "deployment name");
  text(r.region, region, "deployment region");
  const value =
    r.isolation === undefined ? operationsDefaults.isolation : r.isolation;
  fields(
    value,
    [
      "enabled",
      "mode",
      "id",
      "services",
      "grafanaDatabaseIdentity",
      "additionalCells",
    ],
    [],
    "Isolation options",
  );
  insist(
    typeof value.enabled === "boolean",
    "Isolation export must be boolean",
  );
  insist(
    ["shared", "isolated-clusters"].includes(value.mode),
    "Choose shared or isolated-clusters planning",
  );
  text(value.id, cellId, "cell ID");
  services(value.services, value.enabled);
  insist(
    typeof value.grafanaDatabaseIdentity === "string",
    "Database identity must be a nonsecret string",
  );
  if (value.grafanaDatabaseIdentity)
    text(
      value.grafanaDatabaseIdentity,
      database,
      "Grafana host/database identity",
      500,
    );
  insist(
    Array.isArray(value.additionalCells) && value.additionalCells.length <= 99,
    "Use at most 99 additional cell references",
  );
  const ids = new Set([value.id]);
  // Both names represent this deployment in new mode; the raw Terraform export
  // must never be mistaken for an additional independently owned cell.
  const paths = new Set([currentPlatformFile(r), "platform.json"]);
  for (const cell of value.additionalCells) {
    fields(
      cell,
      ["id", "platform", "services"],
      ["grafanaDatabaseIdentity"],
      "Additional cell",
    );
    text(cell.id, cellId, "additional cell ID");
    insist(!ids.has(cell.id), "Cell IDs must be unique");
    ids.add(cell.id);
    const path = localPlatformPath(cell.platform);
    insist(
      !paths.has(path),
      "Each cell must reference a separate local platform file",
    );
    paths.add(path);
    services(cell.services, true);
    if (Object.hasOwn(cell, "grafanaDatabaseIdentity"))
      text(
        cell.grafanaDatabaseIdentity,
        database,
        "additional Grafana host/database identity",
        500,
      );
  }
  if (value.enabled) {
    insist(
      value.mode !== "shared" || value.additionalCells.length === 0,
      "Shared mode describes exactly one cell",
    );
    if (value.mode === "isolated-clusters" && r.mode === "existing") {
      insist(
        selectedClusterAccount(r),
        "Isolated existing cells require the exact cluster ARN in the platform contract",
      );
      insist(
        value.grafanaDatabaseIdentity,
        "Isolated existing cells require the Grafana host/database identity",
      );
    }
  }
  return value;
}
export function createQualification(r) {
  const selectedAccount =
    selectedClusterAccount(r) ??
    (r.mode === "new"
      ? /^arn:aws:iam::(\d{12}):role\//.exec(r.iam?.terraformRoleArn || "")?.[1]
      : null) ??
    "000000000000";
  return {
    version: 1,
    runId: `${r.name}-qualification-001`,
    sourceRevision: "0".repeat(40),
    deploymentArtifacts: {
      platformContractSHA256: zeroDigest,
      renderedValuesSHA256: zeroDigest,
      terraformPlanSHA256: r.mode === "new" ? zeroDigest : null,
    },
    environment: {
      accountId: selectedAccount,
      region: r.region,
      cluster: r.name,
      context: r.name,
      namespace: "observability",
      mode: r.mode === "new" ? "managed" : "software-only",
    },
    features: {
      hubble: r.hubble,
      profiling: r.profiling,
      flink: false,
      gitops: false,
      serviceGraphs: r.serviceGraph?.enabled ?? false,
      externalNotificationDelivery: false,
    },
    recovery: {
      backupMode: "native",
      owner: reference.test(r.owner || "") ? r.owner : "platform-operations",
      providerRef: null,
      rpoMinutes: null,
      rtoMinutes: null,
      destination: {
        accountId: selectedAccount,
        region: r.region,
        cluster: `${r.name}-restore`,
      },
      crossRegion: {
        enabled: false,
        rpoMinutes: null,
        rtoMinutes: null,
        costAssessmentRef: null,
      },
    },
  };
}
function qualificationConfig(r) {
  if (r.qualification === null || r.qualification === undefined) return null;
  const value = structuredClone(r.qualification);
  fields(
    value,
    [
      "version",
      "runId",
      "sourceRevision",
      "deploymentArtifacts",
      "environment",
      "features",
      "recovery",
    ],
    [],
    "Qualification options",
  );
  insist(value.version === 1, "Unsupported qualification version");
  text(value.runId, reference, "qualification run ID");
  text(value.sourceRevision, /^[a-f0-9]{40}$/, "reviewed source revision");
  identity(value.environment, "qualification source", true);
  fields(
    value.deploymentArtifacts,
    ["platformContractSHA256", "renderedValuesSHA256", "terraformPlanSHA256"],
    [],
    "Deployment artifact hashes",
  );
  for (const key of ["platformContractSHA256", "renderedValuesSHA256"])
    text(value.deploymentArtifacts[key], /^[a-f0-9]{64}$/, key);
  if (value.deploymentArtifacts.terraformPlanSHA256 !== null)
    text(
      value.deploymentArtifacts.terraformPlanSHA256,
      /^[a-f0-9]{64}$/,
      "Terraform plan hash",
    );
  fields(
    value.features,
    [
      "hubble",
      "profiling",
      "flink",
      "gitops",
      "serviceGraphs",
      "externalNotificationDelivery",
    ],
    [],
    "Qualification features",
  );
  insist(
    Object.values(value.features).every(
      (enabled) => typeof enabled === "boolean",
    ),
    "Qualification features must be boolean",
  );
  fields(
    value.recovery,
    [
      "backupMode",
      "owner",
      "providerRef",
      "rpoMinutes",
      "rtoMinutes",
      "destination",
      "crossRegion",
    ],
    [],
    "Recovery options",
  );
  insist(
    ["native", "external"].includes(value.recovery.backupMode),
    "Choose native or external backups",
  );
  text(value.recovery.owner, reference, "recovery owner");
  optionalReference(
    value.recovery.providerRef,
    "backup provider runbook reference",
  );
  minutes(value.recovery.rpoMinutes, "Recovery RPO");
  minutes(value.recovery.rtoMinutes, "Recovery RTO");
  identity(value.recovery.destination, "restore destination");
  const cross = value.recovery.crossRegion;
  fields(
    cross,
    ["enabled", "rpoMinutes", "rtoMinutes", "costAssessmentRef"],
    [],
    "Cross-region recovery",
  );
  insist(
    typeof cross.enabled === "boolean",
    "Cross-region opt-in must be boolean",
  );
  minutes(cross.rpoMinutes, "Cross-region RPO");
  minutes(cross.rtoMinutes, "Cross-region RTO");
  optionalReference(
    cross.costAssessmentRef,
    "cross-region cost assessment reference",
  );
  // Installation facts always come from the selected deployment, not stale draft controls.
  Object.assign(value.environment, {
    region: r.region,
    cluster: r.name,
    mode: r.mode === "new" ? "managed" : "software-only",
  });
  Object.assign(value.features, {
    hubble: r.hubble,
    profiling: r.profiling,
    serviceGraphs: r.serviceGraph?.enabled ?? false,
  });
  identity(value.environment, "qualification source", true);
  insist(
    Object.values(value.features).every(
      (enabled) => typeof enabled === "boolean",
    ),
    "Selected deployment features must be boolean",
  );
  if (r.mode === "existing") {
    const selectedAccount = selectedClusterAccount(r);
    insist(
      !selectedAccount || value.environment.accountId === selectedAccount,
      "Qualification account must match the selected cluster ARN",
    );
  } else if (r.iam?.terraformRoleArn) {
    insist(
      value.environment.accountId === r.iam.terraformRoleArn.split(":")[4],
      "Qualification account must match the Terraform assumed-role account",
    );
  }
  const source = value.environment,
    destination = value.recovery.destination;
  insist(
    !["accountId", "region", "cluster"].every(
      (key) => source[key] === destination[key],
    ),
    "Restore destination must be a different isolated cluster",
  );
  insist(
    source.region === destination.region || cross.enabled,
    "A destination in another region requires explicit cross-region opt-in",
  );
  insist(
    !cross.enabled || source.region !== destination.region,
    "Cross-region recovery requires a destination in a different region",
  );
  return value;
}
export function normalizeQualification(r) {
  return qualificationConfig(r);
}
export function validateOperations(r) {
  isolationConfig(r);
  qualificationConfig(r);
  return r;
}
export function operationsGuide(r) {
  const isolation = isolationConfig(r),
    qualification = qualificationConfig(r);
  const sections = [
    "# Offline operations setup",
    "These files describe review inputs, not deployments, backup jobs, replication, capacity measurements or completed tests. No AWS, Kubernetes or external receiver request was made. Keep configuration and evidence private; never paste credentials or secret-bearing plans into the studio. See docs/setup-options.md, docs/isolation.md, docs/recovery.md and docs/live-qualification.md.",
  ];
  if (isolation.enabled)
    sections.push(
      `## Cell and budget review: ${isolation.mode}`,
      `cells.json references this deployment's ${currentPlatformFile(r)} and any separately reviewed local platform files. Paths are relative to cells.json. Only this deployment is generated; additional references do not provision other cells or create service routing. Each platform supports one installation per cluster.`,
      r.mode === "new"
        ? `The current platform.reviewed.json does not exist yet. After the account owner creates the reviewed infrastructure, export the Terraform platform output to platform.json and keep that raw output. Run the README's installer render-only command with the selected tuning and output directory build/${r.name}-review, then copy its normalized contract as shown below. This reviewed contract contains the selected budgets; the raw Terraform output does not. Verify the exact cluster ARN and Grafana database identity before running the planner. An isolated-cell plan remains unavailable until every referenced contract has real coordinates.\n\n\`\`\`sh\ncp build/${r.name}-review/platform.json environments/${r.name}/platform.reviewed.json\n\`\`\``
        : "Before planning, ensure every referenced platform contract includes its selected tuning. For isolated cells, retain exact cluster ARNs and externally confirmed Grafana host/database identities. Other cells' contracts, resource independence and regional service assignments are checked locally by the planner; the browser cannot read them.",
      "```sh\n.venv/bin/python scripts/cell_plan.py --config environments/" +
        r.name +
        "/cells.json --output build/" +
        r.name +
        "-cell-plan.json\n```",
      "Use a new output filename for each review. Configured ceilings distinguish namespace/service log quotas, tenant backend budgets and per-pod sampler limits. They do not prove measured throughput, dollar costs, fair scheduling or network/IAM isolation. The planner rejects reused declared resources; live qualification remains pending. Separate clusters and backends cost more and require independently reviewed state, buckets, roles, quota endpoints and database capacity.",
    );
  if (qualification)
    sections.push(
      "## Recovery and deferred AWS acceptance",
      `qualification.json selects ${qualification.recovery.backupMode} backups and ${qualification.environment.mode} installation ownership. The Hubble, profiling and service-graph gates follow the actual deployment selections. Flink, GitOps and notification flags select future acceptance scope only; they do not install, publish, reconcile or send anything.`,
      "The source account comes from the existing cluster ARN or configured Terraform assumed-role ARN. Without either it is the unresolved 000000000000 placeholder; replace it with the deployment account before qualification. An administrator role may belong to another account and is never used to infer this value. Confirm the exact context, independent restore destination, recovery owner and a unique run ID. A different cluster name alone does not prove independent storage or IAM.",
      "Zero revision/artifact hashes are deliberate pending placeholders. Replace them with the full reviewed commit and SHA-256 hashes of the exact private platform contract and rendered values; managed installations also require the saved reviewed Terraform plan hash. Software-only installations may retain a null Terraform plan hash. Keep the actual plan/state and secret-bearing values out of downloads and evidence. No draft can pass identity acceptance with zero hashes or revision.",
      "Set reviewed RPO/RTO targets before recording recovery success. External backups require the provider's nonsecret runbook reference; neither backup mode installs a backup system. Cross-region recovery requires explicit opt-in, a different region, reviewed RPO/RTO and a cost assessment before that gate can pass. Selecting it does not configure replication or move telemetry.",
      "```sh\n.venv/bin/python scripts/qualification.py --config environments/" +
        r.name +
        "/qualification.json --check-only\n```",
      "This validates the draft and prints the single deferred gate catalog locally. No live tests run. Later move the reviewed file into a private evidence directory, initialize a unique run using the documented qualification workflow, and record receipts only after the account owner performs each authorized gate. Required unexecuted gates stay pending. Evidence is operator-attested and tied to the exact run/configuration/revision, not independently verified.",
    );
  return sections.join("\n\n") + "\n";
}
export function compileOperations(r) {
  const isolation = isolationConfig(r),
    qualification = qualificationConfig(r),
    files = {};
  if (isolation.enabled) {
    const current = {
      id: isolation.id,
      platform: currentPlatformFile(r),
      services: isolation.services,
    };
    if (isolation.grafanaDatabaseIdentity)
      current.grafanaDatabaseIdentity = isolation.grafanaDatabaseIdentity;
    files["cells.json"] = json({
      version: 1,
      mode: isolation.mode,
      cells: [current, ...isolation.additionalCells],
    });
  }
  if (qualification) files["qualification.json"] = json(qualification);
  if (isolation.enabled || qualification)
    files["OPERATIONS.md"] = operationsGuide(r);
  return files;
}
