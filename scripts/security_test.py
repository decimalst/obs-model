#!/usr/bin/env python3
"""Boot pinned Grafana with compiled SSO; no IdP contact or cloud infrastructure."""

import configparser
import json
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import yaml
from collector_test import command, wait
from security_config import ROOT, compile_profile, load_profile


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def main():
    profile = load_profile(ROOT / "examples/security-oidc-external-proxy.yaml")
    result = compile_profile(profile)
    container = None
    with tempfile.TemporaryDirectory(prefix="olly-security-") as tmp:
        directory = Path(tmp)
        values = directory / "values.yaml"
        values.write_text(yaml.safe_dump(result["values"]))
        rendered = subprocess.check_output(
            [
                "helm",
                "template",
                "olly",
                str(ROOT / "charts/platform"),
                "-n",
                "observability",
                "-f",
                str(values),
            ],
            text=True,
        )
        objects = {(d["kind"], d["metadata"]["name"]): d for d in yaml.safe_load_all(rendered) if d}
        ini = configparser.ConfigParser(interpolation=None)
        ini.read_string(objects["ConfigMap", "grafana"]["data"]["grafana.ini"])
        # Only fixture storage/provisioning differ. Auth settings are exactly chart-rendered.
        ini["database"] = {"type": "sqlite3", "path": "/var/lib/grafana/grafana.db"}
        ini["paths"]["provisioning"] = "/tmp/empty-provisioning"
        ini["analytics"] = {
            "reporting_enabled": "false",
            "check_for_updates": "false",
            "check_for_plugin_updates": "false",
        }
        ini["plugins"] = {"preinstall_disabled": "true"}
        with (directory / "grafana.ini").open("w") as stream:
            ini.write(stream)
        image = next(
            c["image"]
            for c in objects["Deployment", "grafana"]["spec"]["template"]["spec"]["containers"]
            if c["name"] == "grafana"
        )
        opener = urllib.request.build_opener(NoRedirect, urllib.request.ProxyHandler({}))

        def get(path, data=None, headers=None):
            req = urllib.request.Request("http://" + address + path, data=data)
            for key, value in (headers or {}).items():
                req.add_header(key, value)
            if data:
                req.add_header("Content-Type", "application/json")
            try:
                with opener.open(req, timeout=5) as response:
                    return response.status, response.headers, response.read()
            except urllib.error.HTTPError as error:
                return error.code, error.headers, error.read()

        try:
            name = "olly-security-" + uuid.uuid4().hex[:8]
            container = command(
                [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    name,
                    "--label",
                    "observability.test=security",
                    "-p",
                    "127.0.0.1::3000",
                    "-e",
                    "GF_AUTH_GENERIC_OAUTH_CLIENT_ID=local-fixture-client",
                    "-e",
                    "GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET=local-fixture-secret",
                    "-e",
                    "GF_SECURITY_ADMIN_PASSWORD=local-fixture-admin",
                    "-e",
                    "GF_SECURITY_SECRET_KEY=local-fixture-encryption-key",
                    "-v",
                    f"{directory}/grafana.ini:/etc/grafana/grafana.ini:ro",
                    image,
                ]
            )
            address = command(["docker", "port", container, "3000/tcp"])
            wait(lambda: get("/api/health")[0] == 200, "Grafana SSO startup", 90)
            assert get("/api/user")[0] == 401, "Anonymous API must be rejected"
            assert get(
                "/login", json.dumps({"user": "admin", "password": "local-fixture-admin"}).encode()
            )[0] in (400, 401, 403, 404), "Local password sign-in must be disabled"
            status, headers, body = get("/login/generic_oauth")
            assert status == 302
            target = urllib.parse.urlsplit(headers["Location"])
            query = urllib.parse.parse_qs(target.query)
            assert target.hostname == "identity.example.com"
            assert query["code_challenge_method"] == ["S256"]
            assert len(query["code_challenge"][0]) >= 40
            assert query["redirect_uri"] == [result["prerequisites"]["callbackURL"]]
            assert query["client_id"] == ["local-fixture-client"]
            assert "local-fixture-secret" not in str(headers) + body.decode()
            # Trust is exercised on token exchange, not authorization URL construction.
            # Loopback fixture token URL guarantees no external provider is contacted.
            ini["auth.generic_oauth"]["tls_client_ca"] = "/no-such-ca.pem"
            ini["auth.generic_oauth"]["token_url"] = "https://127.0.0.1:1/token"
            with (directory / "grafana.ini").open("w") as stream:
                ini.write(stream)
            command(["docker", "restart", container])
            address = command(["docker", "port", container, "3000/tcp"])
            wait(lambda: get("/api/health")[0] == 200, "Grafana invalid-CA startup", 90)
            ca_status, ca_headers, _ = get("/login/generic_oauth")
            assert ca_status == 302
            ca_query = urllib.parse.parse_qs(urllib.parse.urlsplit(ca_headers["Location"]).query)
            cookies = "; ".join(v.split(";", 1)[0] for v in ca_headers.get_all("Set-Cookie"))
            failure, _, _ = get(
                "/login/generic_oauth?"
                + urllib.parse.urlencode(
                    {
                        "code": "local-fixture-code",
                        "state": ca_query["state"][0],
                    }
                ),
                headers={"Cookie": cookies},
            )
            assert failure != 200, "Invalid trust must not complete login"
            logs = command(["docker", "logs", container])
            assert "/no-such-ca.pem" in logs and "no such file or directory" in logs, (
                "The callback must reject the missing CA before token exchange"
            )
            # Exercise the explicit local recovery profile using the same disposable database.
            local = compile_profile(load_profile(ROOT / "examples/security-local.yaml"))
            for section, values in local["values"]["grafana"]["grafana.ini"].items():
                ini[section] = {
                    key: str(value).lower() if isinstance(value, bool) else str(value)
                    for key, value in values.items()
                }
            with (directory / "grafana.ini").open("w") as stream:
                ini.write(stream)
            command(["docker", "restart", container])
            address = command(["docker", "port", container, "3000/tcp"])
            wait(lambda: get("/api/health")[0] == 200, "Grafana local recovery startup", 90)
            assert get("/api/user")[0] == 401
            assert (
                get(
                    "/login",
                    json.dumps({"user": "admin", "password": "local-fixture-admin"}).encode(),
                )[0]
                == 200
            ), "Local profile must retain explicit admin recovery"
            print(
                f"PASS: {image} starts with compiled OAuth, rejects anonymous/local-password "
                "access, emits the exact HTTPS callback and PKCE challenge, rejects missing "
                "CA trust and supports explicit local recovery. "
                "No redirect followed; real IdP/group/TLS/mesh acceptance remains deferred."
            )
        finally:
            if container:
                subprocess.run(["docker", "rm", "-fv", container], capture_output=True, timeout=30)


if __name__ == "__main__":
    main()
