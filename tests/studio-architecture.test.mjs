import test from "node:test";
import assert from "node:assert/strict";
import { defaults, compileRequest } from "../site/model.mjs";
import { architectureModel } from "../site/architecture.mjs";

const request = () => structuredClone(defaults);
const nodes = (model) =>
  Object.fromEntries(
    [...model.lanes.flatMap((l) => l.nodes), ...model.foundation].map((n) => [
      n.id,
      n,
    ]),
  );

test("default map is a draft with no changed areas and optional profiling off", () => {
  const model = architectureModel(request());
  assert.equal(model.changedCount, 0);
  assert.match(model.validation, /^Draft/);
  assert.equal(nodes(model).alloy.enabled, false);
  assert.equal(nodes(model).pyroscope.enabled, false);
  assert.match(
    nodes(model).quota.lines.join(" "),
    /Regional, per namespace\/service/,
  );
});

test("capacity labels match exported tuning and keep replicas distinct from shards", () => {
  const r = {
    ...request(),
    zones: ["us-east-1a", "us-east-1b", "us-east-1c"],
    adminRole: "arn:aws:iam::123456789012:role/Admin",
    logEventsPerSecond: 500,
    traceBaselinePercent: 25,
    logPartitions: 48,
    writerMax: 24,
    clickhouseShards: 3,
  };
  const before = JSON.stringify(r),
    model = architectureModel(r, 2),
    map = nodes(model);
  const tuning = JSON.parse(compileRequest(r)["tuning.json"]);
  assert.match(
    map.quota.lines[0],
    new RegExp(`${tuning.quota.eventsPerSecond} events/s`),
  );
  assert.match(map.quota.lines[0], /burst 1,000/);
  assert.match(
    map.kafka.lines[0],
    new RegExp(`${tuning.scaling.logPartitions} log partitions`),
  );
  assert.match(
    map.kafka.lines[1],
    new RegExp(`ceiling ${tuning.writerAutoscaling.maxReplicas}`),
  );
  assert.match(
    map.samplers.lines[0],
    new RegExp(`${tuning.collectors.traceSamplingPercent}%`),
  );
  assert.match(map.clickhouse.lines[0], /3 shards × 3 replicas = 9/);
  assert.equal(map.quota.changed, true);
  assert.equal(map.quota.current, true);
  assert.equal(map.mimir.changed, false);
  assert.equal(JSON.stringify(r), before);
});

test("invalid drafts cannot masquerade as validated deployment or overstate consumer concurrency", () => {
  const r = request();
  r.writerMax = 99;
  r.logPartitions = 12;
  r.logEventsPerSecond = NaN;
  const model = architectureModel(r);
  assert.match(model.validation, /^Draft/);
  assert.match(nodes(model).kafka.lines[1], /ceiling 12 · requested 99/);
  assert.match(nodes(model).quota.lines[0], /Not set/);
});

test("visibility, privacy, access and startup choices affect their own components", () => {
  const r = request();
  r.profiling = true;
  r.sampleRate = 31;
  r.profileNamespaces = ["checkout"];
  r.privacy.logs.mode = "redact";
  r.privacy.traces.mode = "allowlist";
  r.serviceGraph.enabled = true;
  r.serviceGraph.services = ["checkout"];
  r.startupGates.delaySeconds = 9;
  r.securityProfile = {
    access: { mode: "ingress" },
    authentication: { mode: "oauth" },
    internalTransport: { mode: "existing-mesh" },
  };
  const map = nodes(architectureModel(r, 4));
  assert.equal(map.pyroscope.enabled, true);
  assert.match(map.alloy.lines.join(" "), /31 Hz.*checkout/);
  assert.match(map.grafana.lines.join(" "), /Pyroscope.*ingress · oauth/);
  assert.match(map.router.lines.join(" "), /allowlist/);
  assert.match(map.vector.lines.join(" "), /redact/);
  assert.match(map.samplers.lines.join(" "), /separate pre-tail branch/);
  assert.match(map.startup.lines[0], /9s delay/);
  assert.equal(map.privacy.current, true);
  r.profiling = false;
  assert.equal(nodes(architectureModel(r)).pyroscope.enabled, false);
});

test("existing infrastructure and planning flags never imply new runtime resources", () => {
  const r = request();
  r.mode = "existing";
  r.networkProvider = "cilium";
  r.hubble = true;
  r.existing = { operators: { keda: true }, storage_class: "external-gp3" };
  r.isolation.enabled = true;
  r.isolation.mode = "isolated-clusters";
  r.isolation.additionalCells = [{ id: "another" }];
  r.qualification = {};
  const map = nodes(architectureModel(r, 5));
  assert.match(map.controllers.lines.join(" "), /cert-manager: reuse/);
  assert.match(map.controllers.lines.join(" "), /keda: install/);
  assert.match(map.plumbing.lines.join(" "), /Reuse external-gp3/);
  assert.match(map.scrape.lines.join(" "), /existing Cilium/);
  assert.match(map.plans.lines.join(" "), /pending, not executed/);
  assert.equal(map.extensions.enabled, false);
  assert.equal(map.aws.external, true);
  assert.equal(map.identity.external, true);
});
