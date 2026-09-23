#!/usr/bin/env python3
"""Compile nonsecret security profiles; never read Secrets or contact a cluster/IdP."""

import argparse
import copy
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import jsonschema
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "schemas/security.schema.json").read_text())


def _url(value, *, loopback=False):
    parsed = urlsplit(value)
    if (
        any(c.isspace() or ord(c) < 32 for c in value)
        or any(c in value for c in ("$", "{", "}", "\\", '"', "'"))
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (parsed.scheme != "https" and not loopback)
        or (
            loopback
            and (parsed.scheme != "http" or parsed.hostname not in ("localhost", "127.0.0.1"))
        )
    ):
        raise ValueError("Security URLs must be plain HTTPS URLs without credentials or queries")
    try:
        if parsed.port is not None and parsed.port != (3000 if loopback else 443):
            raise ValueError("Use HTTPS port 443, or loopback port 3000")
    except ValueError as error:
        raise ValueError("Unsupported security URL port") from error
    return parsed


def validate_profile(profile):
    errors = list(jsonschema.Draft7Validator(SCHEMA).iter_errors(profile))
    if errors:
        # Do not echo invalid values, which might accidentally contain a credential.
        path = ".".join(str(p) for p in errors[0].absolute_path) or "profile"
        raise ValueError(f"Invalid security profile at {path}; check security.schema.json")
    p = copy.deepcopy(profile)
    access, auth = p["access"], p["authentication"]
    local = access["mode"] == "port-forward"
    public = _url(access["url"], loopback=local)
    if public.path not in ("", "/"):
        raise ValueError("Grafana security profiles currently require a root-path URL")
    if local:
        if set(access) != {"mode", "url"} or auth["mode"] != "local":
            raise ValueError("Port-forward profiles require local authentication and no ingress")
    elif not access.get("proxyNamespace"):
        raise ValueError("HTTPS profiles require the existing proxy/controller namespace")
    if access["mode"] == "ingress":
        if not access.get("ingressClassName") or not access.get("tlsSecretName"):
            raise ValueError("Ingress requires an existing class and TLS Secret")
    elif any(k in access for k in ("ingressClassName", "tlsSecretName")):
        raise ValueError("Ingress class and TLS Secret apply only to ingress mode")
    if auth["mode"] == "local":
        if set(auth) != {"mode"}:
            raise ValueError("Local authentication does not accept OAuth settings")
    else:
        needed = {
            "mode",
            "secretName",
            "authURL",
            "tokenURL",
            "userInfoURL",
            "scopes",
            "groupsClaim",
            "roles",
        }
        if not needed.issubset(auth):
            raise ValueError("OAuth requires explicit endpoints, Secret, scopes and group roles")
        for key in ("authURL", "tokenURL", "userInfoURL"):
            _url(auth[key])
        if "openid" not in auth["scopes"]:
            raise ValueError("OAuth profiles require the openid scope")
        if "offline_access" in auth["scopes"] and not auth.get("useRefreshToken", False):
            raise ValueError("offline_access requires useRefreshToken")
        groups = [g for role in auth["roles"].values() for g in role]
        if len(groups) != len(set(groups)):
            raise ValueError("A group may map to only one role")
    internal = p["internalTransport"]
    if internal["mode"] == "cluster-network":
        if set(internal) != {"mode"}:
            raise ValueError("Mesh settings require existing-mesh mode")
    else:
        if local or not all(internal.get(k) for k in ("provider", "grafanaClientIdentities")):
            raise ValueError("Existing mesh requires HTTPS ingress and explicit client identities")
        pattern = (
            r"[a-z0-9.-]+/ns/[a-z0-9-]+/sa/[a-z0-9-]+"
            if internal["provider"] == "istio"
            else r"[a-z0-9-]+\.[a-z0-9-]+\.serviceaccount\.identity\.[a-z0-9.-]+"
        )
        if not all(re.fullmatch(pattern, i) for i in internal["grafanaClientIdentities"]):
            raise ValueError("Mesh client identities must be exact provider-specific identities")
    return p


def load_profile(path):
    return validate_profile(yaml.safe_load(Path(path).read_text()))


