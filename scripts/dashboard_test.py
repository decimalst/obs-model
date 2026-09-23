#!/usr/bin/env python3
"""Verify Git-provisioned RED dashboard loads and reloads in pinned Grafana.

Disposable localhost-only Grafana with SQLite and anonymous Viewer access.
Datasource backends are placeholders; actual RED query semantics are exercised
by red_test.py and Prometheus rule tests, not by this provisioning check.
"""

import json
import subprocess
import uuid

import yaml
from collector_test import command, request, wait
from render import ROOT, render


def main():
    render()
    name = "olly-dashboard-" + uuid.uuid4().hex[:8]
    directory = ROOT / "build" / name
    dashboard_dir = directory / "dashboards"
    config_dir = directory / "provisioning"
    (config_dir / "dashboards").mkdir(parents=True)
    (config_dir / "datasources").mkdir()
    dashboard_dir.mkdir()
    documents = {
        (d["kind"], d["metadata"]["name"]): d
        for d in yaml.safe_load_all((ROOT / "build/platform.yaml").read_text())
        if d
    }
    for filename, content in documents["ConfigMap", "olly-dashboards"]["data"].items():
        (dashboard_dir / filename).write_text(content)
    # Use the same provider contract as the production chart; adjust only fixture paths.
    values = yaml.safe_load((ROOT / "charts/platform/values.yaml").read_text())
    provider = values["grafana"]["dashboardProviders"]["dashboardproviders.yaml"]
    provider["providers"][0]["options"]["path"] = "/dashboards"
    (config_dir / "dashboards/provider.yaml").write_text(yaml.safe_dump(provider))
    (config_dir / "datasources/sources.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": 1,
                "datasources": [
                    {
                        "name": "Fixture metrics",
                        "uid": "mimir",
                        "type": "prometheus",
                        "url": "http://127.0.0.1:9090",
                    },
                    {
                        "name": "Fixture traces",
                        "uid": "tempo",
                        "type": "tempo",
                        "url": "http://127.0.0.1:3200",
                    },
                ],
            }
        )
    )
    image = next(
        c["image"]
        for c in documents["Deployment", "grafana"]["spec"]["template"]["spec"]["containers"]
        if c["name"] == "grafana"
    )
    container = None
    try:
        # Pull separately so first-run image transfer is not constrained by command's 40s timeout.
        subprocess.run(["docker", "pull", image], check=True, capture_output=True, timeout=300)
        container = command(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "--label",
                "observability.test=dashboards",
                "-p",
                "127.0.0.1::3000",
                "-e",
                "GF_AUTH_ANONYMOUS_ENABLED=true",
                "-e",
                "GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer",
                "-e",
                "GF_AUTH_DISABLE_LOGIN_FORM=true",
                "-e",
                "GF_ANALYTICS_REPORTING_ENABLED=false",
                "-e",
                "GF_ANALYTICS_CHECK_FOR_UPDATES=false",
                "-e",
                "GF_SECURITY_ADMIN_PASSWORD=local-fixture-only",
                "-v",
                f"{config_dir}:/etc/grafana/provisioning:ro",
                "-v",
                f"{dashboard_dir}:/dashboards:ro",
                image,
            ]
        )
        address = command(["docker", "port", container, "3000/tcp"])
        wait(lambda: request(address, "/api/health"), "Grafana readiness", 90)

        def dashboard():
            return json.loads(request(address, "/api/dashboards/uid/service-red"))

        wait(
            lambda: dashboard().get("dashboard", {}).get("uid") == "service-red", "RED provisioning"
        )
        catalog = json.loads(request(address, "/api/dashboards/uid/service-catalog"))
        assert catalog["meta"]["provisioned"] and not catalog["meta"]["canSave"]
        assert (
            catalog["dashboard"]["panels"]
            == json.loads(
                documents["ConfigMap", "olly-dashboards"]["data"]["service-catalog.json"]
            )["panels"]
        )
        support = json.loads(request(address, "/api/dashboards/uid/platform-support"))
        assert support["meta"]["provisioned"] and not support["meta"]["canSave"]
        assert (
            support["dashboard"]["panels"]
            == json.loads(
                documents["ConfigMap", "olly-dashboards"]["data"]["platform-support.json"]
            )["panels"]
        )
        actual = dashboard()
        assert actual["meta"]["provisioned"]
        assert not actual["meta"]["canSave"]
        expected = json.loads(documents["ConfigMap", "olly-dashboards"]["data"]["service-red.json"])
        assert actual["dashboard"]["panels"] == expected["panels"]
        assert actual["dashboard"]["templating"] == expected["templating"]
        # A projected ConfigMap/file update must be picked up without restarting Grafana.
        changed = dict(expected, title="Service RED reload fixture", version=2)
        (dashboard_dir / "service-red.json").write_text(json.dumps(changed))
        wait(lambda: dashboard()["dashboard"]["title"] == changed["title"], "dashboard reload", 45)
        print(
            f"PASS: {image} loads the RED, catalog and support dashboards and variables, "
            "keeps Viewer edits disabled, and reloads a file update without restart. "
            "Browser rendering and datasource queries are separate acceptance checks."
        )
    finally:
        if container:
            subprocess.run(["docker", "rm", "-fv", container], capture_output=True, timeout=30)


if __name__ == "__main__":
    main()
