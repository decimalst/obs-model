import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  policyDefaults,
  validatePolicies,
  compilePolicies,
} from "../site/policies.mjs";

const options = () => structuredClone(policyDefaults);
const localProfile = () => ({
  version: 1,
  serverSecretName: "grafana-server",
  access: { mode: "port-forward", url: "http://localhost:3000" },
  authentication: { mode: "local" },
  internalTransport: { mode: "cluster-network" },
});
const oauthProfile = () => ({
  version: 1,
  serverSecretName: "grafana-server",
  access: {
    mode: "ingress",
    url: "https://grafana.example.com",
    proxyNamespace: "ingress-system",
    ingressClassName: "shared-ingress",
    tlsSecretName: "grafana-tls",
  },
  authentication: {
    mode: "oauth",
    secretName: "grafana-oauth",
    authURL: "https://identity.example.com/authorize",
    tokenURL: "https://identity.example.com/oauth/token",
    userInfoURL: "https://identity.example.com/userinfo",
    scopes: ["openid", "profile", "email", "groups", "offline_access"],
    groupsClaim: "realm.groups",
    roles: {
      Viewer: ["observability-viewers"],
      Editor: ["observability-editors"],
      Admin: ["observability-admins"],
    },
    useRefreshToken: true,
    caConfigMap: "identity-ca",
  },
  internalTransport: { mode: "cluster-network" },
});
function accepted(profile) {
  const r = options();
  r.securityProfile = profile;
  return compilePolicies(r);
}

test("policy defaults preserve every chart policy key and compile without changing inputs", () => {
  const schema = JSON.parse(readFileSync("charts/platform/values.schema.json"));
  const r = options();
  const before = structuredClone(r);
  const compiled = compilePolicies(r);
  assert.deepEqual(compiled.tuning, {
    privacy: r.privacy,
    serviceGraph: r.serviceGraph,
  });
  assert.deepEqual(compiled.files, {});
  assert.deepEqual(r, before);
  for (const key of ["privacy", "serviceGraph"])
    assert.deepEqual(
      Object.keys(r[key]).sort(),
      Object.keys(schema.properties[key].properties).sort(),
    );
  for (const key of ["logs", "traces"])
    assert.deepEqual(
      Object.keys(r.privacy[key]).sort(),
      Object.keys(schema.properties.privacy.properties[key].properties).sort(),
    );
  compiled.tuning.privacy.logs.patterns.pop();
  assert.deepEqual(r, before);
  assert.equal(validatePolicies(r), r);
});

test("all supported security profiles preserve selections and only emit Secret references", () => {
  for (const profile of [localProfile(), oauthProfile()])
    assert.deepEqual(
      JSON.parse(accepted(profile).files["security-profile.json"]),
      profile,
    );
  const external = oauthProfile();
  external.access = {
    mode: "external-proxy",
    url: "https://grafana.example.com/",
    proxyNamespace: "edge-system",
  };
  for (const provider of ["cluster-network", "istio", "linkerd"]) {
    const profile = structuredClone(external);
    if (provider !== "cluster-network")
      profile.internalTransport = {
        mode: "existing-mesh",
        provider,
        grafanaClientIdentities: [
          provider === "istio"
            ? "cluster.local/ns/edge-system/sa/shared-ingress"
            : "shared-ingress.edge-system.serviceaccount.identity.linkerd.cluster.local",
        ],
      };
    const files = accepted(profile).files;
    assert.deepEqual(Object.keys(files), ["security-profile.json"]);
    assert.deepEqual(JSON.parse(files["security-profile.json"]), profile);
    assert.equal(
      files["security-profile.json"].includes("client_secret"),
      false,
    );
  }
  const secureLocal = oauthProfile();
  secureLocal.authentication = { mode: "local" };
  assert.doesNotThrow(() => accepted(secureLocal));
});