def mesh_policy_examples(profile):
    """External-owner review artifacts, deliberately NOT part of Helm values/install."""
    internal = profile["internalTransport"]
    if internal["mode"] != "existing-mesh":
        return []
    selector = {"matchLabels": {"app.kubernetes.io/name": "grafana"}}
    metadata = {"name": "olly-grafana", "namespace": "observability"}
    if internal["provider"] == "istio":
        return [
            {
                "apiVersion": "security.istio.io/v1",
                "kind": "PeerAuthentication",
                "metadata": metadata,
                "spec": {"selector": selector, "mtls": {"mode": "STRICT"}},
            },
            {
                "apiVersion": "security.istio.io/v1",
                "kind": "AuthorizationPolicy",
                "metadata": metadata,
                "spec": {
                    "selector": selector,
                    "action": "ALLOW",
                    "rules": [
                        {
                            "from": [
                                {"source": {"principals": internal["grafanaClientIdentities"]}}
                            ],
                            "to": [{"operation": {"ports": ["3000"]}}],
                        }
                    ],
                },
            },
        ]
    return [
        {
            "apiVersion": "policy.linkerd.io/v1beta3",
            "kind": "Server",
            "metadata": metadata,
            "spec": {"podSelector": selector, "port": 3000, "proxyProtocol": "HTTP/1"},
        },
        {
            "apiVersion": "policy.linkerd.io/v1alpha1",
            "kind": "MeshTLSAuthentication",
            "metadata": metadata,
            "spec": {"identities": internal["grafanaClientIdentities"]},
        },
        {
            "apiVersion": "policy.linkerd.io/v1alpha1",
            "kind": "AuthorizationPolicy",
            "metadata": metadata,
            "spec": {
                "targetRef": {
                    "group": "policy.linkerd.io",
                    "kind": "Server",
                    "name": "olly-grafana",
                },
                "requiredAuthenticationRefs": [
                    {
                        "group": "policy.linkerd.io",
                        "kind": "MeshTLSAuthentication",
                        "name": "olly-grafana",
                    }
                ],
            },
        },
    ]


