#!/usr/bin/env bash
set -euo pipefail
# Execute locally inside one database replica. The operator owns the default user.
pod=$(kubectl -n observability get pods -l clickhouse.altinity.com/chi=observability -o jsonpath='{.items[0].metadata.name}')
kubectl -n observability get configmap clickhouse-schema -o jsonpath='{.data.schema\.sql}' |
  kubectl -n observability exec -i "$pod" -- clickhouse-client --multiquery
