#!/usr/bin/env python3
"""Configure and install the platform, or run read-only preflight checks."""

import argparse
import os
import pathlib
import subprocess
import sys

import yaml

try:
    from .platform_config import OPERATORS, load_platform, write_platform
    from .preflight import preflight
    from .regional_values import operator_values, values
    from .tuning import load_tuning, merge
except ImportError:
    from platform_config import OPERATORS, load_platform, write_platform
    from preflight import preflight
    from regional_values import operator_values, values
    from tuning import load_tuning, merge


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=pathlib.Path,
        required=True,
        help="Terraform output JSON or a nonsecret platform YAML",
    )
    parser.add_argument("--quota-image", required=True, help="Published image@sha256:digest")
    parser.add_argument("--reuse-operators", nargs="*", choices=OPERATORS, default=[])
    parser.add_argument("--existing-storage-class")
    parser.add_argument(
        "--tuning", type=pathlib.Path, help="Validated nonsecret pipeline overrides"
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Read cloud/cluster state and render; do not change AWS or Kubernetes",
    )
    parser.add_argument("--output-dir", type=pathlib.Path, default=pathlib.Path("build/install"))
    args = parser.parse_args()
    p = load_platform(args.config)
    if args.tuning:
        p["tuning"] = merge(p.get("tuning", {}), load_tuning(args.tuning))
    p["operators"].update({name: False for name in args.reuse_operators})
    if args.existing_storage_class:
        p.update(storage_class=args.existing_storage_class, create_storage_class=False)
    generated = values(p, args.quota_image)
    # All remote calls before deployment are read-only; never change kubeconfig.
    preflight(p)
    generated["networkPolicy"]["operatorNamespaces"] = p.get(
        "operator_namespaces", ["observability-system"]
    )
    directory = args.output_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "platform.yaml"
    manifest.write_text(yaml.safe_dump(generated, sort_keys=False))
    manifest.with_suffix(".operators.yaml").write_text(
        yaml.safe_dump(operator_values(p), sort_keys=False)
    )
    config = directory / "platform.json"
    write_platform(p, config)
    subprocess.run(
        ["helm", "template", "olly", "charts/platform", "-n", "observability", "-f", str(manifest)],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    print(f"Preflight and chart rendering passed. Configuration: {manifest}", flush=True)
    if not args.check_only:
        subprocess.run(
            ["bash", "scripts/deploy.sh", str(manifest), str(config)],
            check=True,
            env={**os.environ, "PYTHON": sys.executable},
        )


if __name__ == "__main__":
    main()
