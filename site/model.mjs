// Pure, dependency-free configuration compiler. Shared by the browser and CLI.
export const operators = [
  "cert-manager",
  "argo-rollouts",
  "strimzi-kafka-operator",
  "opentelemetry-operator",
  "altinity-clickhouse-operator",
  "cluster-autoscaler",
  "metrics-server",
  "keda",
];
export const defaults = Object.freeze({
  version: 1,
  mode: "new",
  name: "olly-production",
  region: "us-east-1",
  zones: [],
  vpcCIDR: "10.40.0.0/16",
  adminRole: "",
  owner: "platform",
  environment: "production",
  costCenter: "platform",
  networkProvider: "aws",
  hubble: false,
  profiling: false,
  profileNamespaces: ["applications"],
  sampleRate: 19,
  logEventsPerSecond: 1000,
  traceBaselinePercent: 10,
  logPartitions: 36,
  writerMax: 12,
  clickhouseShards: 2,
  existing: null,
});
const json = (value) => JSON.stringify(value, null, 2) + "\n";
function insist(test, message) {
  if (!test) throw new Error(message);
}
function bounded(value, min, max, label) {
  insist(
    Number.isInteger(value) && value >= min && value <= max,
    `${label} must be an integer from ${min} to ${max}`,
  );
}
const slug = /^[a-z][a-z0-9-]{1,22}[a-z0-9]$/;
const role = /^arn:aws:iam::\d{12}:role\/[A-Za-z0-9+=,.@_/-]+$/;
export function existingOptions(e) {
  const result = {};
  for (const key of [
    "cluster_arn",
    "operator_namespaces",
    "grafana_db_name",
    "grafana_db_user",
  ])
    if (Object.hasOwn(e, key)) result[key] = structuredClone(e[key]);
  for (const key of ["grafana_db_name", "grafana_db_user"])
    if (Object.hasOwn(result, key))
      insist(
        typeof result[key] === "string" &&
          /^[A-Za-z_][A-Za-z0-9_]{0,62}$/.test(result[key]),
        `Set a valid ${key}`,
      );
  if (Object.hasOwn(result, "operator_namespaces"))
    insist(
      Array.isArray(result.operator_namespaces) &&
        result.operator_namespaces.length > 0 &&
        result.operator_namespaces.every(
          (n) =>
            typeof n === "string" &&
            n.length <= 63 &&
            /^[a-z0-9]([-a-z0-9]*[a-z0-9])?$/.test(n),
        ),
      "Set valid operator namespaces",
    );
  if (Object.hasOwn(result, "cluster_arn"))
    insist(
      typeof result.cluster_arn === "string" &&
        new RegExp(
          "^arn:aws:eks:" +
            e.region +
            ":\\d{12}:cluster/" +
            e.cluster_name +
            "$",
        ).test(result.cluster_arn),
      "Cluster ARN must match the selected region and cluster",
    );
  return result;
}
export function validateRequest(r) {
  insist(
    r && typeof r === "object" && !Array.isArray(r),
    "Configuration must be an object",
  );
  insist(
    Object.keys(r).every((k) => Object.hasOwn(defaults, k)),
    "Unknown configuration field; never include credentials",
  );
  insist(r.version === 1, "Unsupported configuration version");
  insist(["new", "existing"].includes(r.mode), "Choose new or existing EKS");
  insist(
    slug.test(r.name),
    "Name must be 3–24 lowercase letters, digits or hyphens",
  );
  insist(/^[a-z]{2}-[a-z]+-\d$/.test(r.region), "Use a commercial AWS region");
  insist(
    Array.isArray(r.zones) &&
      r.zones.length === 3 &&
      new Set(r.zones).size === 3 &&
      r.zones.every((z) => new RegExp("^" + r.region + "[a-z]$").test(z)),
    "Enter three distinct AZ names actually available in this account and region",
  );
  const cidr = /^(\d+)\.(\d+)\.(\d+)\.(\d+)\/(\d+)$/.exec(r.vpcCIDR);
  insist(
    cidr &&
      cidr.slice(1, 5).every((n) => +n <= 255) &&
      +cidr[5] >= 16 &&
      +cidr[5] <= 24,
    "Use a VPC CIDR between /16 and /24",
  );
  const address = cidr
      .slice(1, 5)
      .reduce((a, n) => (a * 256 + Number(n)) >>> 0, 0),
    mask = (0xffffffff << (32 - Number(cidr[5]))) >>> 0;
  insist(
    (address & mask) >>> 0 === address,
    "VPC CIDR must use its network address",
  );
  for (const key of ["owner", "environment", "costCenter"])
    insist(
      typeof r[key] === "string" && /^[A-Za-z0-9 _.-]{1,80}$/.test(r[key]),
      `Set ${key} using plain text`,
    );
  for (const key of ["profiling", "hubble"])
    insist(typeof r[key] === "boolean", `${key} must be boolean`);
  insist(
    ["aws", "cilium"].includes(r.networkProvider),
    "Select a network-policy provider",
  );
  insist(
    !r.hubble || r.networkProvider === "cilium",
    "Hubble requires existing, qualified Cilium",
  );
  insist(
    r.mode !== "new" || r.networkProvider === "aws",
    "New EKS starts with AWS networking; migrate separately before enabling Cilium",
  );
  insist(
    Array.isArray(r.profileNamespaces) &&
      r.profileNamespaces.length > 0 &&
      r.profileNamespaces.length <= 100 &&
      new Set(r.profileNamespaces).size === r.profileNamespaces.length &&
      r.profileNamespaces.every(
        (n) => /^[a-z0-9]([-a-z0-9]*[a-z0-9])?$/.test(n) && n.length <= 63,
      ),
    "Choose valid, unique profiling namespaces",
  );
  bounded(r.sampleRate, 1, 99, "CPU sample rate");
  bounded(r.logEventsPerSecond, 1, 100000, "Log event rate");
  bounded(r.traceBaselinePercent, 0, 100, "Baseline trace percentage");
  bounded(r.logPartitions, 3, 120, "Kafka partitions");
  bounded(r.writerMax, 3, r.logPartitions, "Consumer maximum");
  bounded(r.clickhouseShards, 2, 32, "ClickHouse shards");
  if (r.mode === "new")
    insist(role.test(r.adminRole), "Supply the administrator IAM role ARN");
  if (r.mode === "existing") {
    const e = r.existing;
    insist(
      e && typeof e === "object" && !Array.isArray(e),
      "Supply the nonsecret existing-platform contract",
    );
    const allowed = [
      "region",
      "cluster_name",
      "cluster_arn",
      "vpc_cidr",
      "availability_zones",
      "quota_redis_host",
      "buckets",
      "role_arns",
      "secret_source",
      "storage_class",
      "create_storage_class",
      "operators",
      "operator_namespaces",
      "grafana_db_name",
      "grafana_db_user",
    ];
    insist(
      Object.keys(e).every((k) => allowed.includes(k)),
      "Existing contract contains unsupported fields; use existing Kubernetes Secrets and omit credentials",
    );
    insist(
      e.region === r.region &&
        e.cluster_name === r.name &&
        e.vpc_cidr === r.vpcCIDR &&
        JSON.stringify(e.availability_zones) === JSON.stringify(r.zones),
      "Existing contract identity, region, VPC and AZs must match the selections",
    );
    existingOptions(e);
    insist(
      e.secret_source === "existing",
      "The browser existing-cluster profile uses existing Kubernetes Secrets",
    );
    insist(
      typeof e.quota_redis_host === "string" &&
        /^[a-zA-Z0-9.-]+$/.test(e.quota_redis_host),
      "Set the existing regional quota Redis host",
    );
    insist(
      typeof e.storage_class === "string" &&
        /^[a-z0-9][a-z0-9.-]*$/.test(e.storage_class) &&
        e.create_storage_class === false,
      "Explicitly reuse a retained, encrypted storage class",
    );
    for (const name of [
      "mimir-blocks",
      "mimir-ruler",
      "mimir-alertmanager",
      "tempo",
      ...(r.profiling ? ["pyroscope"] : []),
    ])
      insist(
        /^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$/.test(e.buckets?.[name] || ""),
        `Set existing regional ${name} bucket`,
      );
    for (const name of [
      "mimir",
      "tempo",
      ...(r.profiling ? ["pyroscope"] : []),
    ])
      insist(
        role.test(e.role_arns?.[name] || ""),
        `Set ${name} Pod Identity role ARN`,
      );
    insist(
      e.operators &&
        Object.keys(e.operators).length === operators.length &&
        operators.every((k) => typeof e.operators[k] === "boolean"),
      "Explicitly select install/reuse for every operator",
    );
  }
  return r;
}
export function tuning(r) {
  return {
    quota: {
      eventsPerSecond: r.logEventsPerSecond,
      eventBurst: r.logEventsPerSecond * 2,
      debugEventsPerSecond: Math.min(100, r.logEventsPerSecond),
      debugEventBurst: Math.min(200, r.logEventsPerSecond * 2),
    },
    collectors: { traceSamplingPercent: r.traceBaselinePercent },
    scaling: { logPartitions: r.logPartitions },
    writerAutoscaling: { maxReplicas: r.writerMax },
    clickhouse: { shards: r.clickhouseShards },
    hubble: { enabled: r.hubble },
    profiling: { sampleRate: r.sampleRate, namespaces: r.profileNamespaces },
  };
}
export function compileRequest(input) {
  const r = validateRequest(structuredClone(input)),
    files = {
      "deployment-request.json": json(r),
      "tuning.json": json(tuning(r)),
    };
  if (r.mode === "new") {
    files["main.tf.json"] = json({
      terraform: {
        required_version: ">= 1.10, < 2.0",
        backend: { s3: {} },
        required_providers: {
          aws: { source: "hashicorp/aws", version: "6.66.0" },
        },
      },
      provider: { aws: { region: r.region } },
      variable: { quota_redis_auth_token: { type: "string", sensitive: true } },
      module: {
        region: {
          source: "../../terraform/modules/region",
          name: r.name,
          region: r.region,
          vpc_cidr: r.vpcCIDR,
          availability_zones: r.zones,
          admin_principal_arn: r.adminRole,
          quota_redis_auth_token: "${var.quota_redis_auth_token}",
          enable_profiling: r.profiling,
          tags: {
            Owner: r.owner,
            Environment: r.environment,
            CostCenter: r.costCenter,
          },
        },
      },
      output: { platform: { value: "${module.region}" } },
    });
  } else
    files["platform.json"] = json({
      ...r.existing,
      network_policy_provider: r.networkProvider,
      profiling: { enabled: r.profiling },
      tuning: tuning(r),
    });
  files["README.md"] =
    `# ${r.name}: reviewed deployment configuration\n\nPlace these files at environments/${r.name}/ in the infrastructure repository. No cloud or repository changes have been made.\n\n${r.mode === "new" ? "Configure the encrypted regional S3 Terraform backend and obtain a quota store password through your approved secret process (TF_VAR_quota_redis_auth_token). Initialize Terraform, retain its lock file, review a saved plan, and apply only through your authorized deployment process. Export terraform output -json to platform.json afterward." : "All cluster, networking, secrets, buckets, roles and storage-class ownership remains external. Explicit operator booleans select install or reuse."}\n\nRun the installer with --config environments/${r.name}/platform.json --tuning environments/${r.name}/tuning.json --quota-image YOUR_PUBLISHED_DIGEST --render-only. This validates and renders locally without AWS or Kubernetes calls. Add --security-profile for a reviewed existing-secret/OIDC/ingress configuration. Follow docs/setup-options.md for privacy, service graphs, isolated-cell budgets and the deferred AWS qualification batch. Later use --check-only for live read-only preflight, then the documented installation or GitOps export workflow. Read docs/configuration-studio.md, docs/ebpf.md and docs/runbooks.md before rollout.\n\n${r.profiling ? "Profiling is opt-in and adds privileged node agents plus a regional multi-component backend. Application namespaces must exist. Select only reviewed workloads using observability/profile=true. Qualify kernel, Pod Identity, persistent storage, AZ failure and profile ingestion before production." : "CPU profiling is disabled."}\n\n${r.hubble ? "Cilium remains owned by the cluster networking release. Qualify chaining/policy migration and Hubble metrics before preflight." : "Hubble is disabled."}\n\nTopology changes to an existing installation require migration review: these files are desired configuration, never a safe-shrink procedure. No cross-region raw-data replication is configured.\n`;
  return files;
}
export function validateService(s) {
  const keys = [
    "apiVersion",
    "name",
    "namespace",
    "owner",
    "description",
    "repository",
    "runbook",
    "lifecycle",
    "profiling",
    "dependencies",
    "slo",
  ];
  insist(
    s && typeof s === "object" && Object.keys(s).every((k) => keys.includes(k)),
    "Unknown service field",
  );
  insist(s.apiVersion === "observability/v1", "Unsupported service version");
  for (const k of ["name", "namespace", "owner"])
    insist(
      /^[a-z][a-z0-9-]{1,61}[a-z0-9]$/.test(s[k] || ""),
      `Set a valid ${k}`,
    );
  insist(
    typeof s.description === "string" &&
      s.description.length > 0 &&
      s.description.length <= 400,
    "Description must be 1–400 characters",
  );
  for (const k of ["repository", "runbook"])
    insist(
      typeof s[k] === "string" &&
        s[k].length <= 500 &&
        /^https:\/\/[A-Za-z0-9.-]+(?::[0-9]+)?(?:\/[^\s<>]*)?$/.test(s[k]),
      `Set an HTTPS ${k} URL`,
    );
  insist(
    ["experimental", "production", "deprecated"].includes(s.lifecycle),
    "Choose lifecycle",
  );
  insist(typeof s.profiling === "boolean", "Profiling must be boolean");
  insist(
    s.slo &&
      Object.keys(s.slo).every((k) =>
        ["enabled", "target", "source"].includes(k),
      ) &&
      typeof s.slo.enabled === "boolean" &&
      Number.isFinite(s.slo.target) &&
      s.slo.target >= 90 &&
      s.slo.target <= 99.99 &&
      ["application", "observed-spans"].includes(s.slo.source),
    "Use an availability target from 90 to 99.99 and a supported metric source",
  );
  if (s.dependencies !== undefined)
    insist(
      Array.isArray(s.dependencies) &&
        s.dependencies.length <= 50 &&
        new Set(s.dependencies).size === s.dependencies.length &&
        s.dependencies.every(
          (d) =>
            typeof d === "string" &&
            /^[a-z][a-z0-9-]+\/[a-z][a-z0-9-]+$/.test(d) &&
            d !== `${s.namespace}/${s.name}`,
        ),
      "Dependencies must be unique namespace/service references",
    );
  return s;
}
// Uncompressed ZIP: filenames are generated internally; no executable input or network upload.
export function zipFiles(files) {
  const enc = new TextEncoder(),
    parts = [],
    directory = [];
  let offset = 0;
  const crc = (bytes) => {
    let v = 0xffffffff;
    for (const b of bytes) {
      v ^= b;
      for (let i = 0; i < 8; i++) v = (v >>> 1) ^ (v & 1 ? 0xedb88320 : 0);
    }
    return (v ^ 0xffffffff) >>> 0;
  };
  for (const [name, content] of Object.entries(files)) {
    const n = enc.encode(name),
      data = enc.encode(content),
      sum = crc(data),
      local = new Uint8Array(30 + n.length),
      lv = new DataView(local.buffer);
    lv.setUint32(0, 0x04034b50, true);
    lv.setUint16(4, 20, true);
    lv.setUint32(14, sum, true);
    lv.setUint32(18, data.length, true);
    lv.setUint32(22, data.length, true);
    lv.setUint16(26, n.length, true);
    local.set(n, 30);
    const central = new Uint8Array(46 + n.length),
      cv = new DataView(central.buffer);
    cv.setUint32(0, 0x02014b50, true);
    cv.setUint16(4, 20, true);
    cv.setUint16(6, 20, true);
    cv.setUint32(16, sum, true);
    cv.setUint32(20, data.length, true);
    cv.setUint32(24, data.length, true);
    cv.setUint16(28, n.length, true);
    cv.setUint32(42, offset, true);
    central.set(n, 46);
    parts.push(local, data);
    directory.push(central);
    offset += local.length + data.length;
  }
  const size = directory.reduce((n, b) => n + b.length, 0),
    end = new Uint8Array(22),
    ev = new DataView(end.buffer);
  ev.setUint32(0, 0x06054b50, true);
  ev.setUint16(8, directory.length, true);
  ev.setUint16(10, directory.length, true);
  ev.setUint32(12, size, true);
  ev.setUint32(16, offset, true);
  const result = new Uint8Array(offset + size + 22);
  let p = 0;
  for (const part of [...parts, ...directory, end]) {
    result.set(part, p);
    p += part.length;
  }
  return result;
}
