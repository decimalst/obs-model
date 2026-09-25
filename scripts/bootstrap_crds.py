#!/usr/bin/env python3
"""Prepare packaged CRDs before `helm --skip-crds` (including on upgrades).

Altinity 0.27.3 repeats schema descriptions and identical type keys. We permit
only those nonsemantic duplicates in that pinned dependency, then emit canonical
JSON/YAML. Conflicting schema keys always fail; all other packages are strict.

Cert-manager's templated CRDs stay owned by the operators Helm release. On a
fresh CLI install, deploy.sh first installs that same release without OTel,
waits for cert-manager readiness, then enables OTel's Certificate/Issuer users.
Do not apply those templated CRDs out of band or adopt an external controller.
"""

import argparse
import pathlib
import subprocess
import tarfile

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


class UniqueLoader(yaml.SafeLoader):
    allow_altinity_annotations = False


def unique_mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        value = loader.construct_object(value_node, deep=True)
        if key in result:
            permitted = loader.allow_altinity_annotations and (
                key == "description" or result[key] == value
            )
            if not permitted:
                raise ValueError(f"Conflicting or unexpected duplicate YAML key: {key}")
        result[key] = value
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


class AltinityLoader(UniqueLoader):
    allow_altinity_annotations = True


def packaged_crds(enabled=None):
    result = []
    for archive in sorted((ROOT / "charts/operators/charts").glob("*.tgz")):
        loader = (
            AltinityLoader
            if archive.name == "altinity-clickhouse-operator-0.27.3.tgz"
            else UniqueLoader
        )
        with tarfile.open(archive) as chart:
            metadata = next(
                m
                for m in chart.getmembers()
                if m.name.count("/") == 1 and m.name.endswith("/Chart.yaml")
            )
            chart_name = yaml.safe_load(chart.extractfile(metadata).read())["name"]
            if enabled is not None and not enabled.get(chart_name, True):
                continue
            if chart_name == "keda":
                # KEDA packages templated CRDs under templates/crds, not crds/.
                rendered = subprocess.run(
                    [
                        "helm",
                        "template",
                        "crd-bootstrap",
                        str(archive),
                        "--set",
                        "crds.install=true",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                result.extend(
                    d
                    for d in yaml.load_all(rendered.stdout, Loader=UniqueLoader)
                    if d and d["kind"] == "CustomResourceDefinition"
                )
            for item in chart.getmembers():
                parts = pathlib.PurePosixPath(item.name).parts
                if len(parts) >= 3 and parts[1] == "crds" and item.name.endswith((".yaml", ".yml")):
                    result.extend(
                        d for d in yaml.load_all(chart.extractfile(item).read(), Loader=loader) if d
                    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operators-values", type=pathlib.Path)
    args = parser.parse_args()
    enabled = (
        yaml.safe_load(args.operators_values.read_text()).get("components", {})
        if args.operators_values
        else None
    )
    path = ROOT / "build/bootstrap-crds.yaml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(yaml.safe_dump_all(packaged_crds(enabled), sort_keys=False))
    print(
        f"Prepared {path}; apply server-side before installing/upgrading operators with --skip-crds"
    )


if __name__ == "__main__":
    main()
