#!/usr/bin/env bash
# No data writes, topic creation, consumer group changes, or raw failure output.
set -euo pipefail
case "$1" in
  kafka|schema) mode=$1 ;;
  *) echo 'Unsupported startup gate' >&2; exit 2 ;;
esac
for ((attempt=1; attempt<=GATE_ATTEMPTS; attempt++)); do
  ready=false
  if [[ "$mode" == kafka ]]; then
    if description=$(timeout --kill-after=5s "${GATE_TIMEOUT_SECONDS}s" /opt/kafka/bin/kafka-topics.sh \
        --bootstrap-server telemetry-kafka-bootstrap:9092 --describe --topic logs \
        --command-config /startup/kafka-client.properties 2>/dev/null); then
      if printf '%s\n' "$description" | awk -v expected="$EXPECTED_PARTITIONS" \
          -f /startup/topic-ready.awk; then ready=true; fi
    fi
  else
    # from_env keeps the mounted writer credential out of command arguments.
    if timeout --kill-after=5s "${GATE_TIMEOUT_SECONDS}s" clickhouse-client \
        --config-file /startup/clickhouse-client.xml --host clickhouse-observability \
        --connect_timeout=5 --send_timeout=10 --receive_timeout=10 \
        --max_execution_time="$GATE_TIMEOUT_SECONDS" \
        --param_expected="$EXPECTED_REPLICAS" \
        < /startup/schema-ready.sql >/dev/null 2>&1; then ready=true; fi
  fi
  if [[ "$ready" == true ]]; then
    echo "Startup $mode gate passed"
    exit 0
  fi
  if ((attempt == 1 || attempt % 12 == 0)); then
    echo "Waiting for startup $mode gate ($attempt/$GATE_ATTEMPTS)"
  fi
  if ((attempt < GATE_ATTEMPTS)); then sleep "$GATE_DELAY_SECONDS"; fi
done
echo "Startup $mode gate exhausted; workload remains blocked" >&2
exit 1
