import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import {
  defaults,
  compileRequest,
  validateRequest,
  validateService,
  operators,
  zipFiles,
  existingOptions,
  validateExistingCoordinates,
} from "../site/model.mjs";
const good = () => ({
  ...structuredClone(defaults),
  zones: ["us-east-1a", "us-east-1b", "us-east-1c"],
  adminRole: "arn:aws:iam::123456789012:role/Admin",
});
test("new cluster bundle keeps secrets out and preserves regional topology", () => {
  const r = good();
  r.profiling = true;
  const f = compileRequest(r),
    tf = JSON.parse(f["main.tf.json"]);
  assert.equal(tf.module.region.enable_profiling, true);
  assert.equal(
    tf.module.region.quota_redis_auth_token,
    "${var.quota_redis_auth_token}",
  );
  assert.deepEqual(tf.module.region.availability_zones, r.zones);
  assert.deepEqual(tf.module.region.existing_role_arns, {});
  assert.equal(tf.provider.aws.assume_role, undefined);
  assert.equal(
    JSON.parse(f["tuning.json"]).collectors.traceSamplingPercent,
    10,
  );
  assert.equal(f["platform.json"], undefined);
  mkdirSync("build/studio-new", { recursive: true });
  writeFileSync("build/studio-new/main.tf.json", f["main.tf.json"]);
});
test("IAM selections compile to actual Terraform inputs and a matching review without credential inference", () => {
  const r = good();
  r.adminRole = "arn:aws:iam::999999999999:role/ExternalAdministrator";
  r.profiling = true;
  r.iam = {
    terraformRoleArn: "arn:aws:iam::123456789012:role/Provisioner",
    existingWorkloadRoles: Object.fromEntries(
      ["mimir", "tempo", "flink", "pyroscope"].map((id) => [
        id,
        `arn:aws:iam::123456789012:role/existing-${id}`,
      ]),
    ),
  };
  const files = compileRequest(r),
    tf = JSON.parse(files["main.tf.json"]),
    plan = JSON.parse(files["IAM-PLAN.json"]);
  assert.deepEqual(tf.provider.aws.assume_role, {
    role_arn: r.iam.terraformRoleArn,
  });
  assert.deepEqual(
    tf.module.region.existing_role_arns,
    r.iam.existingWorkloadRoles,
  );
  assert.equal(tf.module.region.admin_principal_arn, r.adminRole);
  assert.equal(plan.accountId, "123456789012");
  assert.equal(plan.summary.createdRoles, 4);
  assert.equal(plan.summary.reusedRoles, 4);
  assert.equal(plan.summary.createdAssociations, 6);
  assert.match(files["IAM.md"], /state backend/);
  assert.deepEqual(
    compileRequest(JSON.parse(files["deployment-request.json"])),
    files,
  );
  mkdirSync("build/studio-iam", { recursive: true });
  writeFileSync("build/studio-iam/main.tf.json", files["main.tf.json"]);
});
test("rejects unsafe identities, unknown fields, unavailable topology combinations and consumer overrun", () => {
  for (const change of [
    { password: "secret" },
    { zones: ["us-east-1a", "us-east-1a", "us-east-1c"] },
    { region: "eu-west-1" },
    { name: "../../bad" },
    { vpcCIDR: "10.40.1.1/16" },
    { writerMax: 37 },
    { logPartitions: 121 },
    { networkProvider: "cilium" },
    { hubble: true },
    { sampleRate: 0 },
    { sampleRate: 100 },
    { profileNamespaces: ["*"] },
    { existing: { password: "SYNTHETIC-MARKER" } },
  ])
    assert.throws(() => validateRequest({ ...good(), ...change }));
});
test("existing cluster remains externally owned", () => {
  const r = good();
  r.mode = "existing";
  r.name = "existing-eks";
  r.networkProvider = "cilium";
  r.hubble = true;
  r.profiling = true;
  r.existing = {
    region: r.region,
    cluster_name: r.name,
    vpc_cidr: r.vpcCIDR,
    availability_zones: r.zones,
    quota_redis_host: "quota.example.com",
    secret_source: "existing",
    storage_class: "retained-gp3",
    create_storage_class: false,
    role_arns: {
      mimir: r.adminRole,
      tempo: r.adminRole,
      flink: "arn:aws:iam::123456789012:role/Flink",
      pyroscope: r.adminRole,
    },
    buckets: Object.fromEntries(
      [
        "mimir-blocks",
        "mimir-ruler",
        "mimir-alertmanager",
        "tempo",
        "flink",
        "pyroscope",
      ].map((k) => [k, "example-east-" + k]),
    ),
    operators: Object.fromEntries(operators.map((k) => [k, false])),
    cluster_arn: "arn:aws:eks:us-east-1:123456789012:cluster/existing-eks",
    operator_namespaces: ["custom-operators"],
    grafana_db_name: "existing_grafana",
    grafana_db_user: "grafana_writer",
  };
  const files = compileRequest(r),
    p = JSON.parse(files["platform.json"]);
  assert.deepEqual(validateExistingCoordinates(r.existing), r.existing);
  for (const change of [
    { security_profile: { authentication: { mode: "oauth" } } },
    { tuning: { collectors: { traceSamplingPercent: 1 } } },
    { role_arns: { ...r.existing.role_arns, password: "invalid" } },
    { buckets: { ...r.existing.buckets, token: "invalid" } },
  ])
    assert.throws(() =>
      validateExistingCoordinates({ ...r.existing, ...change }),
    );
  assert.equal(p.network_policy_provider, "cilium");
  assert.ok(Object.values(p.operators).every((v) => !v));
  assert.equal(files["main.tf.json"], undefined);
  assert.equal(JSON.parse(files["IAM-PLAN.json"]).summary.createdRoles, 0);
  assert.equal(
    JSON.parse(files["IAM-PLAN.json"]).summary.createdAssociations,
    0,
  );
  assert.equal(p.role_arns.flink, r.existing.role_arns.flink);
  assert.equal(p.buckets.flink, r.existing.buckets.flink);
  for (const key of ["role_arns", "buckets"]) {
    const missing = structuredClone(r);
    delete missing.existing[key].flink;
    assert.throws(() => compileRequest(missing), /flink/);
  }
  assert.deepEqual(existingOptions(p), existingOptions(r.existing));
  assert.equal(p.grafana_db_name, "existing_grafana");
  for (const change of [
    { cluster_arn: "arn:aws:eks:us-west-2:123456789012:cluster/existing-eks" },
    { grafana_db_user: "admin; drop database" },
    { operator_namespaces: ["*"] },
  ])
    assert.throws(() =>
      compileRequest({ ...r, existing: { ...r.existing, ...change } }),
    );
  for (const change of [
    { buckets: { ...r.existing.buckets, pyroscope: { password: "invalid" } } },
    { role_arns: { ...r.existing.role_arns, pyroscope: "invalid" } },
  ])
    assert.throws(() =>
      compileRequest({
        ...r,
        profiling: false,
        existing: { ...r.existing, ...change },
      }),
    );
  mkdirSync("build/studio", { recursive: true });
  writeFileSync("build/studio/existing.json", files["platform.json"]);
  writeFileSync(
    "build/studio/new-tuning.json",
    compileRequest(good())["tuning.json"],
  );
  writeFileSync("build/studio/bundle.zip", zipFiles(files));
});
test("existing deployments require explicit controller ownership and reject credentials", () => {
  const r = good();
  r.mode = "existing";
  r.existing = { password: "bad" };
  assert.throws(() => compileRequest(r));
});
test("service definitions reject script URLs, self-dependencies and unknown SLO inputs", () => {
  const s = JSON.parse(readFileSync("examples/service.json"));
  assert.equal(validateService(s).name, "checkout");
  for (const change of [
    { repository: "javascript:alert(1)" },
    { runbook: 'https://a.example/"<script>' },
    { name: "../../bad" },
    { dependencies: ["applications/checkout"] },
    { slo: { enabled: true, target: 100, source: "application" } },
    { slo: { ...s.slo, query: "up" } },
  ])
    assert.throws(() => validateService({ ...s, ...change }));
});
test("ZIP output supports Unicode content", () => {
  const zip = zipFiles({ "README.md": "Hello — κόσμος\n" });
  assert.equal(new DataView(zip.buffer).getUint32(0, true), 0x04034b50);
  mkdirSync("build/studio", { recursive: true });
  writeFileSync("build/studio/unicode.zip", zip);
});
test("older requests receive explicit advanced defaults and full requests round-trip without drift", () => {
  const old = good();
  for (const key of [
    "securityProfile",
    "privacy",
    "serviceGraph",
    "isolation",
    "qualification",
    "iam",
  ])
    delete old[key];
  const first = compileRequest(old);
  const resumed = JSON.parse(first["deployment-request.json"]);
  assert.deepEqual(resumed.privacy, defaults.privacy);
  assert.deepEqual(resumed.iam, defaults.iam);
  assert.deepEqual(compileRequest(resumed), first);
  assert.equal(Object.hasOwn(old, "privacy"), false);
});
