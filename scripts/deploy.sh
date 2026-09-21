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
# Verify the real API endpoint/account, not the editable context display name.
"$python_bin" scripts/preflight.py --config "$terraform_output" \
  --values "$values_file" --operators-values "${values_file%.yaml}.operators.yaml"
"$python_bin" - "$values_file" <<'PY'
import sys,yaml
v=yaml.safe_load(open(sys.argv[1]))
assert '@sha256:' in v['images']['quota'], 'Publish and pin the log-quota image first'
assert not any('REPLACE' in str(x) for x in v.values()), 'Unresolved regional values'
PY
helm dependency build charts/operators
helm dependency build charts/platform
"$python_bin" scripts/bootstrap_crds.py --operators-values "${values_file%.yaml}.operators.yaml"
if [[ -s build/bootstrap-crds.yaml ]]; then
  kubectl apply --server-side -f build/bootstrap-crds.yaml
fi
# Altinity CRDs have been canonicalized; its chart hook is disabled.
helm upgrade --install operators charts/operators -n observability-system --create-namespace \
  --skip-crds --wait --timeout 15m -f "${values_file%.yaml}.operators.yaml"
kubectl create namespace observability --dry-run=client -o yaml | kubectl apply -f -
"$python_bin" scripts/seed_secrets.py --terraform-output "$terraform_output"
# Rollouts deliberately pause on upgrades. --wait here can deadlock on promotion;
# deployment readiness and data-path checks are explicit subsequent stages.
helm upgrade --install olly charts/platform -n observability -f "$values_file" --timeout 15m
kubectl -n observability wait --for=condition=Ready kafka/telemetry --timeout=15m
kubectl -n observability wait --for=jsonpath='{.status.status}'=Completed clickhouseinstallation/observability --timeout=15m
scripts/schema.sh
"$python_bin" scripts/smoke_test.py
