import test from "node:test";
import assert from "node:assert/strict";
import {
  operationsDefaults,
  createQualification,
  normalizeQualification,
  validateOperations,
  compileOperations,
} from "../site/operations.mjs";
const good = () => ({
  ...structuredClone(operationsDefaults),
  mode: "new",
  name: "olly-production",
  region: "us-east-1",
  adminRole: "arn:aws:iam::123456789012:role/Admin",
  owner: "platform",
  hubble: false,
  profiling: false,
  serviceGraph: { enabled: false },
  existing: null,
});
const withQualification = () => {
  const r = good();
  r.qualification = createQualification(r);
  return r;
};
const withIsolation = () => {
  const r = good();
  r.isolation.enabled = true;
  r.isolation.services = ["applications/checkout", "applications/catalog"];
  return r;
};
test("operations are opt-in, pure and contain no completed evidence", () => {
  assert.deepEqual(compileOperations(good()), {});
  const r = withQualification(),
    original = structuredClone(r);
  const files = compileOperations(r),
    q = JSON.parse(files["qualification.json"]);
  assert.deepEqual(r, original);
  assert.equal(q.sourceRevision, "0".repeat(40));
  assert.equal(q.deploymentArtifacts.platformContractSHA256, "0".repeat(64));
  assert.equal(q.recovery.rpoMinutes, null);
  assert.equal(q.recovery.crossRegion.enabled, false);
  assert.equal(q.environment.mode, "managed");
  assert.equal(q.features.externalNotificationDelivery, false);
  assert.equal(q.features.gitops, false);
  assert.equal(q.features.flink, false);
  assert.equal(q.status, undefined);
  assert.equal(q.receipts, undefined);
  assert.match(files["OPERATIONS.md"], /--check-only/);
  assert.match(
    files["OPERATIONS.md"],
    /Required unexecuted gates stay pending/,
  );
});
test("qualification facts are derived from actual selections, other flags select acceptance only", () => {
  const r = withQualification();
  r.mode = "existing";
  r.hubble = r.profiling = r.serviceGraph.enabled = true;
  r.existing = {
    cluster_arn: "arn:aws:eks:us-east-1:123456789012:cluster/olly-production",
  };
  r.qualification.environment.accountId = "123456789012";
  r.qualification.environment.cluster = "stale-cluster";
  r.qualification.environment.region = "eu-west-1";
  r.qualification.environment.mode = "existing";
  r.qualification.features.flink = true;
  r.qualification.features.gitops = true;
  r.qualification.features.externalNotificationDelivery = true;
  const files = compileOperations(r),
    q = JSON.parse(files["qualification.json"]);
  assert.equal(q.environment.region, r.region);
  assert.equal(q.environment.cluster, r.name);
  assert.equal(q.environment.mode, "software-only");
  assert.ok(Object.values(q.features).every((value) => value === true));
  assert.match(files["OPERATIONS.md"], /acceptance scope only/);
  r.qualification.environment.accountId = "210987654321";
  assert.throws(() => compileOperations(r), /account must match/);
  const fresh = createQualification(r);
  assert.equal(fresh.deploymentArtifacts.terraformPlanSHA256, null);
});
test("qualification account follows deployment identity, never the administrator's account", () => {
  const r = good();
  assert.equal(createQualification(r).environment.accountId, "000000000000");
  r.iam = {
    terraformRoleArn: "arn:aws:iam::210987654321:role/Provisioner",
    existingWorkloadRoles: {},
  };
  r.qualification = createQualification(r);
  assert.equal(r.qualification.environment.accountId, "210987654321");
  assert.doesNotThrow(() => compileOperations(r));
  r.qualification.environment.accountId = "123456789012";
  assert.throws(() => compileOperations(r), /Terraform assumed-role account/);
});
test("recovery destination, cross-region opt-in and nullable reviewed targets are validated", () => {
  const r = withQualification();
  r.qualification.recovery.destination.cluster = r.name;
  assert.throws(() => validateOperations(r), /different isolated cluster/);
  r.qualification.recovery.destination.region = "us-west-2";
  assert.throws(() => validateOperations(r), /explicit cross-region opt-in/);
  r.qualification.recovery.crossRegion.enabled = true;
  assert.doesNotThrow(() => validateOperations(r));
  r.qualification.recovery.backupMode = "external";
  r.qualification.recovery.providerRef = "RUNBOOK-BACKUP-123";
  r.qualification.recovery.rpoMinutes = 60;
  r.qualification.recovery.rtoMinutes = 240;
  r.qualification.recovery.crossRegion = {
    enabled: true,
    rpoMinutes: 120,
    rtoMinutes: 480,
    costAssessmentRef: "COST-2026-09",
  };
  const q = JSON.parse(compileOperations(r)["qualification.json"]);
  assert.deepEqual(q.recovery, r.qualification.recovery);
  r.qualification.recovery.destination.region = r.region;
  r.qualification.recovery.destination.cluster = "restore-lab";
  assert.throws(() => validateOperations(r), /different region/);
});
test("qualification rejects credential fields, schema errors and malformed optional values without echoing values", () => {
  const changes = [
    (q) => {
      q.secret = "DO-NOT-ECHO";
    },
    (q) => {
      q.environment.accessKey = "DO-NOT-ECHO";
    },
    (q) => {
      q.features.password = "DO-NOT-ECHO";
    },
    (q) => {
      q.recovery.token = "DO-NOT-ECHO";
    },
    (q) => {
      q.recovery.destination.password = "DO-NOT-ECHO";
    },
    (q) => {
      q.recovery.crossRegion.secret = "DO-NOT-ECHO";
    },
    (q) => {
      q.deploymentArtifacts.awsAccessKey = "DO-NOT-ECHO";
    },
    (q) => {
      q.sourceRevision = "latest";
    },
    (q) => {
      q.deploymentArtifacts.platformContractSHA256 = "a".repeat(63);
    },
    (q) => {
      q.environment.accountId = 123456789012;
    },
    (q) => {
      q.environment.context = "name\ncommand";
    },
    (q) => {
      q.environment.namespace = "other";
    },
    (q) => {
      q.recovery.providerRef = "https://example.com?token=DO-NOT-ECHO";
    },
    (q) => {
      q.recovery.providerRef = "https://name:DO-NOT-ECHO@example.com";
    },
    (q) => {
      q.recovery.owner = "";
    },
    (q) => {
      q.recovery.rpoMinutes = -1;
    },
    (q) => {
      q.recovery.rtoMinutes = 0.5;
    },
    (q) => {
      q.recovery.crossRegion.rtoMinutes = Infinity;
    },
    (q) => {
      q.recovery.crossRegion.rpoMinutes = 525601;
    },
    (q) => {
      q.recovery.crossRegion.enabled = "yes";
    },
    (q) => {
      q.features.hubble = "yes";
    },
    (q) => {
      q.features.flink = null;
    },
    (q) => {
      delete q.recovery.rpoMinutes;
    },
    (q) => {
      delete q.features.gitops;
    },
  ];
  for (const change of changes) {
    const r = withQualification();
    change(r.qualification);
    assert.throws(
      () => compileOperations(r),
      (error) => !error.message.includes("DO-NOT-ECHO"),
    );
  }
});
test("shared cell exports reference future platform output without claiming provisioning", () => {
  const r = withIsolation(),
    original = structuredClone(r);
  const files = compileOperations(r),
    cells = JSON.parse(files["cells.json"]);
  assert.deepEqual(cells, {
    version: 1,
    mode: "shared",
    cells: [
      {
        id: "regional-shared",
        platform: "platform.reviewed.json",
        services: r.isolation.services,
      },
    ],
  });
  assert.deepEqual(r, original);
  assert.match(
    files["OPERATIONS.md"],
    /current platform.reviewed.json does not exist yet/,
  );
  assert.match(files["OPERATIONS.md"], /Only this deployment is generated/);
  assert.equal(files["platform.json"], undefined);
  assert.equal(files["platform.reviewed.json"], undefined);
  assert.match(
    files["OPERATIONS.md"],
    /cp build\/olly-production-review\/platform.json environments\/olly-production\/platform.reviewed.json/,
  );
  assert.match(
    files["OPERATIONS.md"],
    /reviewed contract contains the selected budgets/,
  );
});
test("existing isolated cells require exact cluster identity and local reviewed peer references", () => {
  const r = withIsolation();
  r.mode = "existing";
  r.isolation.mode = "isolated-clusters";
  assert.throws(() => compileOperations(r), /exact cluster ARN/);
  r.existing = {
    cluster_arn: "arn:aws:eks:us-east-1:123456789012:cluster/wrong-cluster",
  };
  assert.throws(() => compileOperations(r), /must match/);
  r.existing.cluster_arn =
    "arn:aws:eks:us-east-1:123456789012:cluster/olly-production";
  assert.throws(() => compileOperations(r), /Grafana host\/database/);
  r.isolation.grafanaDatabaseIdentity = "critical.example.internal/grafana";
  r.isolation.additionalCells = [
    {
      id: "batch-east",
      platform: "../batch/platform.json",
      grafanaDatabaseIdentity: "batch.example.internal/grafana",
      services: ["analytics/importer"],
    },
  ];
  const files = compileOperations(r),
    cells = JSON.parse(files["cells.json"]);
  assert.equal(cells.cells.length, 2);
  assert.equal(cells.cells[0].platform, "platform.json");
  assert.equal(cells.cells[1].platform, "../batch/platform.json");
  assert.match(files["OPERATIONS.md"], /browser cannot read them/);
  r.isolation.additionalCells[0].id = r.isolation.id;
  assert.throws(() => compileOperations(r), /IDs must be unique/);
});
test("new isolated cell plans explicitly defer coordinate validation until Terraform output exists", () => {
  const r = withIsolation();
  r.isolation.mode = "isolated-clusters";
  const files = compileOperations(r);
  assert.match(
    files["OPERATIONS.md"],
    /remains unavailable until every referenced contract has real coordinates/,
  );
  assert.equal(
    JSON.parse(files["cells.json"]).cells[0].grafanaDatabaseIdentity,
    undefined,
  );
});
test("cell options reject unknown fields, ambiguous ownership and unsafe platform references", () => {
  for (const change of [
    (i) => {
      i.password = "DO-NOT-ECHO";
    },
    (i) => {
      i.enabled = "yes";
    },
    (i) => {
      i.mode = "shared-namespace";
    },
    (i) => {
      i.services = [];
    },
    (i) => {
      i.services = ["checkout"];
    },
    (i) => {
      i.services = ["apps/checkout", "apps/checkout"];
    },
    (i) => {
      i.id = "../unsafe";
    },
    (i) => {
      i.grafanaDatabaseIdentity = "user:DO-NOT-ECHO@host/db";
    },
    (i) => {
      i.additionalCells = [
        { id: "peer", platform: "peer.json", services: ["apps/worker"] },
      ];
    },
  ]) {
    const r = withIsolation();
    change(r.isolation);
    assert.throws(
      () => compileOperations(r),
      (error) => !error.message.includes("DO-NOT-ECHO"),
    );
  }
  for (const platform of [
    "/private/platform.json",
    "https://example.com/platform.json",
    "./platform.json",
    "./platform.reviewed.json",
    "x/../platform.reviewed.json",
    "x/../platform.json",
    "../peer//platform.json",
    "peer.json?token=DO-NOT-ECHO",
    "`command`.json",
  ]) {
    const r = withIsolation();
    r.isolation.mode = "isolated-clusters";
    r.isolation.additionalCells = [
      { id: "peer", platform, services: ["apps/worker"] },
    ];
    assert.throws(
      () => compileOperations(r),
      (error) => !error.message.includes("DO-NOT-ECHO"),
    );
  }
  const r = good();
  r.isolation = null;
  assert.throws(() => validateOperations(r));
});

test("normalized qualification matches the export without mutating the request", () => {
  const r = withQualification();
  r.qualification.environment.cluster = "stale-cluster";
  r.qualification.features.serviceGraphs = false;
  r.serviceGraph.enabled = true;
  const original = structuredClone(r);
  const normalized = normalizeQualification(r);
  assert.equal(normalized.environment.cluster, r.name);
  assert.equal(normalized.features.serviceGraphs, true);
  assert.deepEqual(
    normalized,
    JSON.parse(compileOperations(r)["qualification.json"]),
  );
  assert.deepEqual(r, original);
  normalized.recovery.owner = "changed-owner";
  assert.equal(r.qualification.recovery.owner, "platform");
  assert.equal(normalizeQualification(good()), null);
});
