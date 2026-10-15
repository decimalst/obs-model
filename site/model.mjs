// Pure, dependency-free configuration compiler. Shared by the browser and CLI.
import { iamDefaults, validateIAM, compileIAM } from "./iam.mjs";
import {
  policyDefaults,
  validatePolicies,
  compilePolicies,
} from "./policies.mjs";
import {
  operationsDefaults,
  validateOperations,
  compileOperations,
  normalizeQualification,
} from "./operations.mjs";
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
const startupGateDefaults = Object.freeze({
  attempts: 120,
  delaySeconds: 5,
  timeoutSeconds: 20,
});
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
  startupGates: startupGateDefaults,
  existing: null,
  ...policyDefaults,
  ...operationsDefaults,
  ...iamDefaults,
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
  // Older version-1 requests remain usable; never replace an explicitly supplied
  // section or quietly discard new settings during an import.
  r = {
    ...structuredClone(policyDefaults),
    ...structuredClone(operationsDefaults),
    ...structuredClone(iamDefaults),
    startupGates: structuredClone(startupGateDefaults),
    ...r,
  };
  insist(
    r.startupGates &&
      typeof r.startupGates === "object" &&
      !Array.isArray(r.startupGates),
    "Startup gates must be an object",
  );
  insist(
    Object.keys(r.startupGates).every((k) =>
      Object.hasOwn(startupGateDefaults, k),
    ),
    "Unknown startup gate field; use attempts, delaySeconds or timeoutSeconds",
  );
  // Partial tuning imports retain omitted defaults, without mutating the input.
  r.startupGates = { ...startupGateDefaults, ...r.startupGates };
  bounded(r.startupGates.attempts, 1, 360, "Startup attempts");
  bounded(r.startupGates.delaySeconds, 1, 30, "Startup retry delay");
  bounded(r.startupGates.timeoutSeconds, 5, 60, "Startup request timeout");
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
  if (r.mode === "new") {
    insist(
      r.existing === null,
      "New-cluster requests must not contain an existing-cluster contract",
    );
    insist(
      role.test(r.adminRole),
      "Supply the existing role to grant EKS cluster administrator access; this does not select Terraform credentials",
    );
  }
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
    insist(
      e.buckets &&
        !Array.isArray(e.buckets) &&
        Object.keys(e.buckets).every((key) =>
          [
            "mimir-blocks",
            "mimir-ruler",
            "mimir-alertmanager",
            "tempo",
            "flink",
            "pyroscope",
          ].includes(key),
        ),
      "Existing buckets contain unsupported fields; never include credentials",
    );
    insist(
      e.role_arns &&
        !Array.isArray(e.role_arns) &&
        Object.keys(e.role_arns).every((key) =>
          ["mimir", "tempo", "flink", "pyroscope"].includes(key),
        ),
      "Existing roles contain unsupported fields; never include credentials",
    );
    for (const name of [
      "mimir-blocks",
      "mimir-ruler",
      "mimir-alertmanager",
      "tempo",
      ...(Object.hasOwn(e.buckets, "flink") ||
      Object.hasOwn(e.role_arns, "flink")
        ? ["flink"]
        : []),
      ...(r.profiling || Object.hasOwn(e.buckets, "pyroscope")
        ? ["pyroscope"]
        : []),
    ])
      insist(
        /^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$/.test(e.buckets?.[name] || ""),
        `Set existing regional ${name} bucket`,
      );
    for (const name of [
      "mimir",
      "tempo",
      ...(Object.hasOwn(e.buckets, "flink") ||
      Object.hasOwn(e.role_arns, "flink")
        ? ["flink"]
        : []),
      ...(r.profiling || Object.hasOwn(e.role_arns, "pyroscope")
        ? ["pyroscope"]
        : []),
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
  validatePolicies(r);
  validateIAM(r);
  validateOperations(r);
  r.qualification = normalizeQualification(r);
  return r;
}
export function validateExistingCoordinates(e) {
  insist(
    e && typeof e === "object" && !Array.isArray(e),
    "Supply an existing-platform object",
  );
  const r = validateRequest({
    ...structuredClone(defaults),
    mode: "existing",
    name: e.cluster_name,
    region: e.region,
    vpcCIDR: e.vpc_cidr,
    zones: e.availability_zones,
    existing: structuredClone(e),
  });
  return r.existing;
}
export function tuning(r) {
  return {
    startupGates: structuredClone(r.startupGates),
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
    ...compilePolicies(r).tuning,
  };
}
export function compileRequest(input) {
  const r = validateRequest(structuredClone(input)),
    files = {
      "deployment-request.json": json(r),
      "tuning.json": json(tuning(r)),
      ...compilePolicies(r).files,
      ...compileOperations(r),
      ...compileIAM(r),
    };
  if (r.mode === "new") {
    files["main.tf.json"] = json({
      terraform: {
        required_version: ">= 1.10, < 2.0",
        backend: { s3: {} },
        required_providers: {
          aws: { source: "hashicorp/aws", version: "~> 6.0" },
        },
      },
      provider: {
        aws: {
          region: r.region,
          ...(r.iam.terraformRoleArn
            ? { assume_role: { role_arn: r.iam.terraformRoleArn } }
            : {}),
        },
      },
      variable: { quota_redis_auth_token: { type: "string", sensitive: true } },
      module: {
        region: {
          source: "../../terraform/modules/region",
          name: r.name,
          region: r.region,
          vpc_cidr: r.vpcCIDR,
          availability_zones: r.zones,
          admin_principal_arn: r.adminRole,
          existing_role_arns: r.iam.existingWorkloadRoles,
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
      ...(r.securityProfile ? { security_profile: r.securityProfile } : {}),
    });
  files["README.md"] = deploymentGuide(r);
  return files;
}
function deploymentGuide(r) {
  const dir = `environments/${r.name}`;
  return `# ${r.name}: deployment configuration for review

Place these files at ${dir}/ in the infrastructure repository. Nothing has been deployed, published or verified in AWS. Resume the full selection by importing deployment-request.json in the studio. Keep that request private: it contains nonsecret environment coordinates.

## Installation ownership

${r.mode === "new" ? "This bundle generates new regional Terraform. Configure the encrypted regional state backend and obtain the quota password through your approved secret process (TF_VAR_quota_redis_auth_token). Validate locally; real planning/applying remains deferred to the authorized AWS window. Retain the provider lock. After that deployment, export terraform output -json to platform.json. Until actual resource coordinates are available, the installer and cell planner cannot validate this new region." : "This is software-only installation into existing EKS. Cluster, networking, Secrets, buckets, roles and storage-class ownership stays external. Explicit operator booleans select install or reuse. No Terraform infrastructure is generated."}

## Preview the selected configuration

Read IAM.md and IAM-PLAN.json before deployment. They distinguish the Terraform provisioning identity, the existing EKS administrator principal, created infrastructure roles and created/reused workload roles. No role or policy has been applied by this export. The saved Terraform plan is authoritative for final names, policies and replacements. Kubernetes service accounts/RBAC and Grafana Viewer/Editor/Admin roles are separate from AWS IAM.

From the infrastructure repository, after platform.json exists:

    .venv/bin/python scripts/install.py --config ${dir}/platform.json --tuning ${dir}/tuning.json${r.securityProfile ? ` --security-profile ${dir}/security-profile.json` : ""} --quota-image YOUR_REGISTRY/log-quota@sha256:YOUR_PUBLISHED_DIGEST --render-only --output-dir build/${r.name}-review

Use a real published image digest. This command renders locally and does not contact AWS/Kubernetes. It persists the complete selected contract and emits prerequisite/externally owned policy examples. Keep that complete contract for GitOps; gitops_export.py accepts the same --tuning and --security-profile files. During the later live window, --check-only reads real prerequisites. Removing both flags changes the cluster. Follow docs/setup-options.md and docs/runbooks.md.

## Selected policies and owner actions

- Grafana access: ${r.securityProfile ? `${r.securityProfile.access.mode}; authentication: ${r.securityProfile.authentication.mode}; internal transport: ${r.securityProfile.internalTransport.mode}. Use security-profile.json and supply the referenced existing Secrets/ConfigMaps through their owners. OAuth needs a registered exact callback and reviewed group roles. Ingress and mesh controllers are never installed implicitly; generated mesh guidance covers Grafana only.` : "Existing chart defaults; no explicit profile generated. Access/identity setup is still required before organization-wide use."}
- Log privacy: ${r.privacy.logs.mode}. Selected patterns: ${r.privacy.logs.patterns.join(", ")}. These opt-in patterns are lossy and cannot detect every sensitive value. Existing raw/historical data is unchanged.
- Trace privacy: ${r.privacy.traces.mode}. Strict mode drops events and linked spans and retains only reviewed attributes. Span names ${r.privacy.traces.keepSpanName ? "are explicitly retained and may contain sensitive data" : "are removed in strict mode"}. Review docs/privacy-and-service-graphs.md.
- Observed service graph: ${r.serviceGraph.enabled ? "enabled with an explicit bounded service-name list" : "disabled"}. Graphs use client/server spans before tail sampling; incomplete SDK instrumentation produces incomplete graphs. Keep names unique within the cell and drain samplers before membership changes.
- CPU profiling: ${r.profiling ? "enabled; privileged node collection requires reviewed namespaces/workload labels and kernel/storage acceptance" : "disabled"}.
- Hubble: ${r.hubble ? "enabled using externally managed Cilium; qualify networking ownership first" : "disabled"}.
- Startup dependencies: ${r.startupGates.attempts} attempts, ${r.startupGates.delaySeconds}-second retry delay and ${r.startupGates.timeoutSeconds}-second request timeout per init-container invocation. Gateways wait for Kafka topic/ISR readiness, writers for all-replica ClickHouse schema, and quota admission for the active gateway. Exhausted checks fail closed; Kubernetes may restart the init container. These are not a total deployment deadline or proof of stored-signal delivery. Keep the supported installer/GitOps hook ordering; see docs/startup-gates.md.

${r.isolation.enabled || r.qualification ? "Read OPERATIONS.md for the selected cell/recovery plan, required owner inputs and deferred qualification commands." : "Cell plans and qualification drafts were not selected. Use docs/isolation.md and docs/live-qualification.md to prepare them later."}

All actual AWS checks belong in the deferred 26-gate qualification batch. A draft with placeholder hashes is not acceptance evidence. Full internal TLS and authenticated tenant intake still require separately qualified integrations.

Topology changes require migration review. Do not shrink shards/partitions from a downloaded replacement. Log byte budgets remain independent of event rates. No dollar forecast or raw-data cross-region replication is generated, even when a remote recovery target is planned. Read docs/ebpf.md and docs/configuration-studio.md before rollout.
`;
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
