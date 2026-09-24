import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { iamDefaults, validateIAM, iamPlan, compileIAM } from "../site/iam.mjs";
const request = () => ({
  ...structuredClone(iamDefaults),
  mode: "new",
  name: "olly-test",
  region: "us-east-1",
  profiling: false,
  adminRole: "arn:aws:iam::999999999999:role/ExternalAdmin",
});
const arn = (name, account = "123456789012") =>
  `arn:aws:iam::${account}:role/${name}`;
const existing = () => ({
  ...request(),
  mode: "existing",
  existing: {
    cluster_arn: "arn:aws:eks:us-east-1:123456789012:cluster/olly-test",
    role_arns: { mimir: arn("mimir"), tempo: arn("tempo") },
    buckets: {
      "mimir-blocks": "test-mimir-blocks",
      "mimir-ruler": "test-mimir-ruler",
      "mimir-alertmanager": "test-mimir-alertmanager",
      tempo: "test-tempo",
    },
  },
});

test("default new-region inventory matches Terraform role and association ownership", () => {
  const r = request(),
    original = structuredClone(r),
    p = iamPlan(r);
  assert.deepEqual(r, original);
  assert.equal(p.summary.createdRoles, 7);
  assert.equal(p.summary.createdAssociations, 5);
  assert.equal(p.summary.reusedRoles, 0);
  assert.equal(
    p.accountId,
    null,
    "administrator account must not become deployment account",
  );
  assert.equal(p.accessGrants[0].createsRole, false);
  assert.equal(p.accessGrants[0].scope, "cluster");
  assert.equal(
    p.accessGrants[0].bootstrapClusterCreatorAdminPermissions,
    false,
  );
  assert.match(p.execution.mechanism, /credential chain/);
  assert.equal(
    p.roles.find((role) => role.id === "ebs").association.serviceAccount,
    "ebs-csi-controller-sa",
  );
  assert.equal(
    p.roles.find((role) => role.id === "autoscaler").association.namespace,
    "observability-system",
  );
  assert.ok(
    p.roles.every(
      (role) => role.arn === null && role.namePrefix.startsWith("olly-test-"),
    ),
  );
  const flink = p.roles.find((role) => role.id === "flink");
  assert.match(flink.notes.join(" "), /while the Flink job is disabled/);
  assert.match(JSON.stringify(flink.permissions), /<DEPLOYMENT_ACCOUNT_ID>/);
});

test("profiling adds one storage role and association; reused roles keep external policy ownership", () => {
  const r = request();
  r.profiling = true;
  r.iam = {
    terraformRoleArn: arn("Provisioner"),
    existingWorkloadRoles: { mimir: arn("Mimir"), pyroscope: arn("Pyroscope") },
  };
  const p = iamPlan(r);
  assert.equal(p.summary.createdRoles, 6);
  assert.equal(p.summary.reusedRoles, 2);
  assert.equal(p.summary.createdAssociations, 6);
  assert.equal(p.accountId, "123456789012");
  const role = p.roles.find((item) => item.id === "pyroscope");
  assert.equal(role.ownership, "reused");
  assert.equal(role.permissions.ownership, "external-required-unverified");
  assert.equal(role.association.ownership, "created");
  assert.equal(role.namePrefix, null);
  assert.deepEqual(role.trust.document.Statement[0].Condition, {
    StringEquals: { "aws:SourceAccount": "123456789012" },
  });
  assert.deepEqual(role.permissions.inlinePolicy.Statement[1].Resource, [
    "arn:aws:s3:::olly-test-123456789012-us-east-1-pyroscope/*",
  ]);
  assert.match(
    p.execution.requirements.join(" "),
    /does not configure the S3 state backend/,
  );
});

test("strict IAM validation rejects credentials, malformed ARNs, inactive and conflicting ownership", () => {
  for (const iam of [
    null,
    [],
    {},
    { ...iamDefaults.iam, password: "SYNTHETIC" },
    { ...iamDefaults.iam, terraformRoleArn: "arn:aws:iam::123456789012:root" },
    {
      ...iamDefaults.iam,
      terraformRoleArn: "arn:aws:sts::123456789012:assumed-role/a/session",
    },
    { ...iamDefaults.iam, terraformRoleArn: "" },
    { ...iamDefaults.iam, existingWorkloadRoles: { node: arn("Node") } },
    {
      ...iamDefaults.iam,
      existingWorkloadRoles: { mimir: { key: "SYNTHETIC" } },
    },
    {
      ...iamDefaults.iam,
      existingWorkloadRoles: { pyroscope: arn("Profiles") },
    },
    {
      ...iamDefaults.iam,
      existingWorkloadRoles: {
        mimir: arn("M"),
        tempo: arn("T", "999999999999"),
      },
    },
    {
      terraformRoleArn: arn("Provisioner"),
      existingWorkloadRoles: { mimir: arn("M", "999999999999") },
    },
  ])
    assert.throws(() => validateIAM({ ...request(), iam }));
  const e = existing();
  e.iam.terraformRoleArn = arn("Provisioner");
  assert.throws(() => validateIAM(e), /Software-only/);
  e.iam = {
    terraformRoleArn: null,
    existingWorkloadRoles: { mimir: arn("Mimir") },
  };
  assert.throws(() => validateIAM(e), /Software-only/);
  delete e.iam;
  assert.deepEqual(
    validateIAM(e),
    iamDefaults.iam,
    "legacy requests use empty defaults without mutation",
  );
  assert.equal(e.iam, undefined);
});

