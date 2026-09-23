#!/usr/bin/env python3
"""Read-only prerequisite checks; never create, adopt, or modify existing resources."""

import argparse
import json
import pathlib
import subprocess
from collections import Counter

import yaml

try:
    from .platform_config import BUCKETS, OPERATORS, load_platform
    from .regional_values import operator_values, values
except ImportError:
    from platform_config import BUCKETS, OPERATORS, load_platform
    from regional_values import operator_values, values

CRDS = {
    "keda": ("scaledobjects.keda.sh", "v1alpha1"),
    "cert-manager": ("certificates.cert-manager.io", "v1"),
    "argo-rollouts": ("rollouts.argoproj.io", "v1alpha1"),
    "strimzi-kafka-operator": ("kafkas.kafka.strimzi.io", "v1"),
    "opentelemetry-operator": ("opentelemetrycollectors.opentelemetry.io", "v1beta1"),
    "altinity-clickhouse-operator": ("clickhouseinstallations.clickhouse.altinity.com", "v1"),
}
LABELS = {
    "keda": ("app", "keda-operator"),
    "cert-manager": ("app.kubernetes.io/name", "cert-manager"),
    "argo-rollouts": ("app.kubernetes.io/name", "argo-rollouts"),
    "strimzi-kafka-operator": ("strimzi.io/kind", "cluster-operator"),
    "opentelemetry-operator": ("app.kubernetes.io/name", "opentelemetry-operator"),
    "altinity-clickhouse-operator": ("app", "altinity-clickhouse-operator"),
}


def read(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=45)
    if result.returncode:
        # Error output from external commands can include private values; report only the operation.
        raise ValueError(
            "Cannot read prerequisite: "
            + " ".join(args[:5])
            + ". Check authentication, permissions and network access."
        )
    return json.loads(result.stdout)


def require(ok, message):
    if not ok:
        raise ValueError(message)


def validate_generated_inputs(platform, manifest, controllers):
    """Do not let a stale overlay target another region or take over an excluded controller."""
    expected = values(platform, manifest.get("images", {}).get("quota", ""))

    def includes(actual, wanted):
        if isinstance(wanted, dict):
            return isinstance(actual, dict) and all(
                key in actual and includes(actual[key], value) for key, value in wanted.items()
            )
        return actual == wanted

    require(
        includes(manifest, expected) and includes(controllers, operator_values(platform)),
        "Generated overlays do not match the checked platform configuration; "
        "regenerate with install.py.",
    )


def ready_nodes(nodes, zones):
    counts = Counter()
    for node in nodes:
        spec, labels = node.get("spec", {}), node["metadata"].get("labels", {})
        ready = any(
            c["type"] == "Ready" and c["status"] == "True"
            for c in node.get("status", {}).get("conditions", [])
        )
        schedulable = not spec.get("unschedulable") and not any(
            t.get("effect") in ("NoSchedule", "NoExecute") for t in spec.get("taints", [])
        )
        if (
            ready
            and schedulable
            and labels.get("kubernetes.io/arch") == "amd64"
            and labels.get("kubernetes.io/os") == "linux"
        ):
            counts[labels.get("topology.kubernetes.io/zone")] += 1
    require(
        all(counts[z] >= 2 for z in zones),
        "Need at least two Ready, schedulable, untainted Linux/amd64 nodes per "
        "selected AZ; capacity is still subject to load testing.",
    )


def validate_storage_class(sc):
    require(
        sc.get("provisioner") == "ebs.csi.aws.com"
        and sc.get("parameters", {}).get("encrypted") == "true"
        and sc.get("volumeBindingMode") == "WaitForFirstConsumer"
        and sc.get("reclaimPolicy") == "Retain",
        "Existing StorageClass must use encrypted EBS CSI, WaitForFirstConsumer and "
        "Retain; choose a dedicated class if the current one differs.",
    )