test("security rejects credential-bearing unknown fields without reflecting their names or values", () => {
  const marker = "sensitive-do-not-echo";
  for (const path of [
    [],
    ["access"],
    ["authentication"],
    ["authentication", "roles"],
    ["internalTransport"],
  ]) {
    const profile = oauthProfile();
    let target = profile;
    for (const key of path) target = target[key];
    target[marker] = marker;
    assert.throws(
      () => accepted(profile),
      (error) => !error.message.includes(marker),
    );
  }
  for (const change of [
    (p) => delete p.serverSecretName,
    (p) => {
      p.version = 2;
    },
    (p) => {
      p.serverSecretName = "a/b";
    },
    (p) => {
      p.authentication.secretName = { password: marker };
    },
    (p) => {
      p.authentication.caConfigMap = "../ca";
    },
  ]) {
    const profile = oauthProfile();
    change(profile);
    assert.throws(() => accepted(profile));
  }
});

test("security URL validation rejects unsafe or normalized paths, ports, queries and loopback spellings", () => {
  for (const url of [
    "http://grafana.example.com",
    "https://user:secret@grafana.example.com",
    "https://grafana.example.com?token=secret",
    "https://grafana.example.com/#secret",
    "https://grafana.example.com/grafana",
    "https://grafana.example.com/grafana/..",
    "https://grafana.example.com:8443",
    "https:////grafana.example.com",
    "https://grafana.example.com\\evil",
    "https://${DOMAIN}",
    "https://grafana.example.com\n",
  ]) {
    const profile = oauthProfile();
    profile.access.url = url;
    assert.throws(() => accepted(profile));
  }
  for (const url of [
    "http://localhost:80",
    "http://127.1:3000",
    "http://2130706433:3000",
    "http://0x7f000001:3000",
    "https://localhost:3000",
    "http://example.com:3000",
  ]) {
    const profile = localProfile();
    profile.access.url = url;
    assert.throws(() => accepted(profile));
  }
  for (const url of ["http://localhost:3000", "http://127.0.0.1:3000/"])
    assert.doesNotThrow(() =>
      accepted({ ...localProfile(), access: { mode: "port-forward", url } }),
    );
  for (const key of ["authURL", "tokenURL", "userInfoURL"]) {
    const profile = oauthProfile();
    profile.authentication[key] = "https://identity.example.com?secret=abc";
    assert.throws(() => accepted(profile));
  }
});

test("security rejects mismatched ownership and authentication combinations instead of dropping settings", () => {
  for (const change of [
    (p) => {
      p.access.mode = "external-proxy";
    },
    (p) => {
      delete p.access.proxyNamespace;
    },
    (p) => {
      delete p.access.tlsSecretName;
    },
    (p) => {
      p.authentication.mode = "local";
    },
    (p) => {
      p.authentication.scopes = ["profile"];
    },
    (p) => {
      p.authentication.useRefreshToken = false;
    },
    (p) => {
      p.authentication.useRefreshToken = "true";
    },
    (p) => {
      p.authentication.roles = {};
    },
    (p) => {
      p.authentication.roles.Admin = p.authentication.roles.Viewer;
    },
    (p) => {
      p.authentication.roles.Viewer = [];
    },
    (p) => {
      p.authentication.roles.Admin = ["admin' || true"];
    },
    (p) => {
      p.authentication.groupsClaim = "groups[*]";
    },
    (p) => {
      p.authentication.groupsClaim = "groups || 'Admin'";
    },
    (p) => {
      p.internalTransport.provider = "istio";
    },
    (p) => {
      p.internalTransport = { mode: "existing-mesh", provider: "istio" };
    },
    (p) => {
      p.access = localProfile().access;
    },
  ]) {
    const profile = oauthProfile();
    change(profile);
    assert.throws(() => accepted(profile));
  }
  const local = localProfile();
  local.access.proxyNamespace = "edge-system";
  assert.throws(() => accepted(local));
  for (const identity of [
    "*",
    "cluster.local/ns/*/sa/ingress",
    "cluster.local/ns/edge/sa/ingress\n",
    "shared-ingress.edge-system.serviceaccount.identity.linkerd.cluster.local",
  ]) {
    const profile = oauthProfile();
    profile.internalTransport = {
      mode: "existing-mesh",
      provider: "istio",
      grafanaClientIdentities: [identity],
    };
    assert.throws(() => accepted(profile));
  }
});

