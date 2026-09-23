#!/usr/bin/env python3
"""Prove lag/replication metrics with the exact pinned Strimzi exporter and a local broker."""

import json
import re
import subprocess
import uuid

import yaml
from collector_test import command, request, wait
from render import ROOT


def main():
    name = "olly-exporter-" + uuid.uuid4().hex[:8]
    containers = []
    command(["docker", "network", "create", "--label", "observability.test=exporter", name])
    try:
        env = {
            "KAFKA_NODE_ID": "1",
            "KAFKA_PROCESS_ROLES": "broker,controller",
            "KAFKA_LISTENERS": "PLAINTEXT://:9092,CONTROLLER://:9093",
            "KAFKA_ADVERTISED_LISTENERS": "PLAINTEXT://kafka:9092",
            "KAFKA_LISTENER_SECURITY_PROTOCOL_MAP": "CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT",
            "KAFKA_CONTROLLER_QUORUM_VOTERS": "1@kafka:9093",
            "KAFKA_CONTROLLER_LISTENER_NAMES": "CONTROLLER",
            "KAFKA_INTER_BROKER_LISTENER_NAME": "PLAINTEXT",
            "KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR": "1",
            "KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS": "0",
            "KAFKA_HEAP_OPTS": "-Xms256m -Xmx512m",
        }
        broker = command(
            [
                "docker",
                "run",
                "-d",
                "--network",
                name,
                "--network-alias",
                "kafka",
                "--label",
                "observability.test=exporter",
                *[a for k, v in env.items() for a in ["-e", f"{k}={v}"]],
                "apache/kafka:4.2.1",
            ]
        )
        containers.append(broker)

        def kafka(tool, *args):
            return command(
                [
                    "docker",
                    "exec",
                    broker,
                    "/opt/kafka/bin/kafka-" + tool + ".sh",
                    "--bootstrap-server",
                    "kafka:9092",
                    *args,
                ]
            )

        wait(lambda: kafka("broker-api-versions"), "Kafka", 60)
        kafka(
            "topics",
            "--create",
            "--topic",
            "logs",
            "--partitions",
            "3",
            "--replication-factor",
            "1",
        )
        subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                broker,
                "/opt/kafka/bin/kafka-console-producer.sh",
                "--bootstrap-server",
                "kafka:9092",
                "--topic",
                "logs",
            ],
            input="\n".join(json.dumps({"event": n}) for n in range(13)) + "\n",
            text=True,
            check=True,
            capture_output=True,
            timeout=30,
        )
        kafka(
            "console-consumer",
            "--topic",
            "logs",
            "--group",
            "clickhouse-writer-v1",
            "--from-beginning",
            "--max-messages",
            "1",
            "--timeout-ms",
            "10000",
        )
        kafka(
            "consumer-groups",
            "--group",
            "clickhouse-writer-v1",
            "--topic",
            "logs",
            "--reset-offsets",
            "--to-earliest",
            "--execute",
        )
        image = yaml.safe_load((ROOT / "charts/platform/values.yaml").read_text())["images"][
            "kafkaExporter"
        ]
        exporter = command(
            [
                "docker",
                "run",
                "-d",
                "--network",
                name,
                "--label",
                "observability.test=exporter",
                "-p",
                "127.0.0.1::9404",
                "--entrypoint",
                "/opt/kafka-exporter/kafka_exporter",
                image,
                "--kafka.server=kafka:9092",
                "--topic.filter=^logs$",
                "--group.filter=^clickhouse-writer-v1$",
                "--web.listen-address=:9404",
            ]
        )
        containers.append(exporter)
        address = command(["docker", "port", exporter, "9404/tcp"])

        def found():
            text = request(address, "/metrics")
            lines = [
                line for line in text.splitlines() if line.startswith("kafka_consumergroup_lag{")
            ]
            lag = sum(float(line.rsplit(" ", 1)[1]) for line in lines)
            return lag == 13 and len(lines) == 3

        wait(found, "three partitions and 13 queued events", 45)
        text = request(address, "/metrics")
        assert re.search(
            r"^kafka_topic_partition_under_replicated_partition\{[^\n]+\} 0$", text, re.M
        )
        directory = ROOT / "build/support-metrics"
        directory.mkdir(exist_ok=True)
        (directory / "kafka.prom").write_text(text)
        print(
            "Pinned Kafka exporter reports 13 queued events across 3 partitions "
            "and healthy replication."
        )
    finally:
        for c in reversed(containers):
            subprocess.run(["docker", "rm", "-f", c], capture_output=True, check=False)
        subprocess.run(["docker", "network", "rm", name], capture_output=True, check=False)


if __name__ == "__main__":
    main()