def validate_network_provider(p, daemonsets, kube):
    cni = next((d for d in daemonsets if d["metadata"]["name"] == "aws-node"), {})
    containers = cni.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    agents = [
        c for c in containers if c["name"] in ("aws-eks-nodeagent", "aws-network-policy-agent")
    ]
    flags = []
    for agent in agents:
        options = agent.get("args", [])
        enabled = None
        for i, option in enumerate(options):
            if option.startswith("--enable-network-policy="):
                enabled = option.split("=", 1)[1]
            elif option == "--enable-network-policy":
                enabled = (
                    options[i + 1]
                    if i + 1 < len(options) and options[i + 1] in ("true", "false")
                    else "true"
                )
        flags.append(enabled)
    if p.get("network_policy_provider", "aws") == "aws":
        require(
            flags and all(flag == "true" for flag in flags),
            "AWS VPC CNI NetworkPolicy agent must explicitly enable network policy",
        )
        status = cni.get("status", {})
        require(
            status.get("desiredNumberScheduled", 0) > 0
            and status.get("numberReady") == status.get("desiredNumberScheduled"),
            "AWS VPC CNI must be ready on every eligible node",
        )
        return
    require(
        all(flag == "false" for flag in flags),
        "Cilium requires explicitly disabled AWS network-policy enforcement; "
        "the installer performs no automatic CNI migration",
    )
    cilium = next((d for d in daemonsets if d["metadata"]["name"] == "cilium"), {})
    status = cilium.get("status", {})
    require(
        status.get("desiredNumberScheduled", 0) > 0
        and status.get("numberReady") == status.get("desiredNumberScheduled"),
        "Existing Cilium must be ready on every eligible node",
    )
    config = kube("configmap", "cilium-config", "-n", "kube-system")["data"]
    require(
        config.get("enable-policy") in ("default", "always"),
        "Cilium NetworkPolicy enforcement must be enabled",
    )
    if p.get("tuning", {}).get("hubble", {}).get("enabled"):
        require(
            config.get("enable-hubble") == "true"
            and config.get("hubble-metrics-server") == ":9965",
            "Enable Hubble metrics on :9965 in the externally owned Cilium release",
        )
    if p.get("profiling", {}).get("enabled"):
        # Host kernel and admission readiness remain explicit deployment qualification gates.
        require(
            p.get("tuning", {}).get("profiling", {}).get("namespaces", ["applications"]),
            "Select profile namespaces",
        )