test("privacy selections are complete, explicit, and preserve required regional identity", () => {
  for (const mode of ["off", "redact", "drop"]) {
    const r = options();
    r.privacy.logs.mode = mode;
    r.privacy.logs.patterns = ["credential"];
    r.privacy.traces.mode = "allowlist";
    r.privacy.traces.keepSpanName = true;
    r.privacy.traces.allowedSpanAttributes = [];
    assert.deepEqual(compilePolicies(r).tuning.privacy, r.privacy);
  }
  for (const change of [
    (r) => {
      r.privacy.logs.patterns = [];
    },
    (r) => {
      r.privacy.logs.patterns = ["email", "email"];
    },
    (r) => {
      r.privacy.logs.patterns = ["ssn"];
    },
    (r) => {
      r.privacy.logs.mode = "allowlist";
    },
    (r) => {
      r.privacy.traces.keepSpanName = "true";
    },
    (r) => {
      r.privacy.traces.allowedSpanAttributes = ["*"];
    },
    (r) => {
      r.privacy.traces.allowedResourceAttributes = ["a", "a"];
    },
    (r) => {
      r.privacy.traces.allowedSpanAttributes = Array.from(
        { length: 65 },
        (_, n) => "a" + n,
      );
    },
    (r) => {
      r.privacy.logs.customPattern = ".*";
    },
    (r) => {
      r.privacy.traces.mode = "allowlist";
      r.privacy.traces.allowedResourceAttributes = [];
    },
  ]) {
    const r = options();
    change(r);
    assert.throws(() => compilePolicies(r));
  }
  for (const required of [
    "service.name",
    "service.namespace",
    "cloud.region",
    "k8s.cluster.name",
  ]) {
    const r = options();
    r.privacy.traces.mode = "allowlist";
    r.privacy.traces.allowedResourceAttributes =
      r.privacy.traces.allowedResourceAttributes.filter(
        (name) => name !== required,
      );
    assert.throws(() => compilePolicies(r));
  }
});

test("service graph validates allowlist, bounded state, and histogram boundary ordering", () => {
  for (const boundary of [
    {
      storeTTLSeconds: 2,
      storeMaxItems: 100,
      flushIntervalSeconds: 5,
      cacheLoopSeconds: 15,
      histogramBucketsSeconds: [0.001, 120],
    },
    {
      storeTTLSeconds: 120,
      storeMaxItems: 100000,
      flushIntervalSeconds: 60,
      cacheLoopSeconds: 120,
      histogramBucketsSeconds: [1, 120],
    },
  ]) {
    const r = options();
    Object.assign(r.serviceGraph, boundary, {
      enabled: true,
      services: ["checkout", "payments"],
    });
    assert.deepEqual(compilePolicies(r).tuning.serviceGraph, r.serviceGraph);
  }
  for (const change of [
    { enabled: true },
    { enabled: "false" },
    { services: ["checkout", "checkout"] },
    { services: ["*"] },
    { services: Array.from({ length: 65 }, (_, n) => "service" + n) },
    { storeTTLSeconds: 1 },
    { storeTTLSeconds: 121 },
    { storeMaxItems: 99 },
    { storeMaxItems: 100001 },
    { flushIntervalSeconds: 4 },
    { flushIntervalSeconds: 61 },
    { cacheLoopSeconds: 14 },
    { cacheLoopSeconds: 121 },
    { cacheLoopSeconds: 30.5 },
    { histogramBucketsSeconds: [0, 1] },
    { histogramBucketsSeconds: [2, 1] },
    { histogramBucketsSeconds: [1, 1] },
    { histogramBucketsSeconds: [1, 121] },
    { histogramBucketsSeconds: [1, Infinity] },
    { histogramBucketsSeconds: [NaN, 1] },
    { histogramBucketsSeconds: [1] },
    { histogramBucketsSeconds: [1, "2"] },
    { virtualNodes: true },
  ]) {
    const r = options();
    Object.assign(r.serviceGraph, change);
    assert.throws(() => compilePolicies(r));
  }
});