test("software-only mode creates no IAM resources and preserves explicit inactive profile references", () => {
  const r = existing();
  r.existing.role_arns.flink = arn("Flink");
  r.existing.buckets.flink = "test-flink";
  r.existing.role_arns.pyroscope = arn("Profiles");
  r.existing.buckets.pyroscope = "test-profiles";
  const p = iamPlan(r);
  assert.equal(p.summary.createdRoles, 0);
  assert.equal(p.summary.createdAssociations, 0);
  assert.equal(p.summary.reusedRoles, 4);
  assert.equal(p.accessGrants.length, 0);
  assert.ok(
    p.roles.every(
      (role) =>
        role.ownership === "reused" &&
        role.association.ownership === "external",
    ),
  );
  assert.match(
    p.roles.find((role) => role.id === "pyroscope").notes.join(" "),
    /profiling is disabled/,
  );
  assert.match(
    p.prerequisites.join(" "),
    /Selecting install for the Cluster Autoscaler chart does not create/,
  );
  r.existing.role_arns.pyroscope = arn("Profiles", "111111111111");
  assert.throws(() => iamPlan(r), /same cluster account/);
  r.existing.role_arns = {
    mimir: arn("Mimir", "111111111111"),
    tempo: arn("Tempo", "111111111111"),
  };
  assert.throws(() => iamPlan(r), /match the existing cluster ARN/);
});

test("reused role counts are distinct ARNs while associations and permissions retain workload identity", () => {
  const r = request();
  r.iam.existingWorkloadRoles = { mimir: arn("Shared"), tempo: arn("Shared") };
  const p = iamPlan(r);
  assert.equal(p.summary.createdRoles, 5);
  assert.equal(p.summary.reusedRoles, 1);
  assert.ok(
    p.roles
      .filter((item) => item.ownership === "reused")
      .every((item) =>
        item.notes.some((note) => note.includes("combined permissions")),
      ),
  );
  assert.equal(p.summary.createdAssociations, 5);
  assert.equal(p.roles.filter((item) => item.ownership === "reused").length, 2);
});

test("IAM guide includes exact permissions and honest trust/runtime boundaries", () => {
  const r = request(),
    files = compileIAM(r),
    p = JSON.parse(files["IAM-PLAN.json"]);
  assert.deepEqual(Object.keys(files).sort(), ["IAM-PLAN.json", "IAM.md"]);
  const text = files["IAM.md"];
  for (const phrase of [
    "sts:TagSession",
    "s3:AbortMultipartUpload",
    "s3:ListMultipartUploadParts",
    "required_role_policies",
    "no IAM role",
    "Grafana application roles",
    "service-linked roles",
    "aws:SourceAccount",
    "not bound to a single namespace/cluster",
    "separately",
    "read-only",
    "forbidden-bucket denial",
  ])
    assert.ok(text.includes(phrase), phrase);
  const scaling = p.roles.find((role) => role.id === "autoscaler").permissions
    .inlinePolicy.Statement;
  assert.deepEqual(scaling[1].Condition, {
    StringEquals: {
      "aws:ResourceTag/k8s.io/cluster-autoscaler/olly-test": "owned",
    },
  });
  assert.deepEqual(scaling[0].Resource, ["*"]);
  const storage = p.roles.find((role) => role.id === "mimir").permissions
    .inlinePolicy.Statement;
  assert.equal(storage[0].Resource.length, 3);
  assert.ok(storage.every((statement) => !statement.Action.includes("s3:*")));
  assert.match(text, /No workload Secrets Manager, KMS/);
});

test("documented IAM action inventory stays aligned with local Terraform source", () => {
  const r = request();
  r.profiling = true;
  const p = iamPlan(r);
  const source = [
    "terraform/modules/region/eks.tf",
    "terraform/modules/region/autoscaling.tf",
    "terraform/modules/platform-services/context.tf",
    "terraform/modules/platform-services/main.tf",
  ]
    .map((path) => readFileSync(path, "utf8"))
    .join("\n");
  for (const role of p.roles) {
    for (const statement of role.permissions.inlinePolicy?.Statement || []) {
      for (const action of statement.Action)
        assert.ok(
          source.includes(`"${action}"`),
          `Missing Terraform action ${action}`,
        );
    }
    for (const statement of role.trust.document.Statement) {
      for (const action of statement.Action)
        assert.ok(source.includes(`"${action}"`));
      assert.ok(source.includes(`"${statement.Principal.Service}"`));
    }
  }
  const storageSource = readFileSync(
    "terraform/modules/platform-services/main.tf",
    "utf8",
  );
  const policyBody = storageSource
    .split('data "aws_iam_policy_document" "storage"')[1]
    .split('resource "aws_iam_role_policy"')[0];
  const sourceActions = [...policyBody.matchAll(/"(s3:[A-Za-z]+)"/g)]
    .map((match) => match[1])
    .sort();
  const plannedActions = p.roles
    .find((role) => role.id === "mimir")
    .permissions.inlinePolicy.Statement.flatMap((statement) => statement.Action)
    .sort();
  assert.deepEqual(plannedActions, sourceActions);
});
