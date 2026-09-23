#!/usr/bin/env python3
"""Validate service ownership, compile Git-managed content, scaffold locally, publish explicitly."""

import argparse
import html
import json
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlencode

import jsonschema
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "schemas/service.schema.json").read_text())


def validate_service(service):
    jsonschema.Draft7Validator(SCHEMA).validate(service)
    if f"{service['namespace']}/{service['name']}" in service.get("dependencies", []):
        raise ValueError("A service cannot depend on itself")
    return service


def load_catalog(directory):
    entries = [
        validate_service(json.loads(p.read_text())) for p in sorted(Path(directory).glob("*.json"))
    ]
    identities = [f"{s['namespace']}/{s['name']}" for s in entries]
    if len(set(identities)) != len(identities):
        raise ValueError("Duplicate namespace/service identity")
    return entries


def slo_rules(service):
    if not service["slo"]["enabled"]:
        return []
    name, ns = service["name"], service["namespace"]
    observed = service["slo"]["source"] == "observed-spans"
    metric = "platform_red_calls_total" if observed else "service_requests_total"
    labels = f'service_name="{name}",service_namespace="{ns}"'
    if observed:
        labels += ',span_kind="SPAN_KIND_SERVER",otel_metric_overflow!="true"'
    failure = 'status_code="STATUS_CODE_ERROR"' if observed else 'status_class="5xx"'
    group = "cloud_region,k8s_cluster_name"
    budget = 1 - service["slo"]["target"] / 100
    rules = []
    base = {
        "service_name": name,
        "service_namespace": ns,
        "owner": service["owner"],
        "slo_source": service["slo"]["source"],
    }
    # Rate each writer before aggregating. No request means absent, not healthy.
    for window in ["5m", "30m", "1h", "6h"]:
        total = f"sum by ({group}) (rate({metric}{{{labels}}}[{window}]))"
        error = f"sum by ({group}) (rate({metric}{{{labels},{failure}}}[{window}]))"
        rules.append(
            {
                "record": f"service:slo_error_ratio:rate{window}",
                "expr": f"(({error}) or (0 * ({total}))) / (({total}) > 0)",
                "labels": base,
            }
        )
    selection = (
        f'service_name="{name}",service_namespace="{ns}",slo_source="{service["slo"]["source"]}"'
    )
    for long, short, burn, severity in [
        ("1h", "5m", 14.4, "critical"),
        ("6h", "30m", 6, "warning"),
    ]:
        rules.append(
            {
                "alert": "ServiceErrorBudgetBurn",
                "expr": (
                    f"(service:slo_error_ratio:rate{long}{{{selection}}} > {burn * budget:.10g}) "
                    f"and (service:slo_error_ratio:rate{short}{{{selection}}} "
                    f"> {burn * budget:.10g})"
                ),
                "for": "2m",
                "labels": {**base, "severity": severity},
                "annotations": {
                    "summary": (
                        f"{ns}/{name} exceeds its 30-day availability error budget burn threshold"
                    ),
                    "runbook_url": service["runbook"],
                    "description": (
                        "Observed telemetry only; verify coverage and alert delivery "
                        "before using this SLO operationally."
                    ),
                },
            }
        )
    return rules


