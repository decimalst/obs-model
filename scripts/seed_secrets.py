#!/usr/bin/env python3
"""Copy AWS-managed secrets into Kubernetes without printing secret material.

Run only from the authorized private deployment runner after Terraform apply.
Existing generated application secrets are preserved; never rotate them implicitly.
"""

import argparse
import json
import secrets
import subprocess

try:
    from .platform_config import load_platform
except ImportError:
    from platform_config import load_platform


def command(args, data=None):
    return subprocess.run(args, input=data, capture_output=True, text=True, check=True).stdout


def apply_secret(name, data, namespace="observability", preserve=False):
    found = subprocess.run(
        ["kubectl", "-n", namespace, "get", "secret", name, "--ignore-not-found", "-o", "name"],
        capture_output=True,
        text=True,
        check=True,
    )
    if preserve and found.stdout.strip():
        return
    obj = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": namespace},
        "type": "Opaque",
        "stringData": data,
    }
    # Server-side apply avoids storing a second secret copy in last-applied annotations.
    command(
        ["kubectl", "apply", "--server-side", "--field-manager=observability-secrets", "-f", "-"],
        json.dumps(obj),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--terraform-output", required=True)
    args = parser.parse_args()
    platform = load_platform(args.terraform_output)
    if platform["secret_source"] == "existing":
        print("Using pre-created Kubernetes secrets; no secrets were changed.")
        return

    def aws_secret(arn):
        return command(
            [
                "aws",
                "secretsmanager",
                "get-secret-value",
                "--region",
                platform["region"],
                "--secret-id",
                arn,
                "--query",
                "SecretString",
                "--output",
                "text",
            ]
        ).strip()

    database = json.loads(aws_secret(platform["grafana_db_secret_arn"]))
    apply_secret(
        "grafana-db",
        {
            "GF_DATABASE_HOST": platform["grafana_db_host"] + ":5432",
            "GF_DATABASE_PASSWORD": database["password"],
        },
    )
    apply_secret("quota-redis", {"password": aws_secret(platform["quota_secret_arn"])})
    apply_secret(
        "grafana-admin",
        {"admin-user": "admin", "admin-password": secrets.token_urlsafe(32)},
        preserve=True,
    )
    apply_secret("clickhouse-auth", {"password": secrets.token_hex(32)}, preserve=True)
    print("Required secrets are present; values were not printed.")


if __name__ == "__main__":
    main()