def compile_profile(profile):
    p = validate_profile(profile)
    access, auth, internal = p["access"], p["authentication"], p["internalTransport"]
    local = access["mode"] == "port-forward"
    ini = {
        "server": {"root_url": access["url"].rstrip("/") + "/"},
        "auth.anonymous": {"enabled": False},
        "auth.basic": {"enabled": auth["mode"] == "local"},
        "auth": {
            "disable_login_form": auth["mode"] != "local",
            "login_maximum_inactive_lifetime_duration": "1h",
            "login_maximum_lifetime_duration": "8h",
            "token_rotation_interval_minutes": 10,
        },
        "security": {"cookie_secure": not local, "cookie_samesite": "lax"},
        "users": {"allow_sign_up": False},
        "auth.generic_oauth": {"enabled": False},
    }
    grafana = {
        "service": {"type": "ClusterIP"},
        "ingress": {"enabled": False},
        "grafana.ini": ini,
        "envValueFrom": {
            "GF_SECURITY_SECRET_KEY": {
                "secretKeyRef": {"name": p["serverSecretName"], "key": "secret-key"}
            }
        },
    }
    prerequisites = {
        "namespace": "observability",
        "secrets": [{"name": p["serverSecretName"], "keys": ["secret-key"]}],
        "configMaps": [],
        "ingressClassName": access.get("ingressClassName"),
        "proxyNamespace": access.get("proxyNamespace"),
        "callbackURL": None,
        "externalPolicyRequired": internal["mode"] == "existing-mesh",
        "meshProvider": internal.get("provider"),
        "meshCRDs": [],
        "coverage": {
            "browserToGrafana": "loopback tunnel" if local else "external TLS termination",
            "proxyToGrafana": "plaintext within cluster",
            "otlpArrowVectorKafkaClickHousePeers": "unchanged; no transport encryption added",
            "tenantAuthentication": "unchanged; platform tenant header is not authentication",
            "hostAgentsAndProfilers": "unchanged; outside generated mesh policy",
            "grafanaDatabase": "existing TLS require mode; CA hostname verification not added",
        },
    }
    if not local:
        # Additive to the platform policy: allow only the explicitly selected proxy namespace.
        grafana["networkPolicy"] = {
            "enabled": True,
            "allowExternal": False,
            "explicitNamespacesSelector": {
                "matchLabels": {"kubernetes.io/metadata.name": access["proxyNamespace"]}
            },
        }
    if access["mode"] == "ingress":
        grafana["ingress"] = {
            "enabled": True,
            "ingressClassName": access["ingressClassName"],
            "hosts": [urlsplit(access["url"]).hostname],
            "tls": [
                {"secretName": access["tlsSecretName"], "hosts": [urlsplit(access["url"]).hostname]}
            ],
        }
        prerequisites["secrets"].append(
            {"name": access["tlsSecretName"], "keys": ["tls.crt", "tls.key"]}
        )
    if auth["mode"] == "oauth":
        groups = [
            group for role in ("Admin", "Editor", "Viewer") for group in auth["roles"].get(role, [])
        ]
        claim = auth["groupsClaim"]
        clauses = [
            f"contains({claim}[*], '{group}') && '{role}'"
            for role in ("Admin", "Editor", "Viewer")
            for group in auth["roles"].get(role, [])
        ]
        oauth = {
            "enabled": True,
            "name": "Organization SSO",
            "allow_sign_up": True,
            "client_id": "$__env{GF_AUTH_GENERIC_OAUTH_CLIENT_ID}",
            "client_secret": "$__env{GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET}",
            "auth_url": auth["authURL"],
            "token_url": auth["tokenURL"],
            "api_url": auth["userInfoURL"],
            "scopes": " ".join(auth["scopes"]),
            "groups_attribute_path": claim,
            "allowed_groups": " ".join(groups),
            "role_attribute_path": " || ".join(clauses) + " || ''",
            "role_attribute_strict": True,
            "allow_assign_grafana_admin": False,
            "skip_org_role_sync": False,
            "use_pkce": True,
            "use_refresh_token": auth.get("useRefreshToken", False),
            "tls_skip_verify_insecure": False,
        }
        ini["auth.generic_oauth"] = oauth
        grafana["envValueFrom"].update(
            {
                "GF_AUTH_GENERIC_OAUTH_CLIENT_" + kind.upper(): {
                    "secretKeyRef": {"name": auth["secretName"], "key": "client-" + kind}
                }
                for kind in ("id", "secret")
            }
        )
        prerequisites["secrets"].append(
            {"name": auth["secretName"], "keys": ["client-id", "client-secret"]}
        )
        prerequisites["callbackURL"] = access["url"].rstrip("/") + "/login/generic_oauth"
        if auth.get("caConfigMap"):
            grafana["extraConfigmapMounts"] = [
                {
                    "name": "oidc-ca",
                    "mountPath": "/etc/grafana/oidc-ca.pem",
                    "subPath": "ca.crt",
                    "configMap": auth["caConfigMap"],
                    "readOnly": True,
                    "optional": False,
                }
            ]
            oauth["tls_client_ca"] = "/etc/grafana/oidc-ca.pem"
            prerequisites["configMaps"].append({"name": auth["caConfigMap"], "keys": ["ca.crt"]})
    if internal["mode"] == "existing-mesh":
        provider = internal["provider"]
        grafana["podAnnotations"] = (
            {"sidecar.istio.io/inject": "true"}
            if provider == "istio"
            else {"linkerd.io/inject": "enabled"}
        )
        prerequisites["coverage"]["proxyToGrafana"] = (
            "mTLS + exact client identities ONLY after external policy enforcement and validation"
        )
        prerequisites["meshCRDs"] = (
            ["peerauthentications.security.istio.io", "authorizationpolicies.security.istio.io"]
            if provider == "istio"
            else [
                "servers.policy.linkerd.io",
                "meshtlsauthentications.policy.linkerd.io",
                "authorizationpolicies.policy.linkerd.io",
            ]
        )
    return {
        "values": {"grafana": grafana},
        "prerequisites": prerequisites,
        "externalPolicyExamples": mesh_policy_examples(p),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = compile_profile(load_profile(args.profile))
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name, text in {
        "values.yaml": yaml.safe_dump(result["values"], sort_keys=False),
        "prerequisites.json": json.dumps(result["prerequisites"], indent=2) + "\n",
        "external-policy-examples.yaml": yaml.safe_dump_all(result["externalPolicyExamples"]),
    }.items():
        path = args.output / name
        path.write_text(text)
        path.chmod(0o600)
    print("Compiled nonsecret values and external-owner prerequisites; nothing deployed.")


if __name__ == "__main__":
    main()