def dashboard(entries):
    panels = []
    if not entries:
        entries_text = (
            "Register a service through the configuration studio, then commit its "
            "definition under catalog/services/. The catalog is generated during "
            "review and provisioned by Grafana."
        )
        panels.append(
            {
                "id": 1,
                "type": "text",
                "title": "Register your first service",
                "gridPos": {"x": 0, "y": 0, "w": 24, "h": 5},
                "options": {"mode": "markdown", "content": entries_text},
            }
        )
    for i, s in enumerate(entries):
        query = urlencode({"var-service": s["name"], "var-namespace": s["namespace"]})
        # HTML escape all user-provided content; URL fields reject executable schemes.
        content = (
            f"<p>{html.escape(s['description'])}</p>"
            f"<p>Owner: <b>{html.escape(s['owner'])}</b> · {s['lifecycle']}</p>"
            f'<p><a href="{html.escape(s["repository"], quote=True)}">Repository</a> · '
            f'<a href="{html.escape(s["runbook"], quote=True)}">Runbook</a> · '
            f'<a href="/d/service-red?{html.escape(query)}">RED dashboard</a></p>'
            f"<p>CPU profiling: {'opted in' if s['profiling'] else 'off'}. "
            f"SLO: {s['slo']['target']}% ({s['slo']['source']}) — "
            f"{'enabled' if s['slo']['enabled'] else 'off'}.</p>"
            "<p>Dependencies: "
            f"{html.escape(', '.join(s.get('dependencies', [])) or 'None declared')}</p>"
        )
        panels.append(
            {
                "id": i + 1,
                "title": f"{s['namespace']}/{s['name']}",
                "type": "text",
                "gridPos": {"x": (i % 2) * 12, "y": (i // 2) * 7, "w": 12, "h": 7},
                "options": {"mode": "html", "content": content},
            }
        )
    return {
        "uid": "service-catalog",
        "title": "Service catalog",
        "schemaVersion": 39,
        "version": 1,
        "editable": False,
        "tags": ["catalog"],
        "panels": panels,
    }


def compiled(entries):
    return {
        "charts/platform/files/dashboards/service-catalog.json": json.dumps(
            dashboard(entries), indent=2
        )
        + "\n",
        "charts/platform/files/rules/catalog.yaml": yaml.safe_dump(
            {
                "groups": [
                    {
                        "name": "service-catalog-slos",
                        "rules": [r for s in entries for r in slo_rules(s)],
                    }
                ]
                if any(s["slo"]["enabled"] for s in entries)
                else []
            },
            sort_keys=False,
        ),
        "catalog/index.json": json.dumps(
            {"apiVersion": "observability/v1", "services": entries}, indent=2
        )
        + "\n",
    }


def onboarding(service):
    return {
        "metadata": {"labels": {"observability/collect": "true"}},
        "podTemplate": {
            "metadata": {
                "labels": {
                    "observability/service": service["name"],
                    "observability/profile": str(service["profiling"]).lower(),
                    "app.kubernetes.io/name": service["name"],
                }
            },
            "env": [
                {
                    "name": "POD_NAMESPACE",
                    "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}},
                },
                {"name": "POD_UID", "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}}},
                {"name": "OTEL_SERVICE_NAME", "value": service["name"]},
                {
                    "name": "OTEL_RESOURCE_ATTRIBUTES",
                    "value": (
                        "service.namespace=$(POD_NAMESPACE),service.instance.id=$(POD_UID),"
                        "k8s.namespace.name=$(POD_NAMESPACE),k8s.pod.uid=$(POD_UID)"
                    ),
                },
                {
                    "name": "OTEL_EXPORTER_OTLP_ENDPOINT",
                    "value": "http://otel-agent.observability.svc:4318",
                },
                {"name": "OTEL_TRACES_SAMPLER", "value": "always_on"},
                {"name": "OTEL_LOGS_EXPORTER", "value": "none"},
            ],
        },
    }


def scaffold(service, output):
    validate_service(service)
    output = Path(output)
    if output.exists():
        raise ValueError("Output must not exist; never overwrite an application repository")
    shutil.copytree(ROOT / "templates/service", output)
    (output / "service.json").write_text(json.dumps(service, indent=2) + "\n")
    (output / "onboarding.yaml").write_text(yaml.safe_dump(onboarding(service), sort_keys=False))
    for path in output.rglob("*"):
        if path.is_file() and path.name not in ("service.json", "onboarding.yaml"):
            text = (
                path.read_text()
                .replace("__SERVICE_NAME__", service["name"])
                .replace("__NAMESPACE__", service["namespace"])
                .replace("__OWNER__", service["owner"])
                .replace("__PROFILE__", str(service["profiling"]).lower())
            )
            path.write_text(text)
    return output


def publish(directory, repository, confirmation):
    if (
        not re.fullmatch(r"[A-Za-z0-9-]+/[a-z][a-z0-9-]{1,61}[a-z0-9]", repository)
        or confirmation != repository
    ):
        raise ValueError("Publishing requires --confirm-publish matching owner/repository exactly")
    directory = Path(directory).resolve()
    service = validate_service(json.loads((directory / "service.json").read_text()))
    if service["repository"] != "https://github.com/" + repository:
        raise ValueError("Service repository URL must match the publish destination")
    # Require a reviewed existing commit. Never silently stage secrets or commit unknown files.
    top = subprocess.check_output(
        ["git", "-C", str(directory), "rev-parse", "--show-toplevel"], text=True
    ).strip()
    if (
        Path(top).resolve() != directory
        or subprocess.check_output(
            ["git", "-C", str(directory), "status", "--porcelain"], text=True
        ).strip()
    ):
        raise ValueError(
            "Initialize, review and commit the scaffold in its own clean repository first"
        )
    if subprocess.check_output(["git", "-C", str(directory), "remote"], text=True).strip():
        raise ValueError("Publish only a new repository without existing remotes")
    subprocess.run(
        [
            "gh",
            "repo",
            "create",
            repository,
            "--private",
            "--source",
            str(directory),
            "--remote",
            "origin",
            "--push",
        ],
        check=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("compile")
    build.add_argument("--check", action="store_true")
    build.add_argument("--directory", type=Path, default=ROOT / "catalog/services")
    new = sub.add_parser("scaffold")
    new.add_argument("--service", type=Path, required=True)
    new.add_argument("--output", type=Path, required=True)
    pub = sub.add_parser("publish")
    pub.add_argument("--directory", type=Path, required=True)
    pub.add_argument("--repository", required=True)
    pub.add_argument("--confirm-publish", required=True)
    args = parser.parse_args()
    if args.command == "compile":
        for file, content in compiled(load_catalog(args.directory)).items():
            dest = ROOT / file
            if args.check:
                if not dest.exists() or dest.read_text() != content:
                    raise ValueError(f"Stale generated content: {file}; run make catalog")
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(content)
    elif args.command == "scaffold":
        scaffold(json.loads(args.service.read_text()), args.output)
        print(f"Created {args.output}; review, test and commit before explicitly publishing.")
    else:
        publish(args.directory, args.repository, args.confirm_publish)


if __name__ == "__main__":
    main()
