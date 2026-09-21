#!/usr/bin/env bash
set -euo pipefail
if [[ $# != 1 ]]; then echo 'Usage: scripts/promote.sh log-quota|vector-gateway|otel-metrics|otel-router' >&2; exit 2; fi
case "$1" in log-quota|vector-gateway|otel-metrics|otel-router) ;; *) echo 'Stateful and unknown components cannot use this workflow' >&2; exit 2;; esac
kubectl argo rollouts get rollout "$1" -n observability
kubectl argo rollouts promote "$1" -n observability
kubectl argo rollouts status "$1" -n observability --timeout 5m
