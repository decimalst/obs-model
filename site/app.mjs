import {
  defaults,
  operators,
  compileRequest,
  validateService,
  zipFiles,
  existingOptions,
} from "./model.mjs";
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
function request() {
  const data = new FormData(form),
    r = structuredClone(defaults);
  for (const k of Object.keys(r)) {
    if (["version", "existing"].includes(k)) continue;
    if (["hubble", "profiling"].includes(k)) r[k] = data.has(k);
    else if (["zones", "profileNamespaces"].includes(k))
      r[k] = list(data.get(k));
    else if (typeof defaults[k] === "number") r[k] = Number(data.get(k));
    else if (data.has(k)) r[k] = data.get(k).trim();
  }
  if (r.mode === "existing")
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
        ...(r.profiling ? { pyroscope: data.get("pyroscopeRole").trim() } : {}),
      },
      buckets: {
        "mimir-blocks": data.get("mimirBlocks").trim(),
        "mimir-ruler": data.get("mimirRuler").trim(),
        "mimir-alertmanager": data.get("mimirAlertmanager").trim(),
        tempo: data.get("tempoBucket").trim(),
        ...(r.profiling
          ? { pyroscope: data.get("pyroscopeBucket").trim() }
          : {}),
      },
      operators: Object.fromEntries(
        operators.map((k) => [k, data.has("install-" + k)]),
      ),
    };
  return r;
}
function showFile(name) {
  $("#preview").textContent = files[name];
  $$("#file-tabs button").forEach((b) =>
    b.classList.toggle("selected", b.textContent === name),
  );
}
function update() {
  const existing = new FormData(form).get("mode") === "existing";
  $("#existing-fields").hidden = !existing;
  $("#admin-field").hidden = existing;
  $("#deployment-error").textContent = "";
  if (step === 3) {
    try {
      files = compileRequest(request());
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
      $("#preview").textContent =
        "Resolve the configuration issue below to generate your files.";
      $("#file-tabs").replaceChildren();
      $("#deployment-error").textContent = e.message;
      $("#download").disabled = true;
    }
  }
}
function setStep(n) {
  step = n;
  $$("[data-page]").forEach((e) => (e.hidden = +e.dataset.page !== n));
  $$("[data-step]").forEach((e) => {
    e.classList.toggle("selected", +e.dataset.step === n);
    e.setAttribute("aria-current", +e.dataset.step === n ? "step" : "false");
  });
  $("#back").hidden = n === 0;
  $("#next").hidden = n === 3;
  update();
}
$$("[data-step]").forEach((e) =>
  e.addEventListener("click", () => setStep(+e.dataset.step)),
);
$("#next").addEventListener("click", () => setStep(Math.min(3, step + 1)));
$("#back").addEventListener("click", () => setStep(Math.max(0, step - 1)));
form.addEventListener("input", update);
form.addEventListener("submit", (e) => e.preventDefault());
$("#download").addEventListener("click", () => {
  try {
    const r = request();
    save(r.name + ".zip", zipFiles(compileRequest(r)), "application/zip");
  } catch (e) {
    $("#deployment-error").textContent = e.message;
  }
});
$("#import-existing").addEventListener("click", () => {
  try {
    const e = JSON.parse(form.elements.existing.value);
    for (const [k, v] of Object.entries({
      name: e.cluster_name,
      region: e.region,
      vpcCIDR: e.vpc_cidr,
      zones: e.availability_zones?.join(", "),
    })) {
      if (typeof v !== "string")
        throw new Error(
          "The contract needs cluster_name, region, vpc_cidr and availability_zones",
        );
      form.elements[k].value = v;
    }
    for (const [k, v] of Object.entries({
      quotaRedis: e.quota_redis_host,
      storageClass: e.storage_class,
      mimirRole: e.role_arns?.mimir,
      tempoRole: e.role_arns?.tempo,
      pyroscopeRole: e.role_arns?.pyroscope,
      mimirBlocks: e.buckets?.["mimir-blocks"],
      mimirRuler: e.buckets?.["mimir-ruler"],
      mimirAlertmanager: e.buckets?.["mimir-alertmanager"],
      tempoBucket: e.buckets?.tempo,
      pyroscopeBucket: e.buckets?.pyroscope,
    }))
      form.elements[k].value = v || "";
    if (
      !e.operators ||
      !operators.every((k) => typeof e.operators[k] === "boolean")
    )
      throw new Error(
        "Supply every operator install/reuse choice in the imported contract",
      );
    for (const k of operators)
      form.elements["install-" + k].checked = e.operators[k];
    existingExtras = existingOptions(e);
    update();
  } catch (e) {
    $("#deployment-error").textContent = e.message;
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
setStep(0);