def preflight(p, reader=read):
    def aws(service, operation, *args):
        return reader(
            [
                "aws",
                service,
                operation,
                *args,
                "--region",
                p["region"],
                "--output",
                "json",
                "--no-cli-pager",
            ]
        )

    def kube(resource, *args):
        return reader(["kubectl", "--request-timeout=20s", "get", resource, *args, "-o", "json"])

    cluster = aws("eks", "describe-cluster", "--name", p["cluster_name"])["cluster"]
    require(cluster["status"] == "ACTIVE", "Existing EKS cluster must be ACTIVE")
    require(
        cluster["version"] == "1.35",
        "This release is validated for EKS 1.35; qualify another version before "
        "extending the supported version check.",
    )
    require(
        not p.get("cluster_arn") or p["cluster_arn"] == cluster["arn"],
        "Configured cluster ARN does not match the AWS account/region/cluster",
    )
    p["cluster_arn"] = cluster["arn"]
    context = reader(["kubectl", "config", "view", "--minify", "-o", "json"])
    require(
        context["clusters"][0]["cluster"]["server"].rstrip("/") == cluster["endpoint"].rstrip("/"),
        "Current Kubernetes context points at a different API server. Select the "
        "intended EKS context first; no kubeconfig was changed.",
    )
    vpc = aws("ec2", "describe-vpcs", "--vpc-ids", cluster["resourcesVpcConfig"]["vpcId"])["Vpcs"][
        0
    ]
    require(
        p["vpc_cidr"] == vpc["CidrBlock"],
        "Configured VPC CIDR differs from the existing cluster VPC",
    )
    ready_nodes(kube("nodes")["items"], p["availability_zones"])
    kube("csidriver", "ebs.csi.aws.com")
    ds = kube("daemonsets", "-n", "kube-system")["items"]
    identity = next((d for d in ds if d["metadata"]["name"] == "eks-pod-identity-agent"), {})
    status = identity.get("status", {})
    require(
        status.get("desiredNumberScheduled", 0) > 0
        and status.get("numberReady") == status.get("desiredNumberScheduled"),
        "Install/repair the EKS Pod Identity agent before deployment",
    )
    validate_network_provider(p, ds, kube)
    classes = {s["metadata"]["name"]: s for s in kube("storageclasses")["items"]}
    sc = classes.get(p["storage_class"])
    if p["create_storage_class"]:
        if sc:
            annotations = sc["metadata"].get("annotations", {})
            require(
                annotations.get("meta.helm.sh/release-name") == "olly"
                and annotations.get("meta.helm.sh/release-namespace") == "observability",
                "StorageClass already exists outside this release; select "
                "--existing-storage-class instead of adopting it",
            )
            validate_storage_class(sc)
    else:
        require(sc is not None, "Requested existing StorageClass is missing")
        validate_storage_class(sc)
    crds = {c["metadata"]["name"]: c for c in kube("crds")["items"]}
    deployments = kube("deployments", "-A")["items"]
    releases = reader(["helm", "list", "-A", "--all", "-o", "json"])
    owns_operators = any(
        r["name"] == "operators" and r["namespace"] == "observability-system" for r in releases
    )
    owned_components = (
        reader(["helm", "get", "values", "operators", "-n", "observability-system", "-o", "json"])
        or {}
        if owns_operators
        else {}
    )
    api_services = kube("apiservices")["items"]
    metrics_api = next(
        (a for a in api_services if a["metadata"]["name"] == "v1beta1.metrics.k8s.io"),
        None,
    )
    if p.get("operators", {}).get("metrics-server", True) and metrics_api:
        require(
            owns_operators and owned_components.get("components", {}).get("metrics-server", True),
            "metrics-server already exists. Explicitly reuse it "
            "to avoid APIService ownership conflicts.",
        )
    external_api = next(
        (a for a in api_services if a["metadata"]["name"] == "v1beta1.external.metrics.k8s.io"),
        None,
    )
    if p.get("operators", {}).get("keda", True) and external_api:
        require(
            owns_operators and owned_components.get("components", {}).get("keda", False),
            "An external metrics API already exists. Explicitly reuse compatible KEDA, "
            "or disable KEDA installation and select writerAutoscaling.mode=cpu. "
            "Do not replace another metrics adapter implicitly.",
        )
    uses_keda = (
        p.get("tuning", {}).get("autoscaling", {}).get("enabled", True)
        and p.get("tuning", {}).get("writerAutoscaling", {}).get("mode", "kafka") == "kafka"
    )
    if uses_keda and not p.get("operators", {}).get("keda", True):
        require(
            external_api
            and any(
                c["type"] == "Available" and c["status"] == "True"
                for c in external_api.get("status", {}).get("conditions", [])
            ),
            "Reused KEDA requires an Available external metrics API",
        )
    capacity_controllers = [
        d
        for d in deployments
        if any(
            "cluster-autoscaler:" in c.get("image", "")
            or "karpenter/controller:" in c.get("image", "")
            for c in d["spec"]["template"]["spec"]["containers"]
        )
    ]
    if p.get("operators", {}).get("cluster-autoscaler", True) and capacity_controllers:
        require(
            owns_operators
            and owned_components.get("components", {}).get("cluster-autoscaler", True),
            "A capacity controller already exists. Reuse existing capacity management.",
        )
    namespaces = {"observability-system"}
    for name in OPERATORS:
        enabled = p.get("operators", {}).get(name, True)
        require(
            enabled
            or not owns_operators
            or not owned_components.get("components", {}).get(name, True),
            f"{name} belongs to the operators release. Reuse cannot transfer ownership "
            "or uninstall it; keep it enabled until an explicit ownership migration.",
        )
        if name == "keda" and not enabled and not uses_keda:
            continue
        if name in CRDS:
            crd_name, version = CRDS[name]
            existing = crds.get(crd_name)
            if enabled:
                require(
                    not existing
                    or (owns_operators and owned_components.get("components", {}).get(name, True)),
                    f"{name} CRDs already exist. Explicitly reuse that controller "
                    "to avoid taking over its resources.",
                )
            else:
                require(
                    existing
                    and any(
                        v["name"] == version and v.get("served")
                        for v in existing["spec"]["versions"]
                    ),
                    f"Existing {name} must serve {crd_name}/{version}",
                )
                key, value = LABELS[name]
                matches = [
                    d
                    for d in deployments
                    if d["metadata"].get("labels", {}).get(key) == value
                    and d.get("status", {}).get("availableReplicas", 0) > 0
                ]
                # Altinity charts use varying labels; match the actual pinned controller image too.
                if name == "altinity-clickhouse-operator":
                    matches = [
                        d
                        for d in deployments
                        if any(
                            "altinity/clickhouse-operator:" in c.get("image", "")
                            for c in d["spec"]["template"]["spec"]["containers"]
                        )
                        and d.get("status", {}).get("availableReplicas", 0) > 0
                    ]
                require(
                    matches,
                    f"No available existing {name} controller found. "
                    "Verify its labels, version and watch namespace.",
                )
                namespaces.update(d["metadata"]["namespace"] for d in matches)
    if not p.get("operators", {}).get("metrics-server", True):
        require(metrics_api is not None, "Existing metrics-server API is missing")
        require(
            any(
                c["type"] == "Available" and c["status"] == "True"
                for c in metrics_api.get("status", {}).get("conditions", [])
            ),
            "Existing metrics-server API must be Available for HPA",
        )
    # Capacity management may be Cluster Autoscaler, Karpenter, or externally managed.
    for key in BUCKETS + (("pyroscope",) if p.get("profiling", {}).get("enabled") else ()):
        location = (
            aws("s3api", "get-bucket-location", "--bucket", p["buckets"][key]).get(
                "LocationConstraint"
            )
            or "us-east-1"
        )
        require(
            ("eu-west-1" if location == "EU" else location) == p["region"],
            f"Bucket {key} is outside the telemetry region",
        )
    associations = aws(
        "eks", "list-pod-identity-associations", "--cluster-name", p["cluster_name"]
    )["associations"]
    for service in ("mimir", "tempo") + (
        ("pyroscope",) if p.get("profiling", {}).get("enabled") else ()
    ):
        found = next(
            (
                a
                for a in associations
                if a["namespace"] == "observability" and a["serviceAccount"] == service
            ),
            None,
        )
        require(found, f"Missing Pod Identity association for observability/{service}")
        association = aws(
            "eks",
            "describe-pod-identity-association",
            "--cluster-name",
            p["cluster_name"],
            "--association-id",
            found["associationId"],
        )["association"]
        require(
            association["roleArn"] == p["role_arns"][service],
            f"Pod Identity role differs for {service}",
        )
    if p["secret_source"] == "aws":
        for key in ("grafana_db_secret_arn", "quota_secret_arn"):
            aws("secretsmanager", "describe-secret", "--secret-id", p[key])
    else:
        keys = {
            "grafana-db": {"GF_DATABASE_HOST", "GF_DATABASE_PASSWORD"},
            "grafana-admin": {"admin-user", "admin-password"},
            "quota-redis": {"password"},
            "clickhouse-auth": {"password"},
        }
        for name, required in keys.items():
            # Emit only key names from kubectl; never capture or print secret values.
            found = reader(
                [
                    "kubectl",
                    "-n",
                    "observability",
                    "get",
                    "secret",
                    name,
                    "-o",
                    'go-template={"keys":{ {{range $k, $v := .data}}{{printf "%q: '
                    'true," $k}}{{end}}"_end":true}}',
                ]
            )
            require(
                required <= set(found["keys"]), f"Existing secret {name} is missing required keys"
            )
    p["operator_namespaces"] = sorted(namespaces)
    print(
        "Verified cluster identity, AZs, node prerequisites, ownership, regional "
        "buckets and Pod Identity associations. No remote changes made.",
        flush=True,
    )
    return p


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--values")
    parser.add_argument("--operators-values")
    args = parser.parse_args()
    platform = preflight(load_platform(args.config))
    if args.values or args.operators_values:
        require(args.values and args.operators_values, "Supply both generated values files")
        validate_generated_inputs(
            platform,
            yaml.safe_load(pathlib.Path(args.values).read_text()),
            yaml.safe_load(pathlib.Path(args.operators_values).read_text()),
        )


if __name__ == "__main__":
    main()
