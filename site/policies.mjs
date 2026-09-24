// Browser-safe, dependency-free mirror of the versioned security/tuning contracts.
// These options compile files only; they never contact a cluster or identity provider.
export const policyDefaults = Object.freeze({
  securityProfile: null,
  privacy: {
    logs: { mode: "off", patterns: ["email", "bearer", "credential"] },
    traces: {
      mode: "off",
      keepSpanName: false,
      allowedSpanAttributes: [
        "http.request.method",
        "http.response.status_code",
        "http.status_code",
        "rpc.system",
        "db.system",
        "messaging.system",
      ],
      allowedResourceAttributes: [
        "service.name",
        "service.namespace",
        "service.instance.id",
        "k8s.namespace.name",
        "k8s.pod.uid",
        "k8s.node.name",
        "cloud.region",
        "k8s.cluster.name",
      ],
    },
  },
  serviceGraph: {
    enabled: false,
    services: [],
    storeTTLSeconds: 30,
    storeMaxItems: 10000,
    flushIntervalSeconds: 15,
    cacheLoopSeconds: 60,
    histogramBucketsSeconds: [
      0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10,
    ],
  },
});

const reference = /^[a-z0-9]([a-z0-9.-]{0,61}[a-z0-9])?$/;
const attribute = /^[a-zA-Z][a-zA-Z0-9_.-]{0,127}$/;
function insist(condition, message) {
  if (!condition) throw new Error(message);
}
function object(value, fields, required, label) {
  insist(
    value !== null && typeof value === "object" && !Array.isArray(value),
    `${label} must be an object`,
  );
  insist(
    Object.keys(value).every((key) => fields.includes(key)) &&
      required.every((key) => Object.hasOwn(value, key)),
    `${label} has missing or unsupported fields; never include credentials`,
  );
}
function choice(value, options, label) {
  insist(options.includes(value), `Select a supported ${label}`);
}
function text(value, pattern, label) {
  insist(
    typeof value === "string" && pattern.test(value),
    `Set a valid ${label}`,
  );
}
function boolean(value, label) {
  insist(typeof value === "boolean", `${label} must be boolean`);
}
function integer(value, min, max, label) {
  insist(
    Number.isInteger(value) && value >= min && value <= max,
    `${label} must be an integer from ${min} to ${max}`,
  );
}
function list(value, min, max, check, label) {
  insist(
    Array.isArray(value) &&
      value.length >= min &&
      value.length <= max &&
      new Set(value).size === value.length &&
      value.every(check),
    `${label} must contain ${min}–${max} valid, unique entries`,
  );
}
function securityURL(value, loopback = false) {
  // Validate the unnormalized input before WHATWG URL parsing. In particular,
  // URL() normalizes backslashes, missing slashes, and explicit default ports.
  const bad =
    "Use plain HTTPS URLs without credentials or queries (HTTP loopback for port-forward)";
  insist(
    typeof value === "string" &&
      value.length > 0 &&
      value.length <= 2048 &&
      !/[\p{White_Space}\u0000-\u001f$\{\}\\"']/u.test(value) &&
      /^https?:\/\/[^/]/i.test(value),
    bad,
  );
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error(bad);
  }
  const authority = value.match(/^https?:\/\/([^/?#]*)/i)?.[1] ?? "";
  const port = authority.match(/:(\d+)$/)?.[1];
  insist(
    parsed.hostname &&
      !parsed.username &&
      !parsed.password &&
      !parsed.search &&
      !parsed.hash &&
      (loopback
        ? parsed.protocol === "http:" &&
          ["localhost", "127.0.0.1"].includes(parsed.hostname) &&
          // Do not allow URL normalization to turn alternate integer/IP forms
          // into an accepted loopback address.
          /^(localhost|127\.0\.0\.1)(?::\d*)?$/i.test(authority)
        : parsed.protocol === "https:") &&
      (port === undefined || Number(port) === (loopback ? 3000 : 443)),
    bad,
  );
  return parsed;
}

function validateSecurity(profile) {
  if (profile === null) return;
  const fields = [
    "version",
    "serverSecretName",
    "access",
    "authentication",
    "internalTransport",
  ];
  object(profile, fields, fields, "Security profile");
  insist(profile.version === 1, "Unsupported security profile version");
  text(
    profile.serverSecretName,
    reference,
    "existing Grafana server Secret name",
  );
  const { access, authentication: auth, internalTransport: internal } = profile;
  object(
    access,
    ["mode", "url", "proxyNamespace", "ingressClassName", "tlsSecretName"],
    ["mode", "url"],
    "Grafana access",
  );
  choice(
    access.mode,
    ["port-forward", "ingress", "external-proxy"],
    "Grafana access mode",
  );
  for (const key of ["proxyNamespace", "ingressClassName", "tlsSecretName"])
    if (Object.hasOwn(access, key))
      text(access[key], reference, "existing access resource name");
  const local = access.mode === "port-forward";
  const publicURL = securityURL(access.url, local);
  insist(
    publicURL.pathname === "/",
    "Grafana security profiles require a root-path URL",
  );
  // URL() resolves /../ away; the backend rejects non-root paths before that normalization.
  const rawPath = access.url
    .replace(/^https?:\/\/[^/?#]*/i, "")
    .split(/[?#]/, 1)[0];
  insist(
    ["", "/"].includes(rawPath),
    "Grafana security profiles require a root-path URL",
  );
  if (local) {
    insist(
      Object.keys(access).length === 2,
      "Port-forward access accepts only mode and URL",
    );
  } else {
    insist(
      Boolean(access.proxyNamespace),
      "HTTPS access requires the existing proxy namespace",
    );
  }
  if (access.mode === "ingress") {
    insist(
      Boolean(access.ingressClassName && access.tlsSecretName),
      "Ingress requires an existing class and TLS Secret",
    );
  } else {
    insist(
      !Object.hasOwn(access, "ingressClassName") &&
        !Object.hasOwn(access, "tlsSecretName"),
      "Ingress class and TLS Secret apply only to ingress mode",
    );
  }
  const authFields = [
    "mode",
    "secretName",
    "authURL",
    "tokenURL",
    "userInfoURL",
    "scopes",
    "groupsClaim",
    "roles",
    "useRefreshToken",
    "caConfigMap",
  ];
  object(auth, authFields, ["mode"], "Grafana authentication");
  choice(auth.mode, ["local", "oauth"], "authentication mode");
  insist(
    !local || auth.mode === "local",
    "Port-forward profiles require local authentication",
  );
  if (auth.mode === "local") {
    insist(
      Object.keys(auth).length === 1,
      "Local authentication does not accept OAuth settings",
    );
  } else {
    insist(
      authFields.slice(0, 8).every((key) => Object.hasOwn(auth, key)),
      "OAuth requires explicit endpoints, Secret, scopes and group roles",
    );
    text(auth.secretName, reference, "existing OAuth Secret name");
    for (const key of ["authURL", "tokenURL", "userInfoURL"])
      securityURL(auth[key]);
    list(
      auth.scopes,
      1,
      20,
      (scope) =>
        typeof scope === "string" && /^[A-Za-z0-9_:.\/-]{1,128}$/.test(scope),
      "OAuth scopes",
    );
    insist(auth.scopes.includes("openid"), "OAuth requires the openid scope");
    if (Object.hasOwn(auth, "useRefreshToken"))
      boolean(auth.useRefreshToken, "OAuth refresh token selection");
    insist(
      !auth.scopes.includes("offline_access") || auth.useRefreshToken === true,
      "offline_access requires refresh tokens",
    );
    if (Object.hasOwn(auth, "caConfigMap"))
      text(auth.caConfigMap, reference, "existing CA ConfigMap name");
    text(
      auth.groupsClaim,
      /^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$/,
      "OAuth group claim path",
    );
    object(
      auth.roles,
      ["Viewer", "Editor", "Admin"],
      [],
      "OAuth role mappings",
    );
    insist(
      Object.keys(auth.roles).length > 0,
      "OAuth requires at least one group role mapping",
    );
    for (const groups of Object.values(auth.roles))
      list(
        groups,
        1,
        50,
        (group) =>
          typeof group === "string" &&
          /^[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,127}$/.test(group),
        "OAuth role groups",
      );
    const groups = Object.values(auth.roles).flat();
    insist(
      new Set(groups).size === groups.length,
      "A group may map to only one Grafana role",
    );
  }
  object(
    internal,
    ["mode", "provider", "grafanaClientIdentities"],
    ["mode"],
    "Internal transport",
  );
  choice(
    internal.mode,
    ["cluster-network", "existing-mesh"],
    "internal transport mode",
  );
  if (internal.mode === "cluster-network") {
    insist(
      Object.keys(internal).length === 1,
      "Mesh settings require existing-mesh mode",
    );
  } else {
    insist(!local, "An existing mesh requires HTTPS access");
    choice(internal.provider, ["istio", "linkerd"], "existing mesh provider");
    const pattern =
      internal.provider === "istio"
        ? /^[a-z0-9.-]+\/ns\/[a-z0-9-]+\/sa\/[a-z0-9-]+$/
        : /^[a-z0-9-]+\.[a-z0-9-]+\.serviceaccount\.identity\.[a-z0-9.-]+$/;
    list(
      internal.grafanaClientIdentities,
      1,
      20,
      (identity) =>
        typeof identity === "string" &&
        identity.length <= 256 &&
        pattern.exec(identity)?.[0] === identity,
      "Exact mesh client identities",
    );
  }
}

export function validatePolicies(r) {
  insist(
    r !== null && typeof r === "object" && !Array.isArray(r),
    "Policy options must be an object",
  );
  validateSecurity(r.securityProfile);
  object(r.privacy, ["logs", "traces"], ["logs", "traces"], "Privacy policy");
  const { logs, traces } = r.privacy;
  object(logs, ["mode", "patterns"], ["mode", "patterns"], "Log privacy");
  choice(logs.mode, ["off", "redact", "drop"], "log privacy mode");
  list(
    logs.patterns,
    1,
    3,
    (pattern) => ["email", "bearer", "credential"].includes(pattern),
    "Log privacy patterns",
  );
  const traceFields = [
    "mode",
    "keepSpanName",
    "allowedSpanAttributes",
    "allowedResourceAttributes",
  ];
  object(traces, traceFields, traceFields, "Trace privacy");
  choice(traces.mode, ["off", "allowlist"], "trace privacy mode");
  boolean(traces.keepSpanName, "Trace span name retention");
  for (const key of ["allowedSpanAttributes", "allowedResourceAttributes"])
    list(
      traces[key],
      0,
      64,
      (item) => typeof item === "string" && attribute.test(item),
      "Trace attribute allowlist",
    );
  if (traces.mode === "allowlist")
    insist(
      [
        "service.name",
        "service.namespace",
        "cloud.region",
        "k8s.cluster.name",
      ].every((name) => traces.allowedResourceAttributes.includes(name)),
      "Trace privacy must preserve service, namespace and regional identity",
    );
  const graph = r.serviceGraph;
  const graphFields = Object.keys(policyDefaults.serviceGraph);
  object(graph, graphFields, graphFields, "Service graph");
  boolean(graph.enabled, "Service graph selection");
  list(
    graph.services,
    0,
    64,
    (service) =>
      typeof service === "string" &&
      /^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$/.test(service),
    "Service graph allowlist",
  );
  insist(
    !graph.enabled || graph.services.length > 0,
    "Service graphs require an explicit service allowlist",
  );
  integer(graph.storeTTLSeconds, 2, 120, "Service graph pairing TTL");
  integer(graph.storeMaxItems, 100, 100000, "Service graph pairing capacity");
  integer(graph.flushIntervalSeconds, 5, 60, "Service graph flush interval");
  integer(graph.cacheLoopSeconds, 15, 120, "Service graph cache interval");
  list(
    graph.histogramBucketsSeconds,
    2,
    20,
    (bucket) =>
      typeof bucket === "number" &&
      Number.isFinite(bucket) &&
      bucket > 0 &&
      bucket <= 120,
    "Service graph histogram buckets in seconds",
  );
  insist(
    graph.histogramBucketsSeconds.every(
      (bucket, index, buckets) => index === 0 || bucket > buckets[index - 1],
    ),
    "Service graph histogram buckets must be strictly increasing",
  );
  return r;
}

export function compilePolicies(r) {
  validatePolicies(r);
  return {
    tuning: structuredClone({
      privacy: r.privacy,
      serviceGraph: r.serviceGraph,
    }),
    files:
      r.securityProfile === null
        ? {}
        : {
            "security-profile.json":
              JSON.stringify(r.securityProfile, null, 2) + "\n",
          },
  };
}
