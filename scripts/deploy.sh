#!/usr/bin/env bash
# Deploy only after reviewing and applying the separate regional Terraform plan.
set -euo pipefail
if [[ $# != 2 ]]; then
  echo "Usage: scripts/deploy.sh build/REGION.yaml build/REGION.terraform.json" >&2
  exit 2
fi
values_file=$1
terraform_output=$2
python_bin=${PYTHON:-.venv/bin/python}
operators_values="${values_file%.yaml}.operators.yaml"
# Verify the real API endpoint/account, not the editable context display name.
"$python_bin" scripts/preflight.py --config "$terraform_output" \
  --values "$values_file" --operators-values "$operators_values"
"$python_bin" - "$values_file" <<'PY'
import sys,yaml
v=yaml.safe_load(open(sys.argv[1]))
assert '@sha256:' in v['images']['quota'], 'Publish and pin the log-quota image first'
assert not any('REPLACE' in str(x) for x in v.values()), 'Unresolved regional values'
PY
# A failed lookup must not be mistaken for a fresh install: disabling an
# existing OTel controller even briefly would remove its Helm-owned resources.
bootstrap_operators=$("$python_bin" - "$operators_values" <<'PY'
import json, subprocess, sys, yaml
with open(sys.argv[1]) as source:
    components = yaml.safe_load(source).get('components', {})
releases = json.loads(subprocess.run(
    ['helm', 'list', '-A', '--deployed', '--failed', '--pending', '--uninstalled',
     '--superseded', '--uninstalling', '--filter', '^operators$', '-o', 'json'],
    check=True, capture_output=True, text=True,
).stdout)
exists = any(r['name'] == 'operators' and r['namespace'] == 'observability-system'
             for r in releases)
print('yes' if not exists and components.get('cert-manager', True)
      and components.get('opentelemetry-operator', True) else 'no')
PY
)
# KEDA and Strimzi install RoleBindings in their watched namespace.
kubectl create namespace observability --dry-run=client -o yaml | kubectl apply -f -
"$python_bin" scripts/helm_dependencies.py charts/operators charts/platform
"$python_bin" scripts/bootstrap_crds.py --operators-values "$operators_values"
if [[ -s build/bootstrap-crds.yaml ]]; then
  kubectl apply --server-side -f build/bootstrap-crds.yaml
fi
# Cert-manager's templated CRDs and webhook must be ready before OTel creates
# Certificate/Issuer objects. Bootstrap the SAME release only on first install;
# an existing release always receives its full selected component configuration.
if [[ "$bootstrap_operators" == yes ]]; then
  helm upgrade --install operators charts/operators -n observability-system --create-namespace \
    --skip-crds --wait --timeout 15m -f "$operators_values" \
    --set components.opentelemetry-operator=false
fi
# Altinity CRDs have been canonicalized; its chart hook is disabled.
helm upgrade --install operators charts/operators -n observability-system --create-namespace \
  --skip-crds --wait --timeout 15m -f "$operators_values"
"$python_bin" scripts/seed_secrets.py --terraform-output "$terraform_output"
# Rollouts deliberately pause on upgrades. --wait here can deadlock on promotion;
# deployment readiness and data-path checks are explicit subsequent stages.
helm upgrade --install olly charts/platform -n observability -f "$values_file" --timeout 15m
kubectl -n observability wait --for=condition=Ready kafka/telemetry --timeout=15m
kubectl -n observability wait --for=jsonpath='{.status.status}'=Completed clickhouseinstallation/observability --timeout=15m
scripts/schema.sh
"$python_bin" scripts/smoke_test.py
