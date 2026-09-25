#!/usr/bin/env bash
# Run explicitly through SSM on the dedicated Amazon Linux 2023 lab runner.
# Installs pinned client tools and an integrity-checked, nonsecret source bundle.
# Does not apply Terraform, install Helm releases, or delete resources.
set -euo pipefail
umask 077
if [[ $# != 5 ]]; then
  echo "Usage: lab_runner_bootstrap.sh ACCOUNT REGION CLUSTER ARTIFACT_BUCKET BUNDLE_SHA256" >&2
  exit 2
fi
lab_account=$1
lab_region=$2
lab_cluster=$3
lab_bucket=$4
lab_digest=$5
[[ "$lab_account" =~ ^[0-9]{12}$ && "$lab_region" =~ ^[a-z]{2}-[a-z]+-[0-9]$ ]]
[[ "$lab_cluster" =~ ^olly-lab-[a-z0-9-]+$ && "$lab_bucket" =~ ^[a-z0-9][a-z0-9.-]+[a-z0-9]$ ]]
[[ "$lab_digest" =~ ^[a-f0-9]{64}$ && $(uname -m) == x86_64 ]]
[[ $(aws sts get-caller-identity --query Account --output text --region "$lab_region") == "$lab_account" ]]
lab_workdir="/opt/$lab_cluster"
if [[ -e "$lab_workdir" ]]; then
  echo "Runner workspace already exists; inspect and reuse it deliberately." >&2
  exit 2
fi
lab_temp=$(mktemp -d)
trap 'rm -rf -- "$lab_temp"' EXIT
dnf install -y python3.11 python3.11-pip tar gzip
curl --fail --silent --show-error --location \
  https://get.helm.sh/helm-v4.2.3-linux-amd64.tar.gz -o "$lab_temp/helm.tar.gz"
printf '%s  %s\n' e9b88b4ee95b18c706839c28d3a0220e5bc470e9cd9262410c90793c45ff8b7c "$lab_temp/helm.tar.gz" | sha256sum --check --status
tar -xzf "$lab_temp/helm.tar.gz" -C "$lab_temp"
install -m 0755 "$lab_temp/linux-amd64/helm" /usr/local/bin/helm
curl --fail --silent --show-error --location \
  https://dl.k8s.io/release/v1.35.0/bin/linux/amd64/kubectl -o "$lab_temp/kubectl"
printf '%s  %s\n' a2e984a18a0c063279d692533031c1eff93a262afcc0afdc517375432d060989 "$lab_temp/kubectl" | sha256sum --check --status
install -m 0755 "$lab_temp/kubectl" /usr/local/bin/kubectl
aws s3 cp "s3://$lab_bucket/bundle.tar.gz" "$lab_temp/bundle.tar.gz" \
  --region "$lab_region" --only-show-errors
printf '%s  %s\n' "$lab_digest" "$lab_temp/bundle.tar.gz" | sha256sum --check --status
mkdir -m 0700 "$lab_workdir"
tar -xzf "$lab_temp/bundle.tar.gz" -C "$lab_workdir" --no-same-owner
cd "$lab_workdir"
python3.11 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check -r requirements-dev.txt
# A tracked-source bundle does not contain the ignored dependency archives.
# Render-only must have both locked chart dependency trees before installation.
.venv/bin/python scripts/helm_dependencies.py charts/operators charts/platform
aws eks update-kubeconfig --region "$lab_region" --name "$lab_cluster" \
  --kubeconfig "$lab_workdir/kubeconfig" --alias "$lab_cluster" >/dev/null
chmod 0600 "$lab_workdir/kubeconfig"
echo "Runner tools and verified source bundle are ready; deployment remains a separate step."
