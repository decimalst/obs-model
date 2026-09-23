#!/usr/bin/env python3
"""Export reviewed regional configuration for Flux; never contacts a cluster."""

import argparse
from pathlib import Path

import yaml

try:
    from .bootstrap_crds import packaged_crds
    from .platform_config import load_platform
    from .regional_values import operator_values, values
    from .tuning import load_tuning, merge
except ImportError:
    from bootstrap_crds import packaged_crds
    from platform_config import load_platform
    from regional_values import operator_values, values
    from tuning import load_tuning, merge


def bundle(platform, image, path, automatic_promotion=True):
    """Keep release names/storage namespaces identical to the verified installer."""
    operators = operator_values(platform)
    regional = values(platform, image)
    regional["rollouts"] = {"autoPromotionEnabled": automatic_promotion}
    documents = {}
    documents["crds/resources.yaml"] = packaged_crds(operators["components"])
    documents["crds/kustomization.yaml"] = {
        "apiVersion": "kustomize.config.k8s.io/v1beta1",
        "kind": "Kustomization",
        "resources": ["resources.yaml"] if documents["crds/resources.yaml"] else [],
    }
    sync = []
    for name, namespace, chart_values in [
        ("operators", "observability-system", operators),
        ("platform", "observability", regional),
    ]:
        release = {
            "apiVersion": "helm.toolkit.fluxcd.io/v2",
            "kind": "HelmRelease",
            "metadata": {"name": name, "namespace": "flux-system"},
            "spec": {
                "interval": "5m",
                "timeout": "15m",
                "releaseName": "olly" if name == "platform" else "operators",
                "targetNamespace": namespace,
                "storageNamespace": namespace,
                "chart": {
                    "spec": {
                        "chart": f"./charts/{name}",
                        "sourceRef": {"kind": "GitRepository", "name": "flux-system"},
                        "reconcileStrategy": "Revision",
                        "interval": "1m",
                    }
                },
                "install": {
                    "crds": "Skip",
                    "createNamespace": False,
                    "disableWait": name == "platform",
                },
                "upgrade": {
                    "crds": "Skip",
                    "disableWait": name == "platform",
                    "cleanupOnFail": True,
                    "remediation": {"retries": 0},
                },
                "driftDetection": {"mode": "enabled"},
                "values": chart_values,
            },
        }
        if name == "platform":
            release["spec"]["dependsOn"] = [{"name": "operators"}]
            release["spec"]["driftDetection"]["ignore"] = [
                {
                    "paths": ["/spec/replicas"],
                    "target": {
                        "kind": "Rollout|Deployment",
                        "name": "log-quota|vector-gateway|vector-writer|otel-metrics|otel-router",
                    },
                },
                {
                    "paths": ["/spec/selector/rollouts-pod-template-hash"],
                    "target": {
                        "kind": "Service",
                        "name": "(log-quota|vector-gateway|otel-metrics|otel-router)(-preview)?",
                    },
                },
            ]
            if not regional.get("autoscaling", {}).get("enabled", True):
                release["spec"]["driftDetection"]["ignore"] = [
                    rule
                    for rule in release["spec"]["driftDetection"]["ignore"]
                    if rule["paths"] != ["/spec/replicas"]
                ]
        documents[f"{name}/release.yaml"] = release
        documents[f"{name}/kustomization.yaml"] = {
            "apiVersion": "kustomize.config.k8s.io/v1beta1",
            "kind": "Kustomization",
            "resources": ["release.yaml"],
        }
    for name, dependency in [("crds", None), ("operators", "crds"), ("platform", "operators")]:
        spec = {
            "interval": "5m",
            "retryInterval": "1m",
            "timeout": "15m",
            "path": f"./{path}/{name}",
            "prune": False,
            "wait": name != "platform",
            "sourceRef": {"kind": "GitRepository", "name": "flux-system"},
        }
        if dependency:
            spec["dependsOn"] = [{"name": "olly-" + dependency}]
        sync.append(
            {
                "apiVersion": "kustomize.toolkit.fluxcd.io/v1",
                "kind": "Kustomization",
                "metadata": {"name": "olly-" + name, "namespace": "flux-system"},
                "spec": spec,
            }
        )
    documents["sync.yaml"] = sync
    # flux bootstrap creates flux-system/ alongside these files.
    documents["kustomization.yaml"] = {
        "apiVersion": "kustomize.config.k8s.io/v1beta1",
        "kind": "Kustomization",
        "resources": ["flux-system", "sync.yaml"],
    }
    return documents


def write_bundle(documents, destination):
    for relative, content in documents.items():
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump_all(content if isinstance(content, list) else [content], sort_keys=False)
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--quota-image", required=True)
    parser.add_argument("--tuning", type=Path)
    parser.add_argument("--security-profile", type=Path)
    parser.add_argument(
        "--output", type=Path, required=True, help="Repository-relative gitops/clusters/REGION path"
    )
    parser.add_argument("--manual-promotion", action="store_true")
    args = parser.parse_args()
    if args.output.is_absolute() or ".." in args.output.parts:
        parser.error("output must be a path inside this repository")
    if args.output.exists():
        parser.error(
            "output exists; edit its reviewed release values instead of overwriting Git state"
        )
    platform = load_platform(args.config)
    if args.security_profile:
        try:
            from .security_config import load_profile
        except ImportError:
            from security_config import load_profile

        platform["security_profile"] = load_profile(args.security_profile)
    if args.tuning:
        platform["tuning"] = merge(platform.get("tuning", {}), load_tuning(args.tuning))
    write_bundle(
        bundle(platform, args.quota_image, args.output.as_posix(), not args.manual_promotion),
        args.output,
    )
    print(f"Wrote {args.output}. Review and commit; bootstrap instructions: docs/gitops.md.")


if __name__ == "__main__":
    main()
