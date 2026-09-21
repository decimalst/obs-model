#!/usr/bin/env python3
"""Render charts and extract component configs for executable validation."""

import pathlib
import subprocess

import yaml
from bootstrap_crds import packaged_crds

ROOT = pathlib.Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"


def render(platform_values=None):
    BUILD.mkdir(exist_ok=True)
    bootstrap = packaged_crds()
    (BUILD / "bootstrap-crds.yaml").write_text(yaml.safe_dump_all(bootstrap, sort_keys=False))
    packaged_names = {d["metadata"]["name"] for d in bootstrap}
    for chart, namespace in [
        ("operators", "observability-system"),
        ("platform", "observability"),
        ("debug-grants", "applications"),
        ("flink-operator", "observability-system"),
    ]:
        result = subprocess.run(
            [
                "helm",
                "template",
                "olly",
                str(ROOT / "charts" / chart),
                "-n",
                namespace,
                "--include-crds",
                *(["-f", str(platform_values)] if chart == "platform" and platform_values else []),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        if chart == "operators":
            objects = [d for d in yaml.safe_load_all(result.stdout) if d]
            objects = [
                d
                for d in objects
                if not (
                    d["kind"] == "CustomResourceDefinition"
                    and d["metadata"]["name"] in packaged_names
                )
            ]
            result.stdout = yaml.safe_dump_all(bootstrap + objects, sort_keys=False)
        (BUILD / f"{chart}.yaml").write_text(result.stdout)
    for doc in yaml.safe_load_all((BUILD / "platform.yaml").read_text()):
        if doc and doc["kind"] == "OpenTelemetryCollector":
            directory = BUILD / "configs" / doc["metadata"]["name"]
            directory.mkdir(exist_ok=True, parents=True)
            (directory / "collector.yaml").write_text(yaml.safe_dump(doc["spec"]["config"]))
        if not doc or doc["kind"] != "ConfigMap":
            continue
        for filename, value in doc.get("data", {}).items():
            if filename in [
                "vector.yaml",
                "collector.yaml",
                "schema.sql",
                "mimir.yaml",
                "tempo.yaml",
            ]:
                directory = BUILD / "configs" / doc["metadata"]["name"]
                directory.mkdir(exist_ok=True, parents=True)
                (directory / filename).write_text(value)


if __name__ == "__main__":
    render()
