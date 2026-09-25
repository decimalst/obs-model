#!/usr/bin/env python3
"""Disposable local Kafka/ClickHouse startup-gate tests. Never accesses AWS/Kubernetes."""

import subprocess
import uuid
from pathlib import Path

import yaml

try:
    from .clickhouse_fixture import ClickHouseCluster, command, wait
    from .render import render
except ImportError:
    from clickhouse_fixture import ClickHouseCluster, command, wait
    from render import render

ROOT = Path(__file__).resolve().parents[1]
GATES = ROOT / "charts/platform/files/startup"


def check_gate(network, image, mode, *, env=None, extra=None):
    args = [
        "docker",
        "run",
        "--rm",
        "--network",
        network,
        "--read-only",
        "--user",
        "10001:10001",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--tmpfs",
        "/tmp:rw,size=64m",
        "--memory",
        "512m",
        "--cpus",
        "1",
        "-v",
        f"{GATES}:/startup:ro",
        "-e",
        "GATE_ATTEMPTS=1",
        "-e",
        "GATE_DELAY_SECONDS=1",
        "-e",
        "GATE_TIMEOUT_SECONDS=20",
    ]
    for key, value in (env or {}).items():
        args.extend(["-e", f"{key}={value}"])
    args.extend(extra or [])
    args.extend(["--entrypoint", "/bin/bash", image, "/startup/wait-backend.sh", mode])
    return subprocess.run(args, capture_output=True, text=True, timeout=35)


def main():
    render()
    values = yaml.safe_load((ROOT / "charts/platform/values.yaml").read_text())
    prefix = "olly-startup-" + uuid.uuid4().hex[:8]
    containers = []
    command(["docker", "network", "create", "--label", "observability.test=startup", prefix])
    try:
        voters = ",".join(f"{i}@kafka-{i}:9093" for i in (1, 2, 3))
        for number in (1, 2, 3):
            args = [
                "docker",
                "run",
                "-d",
                "--network",
                prefix,
                "--network-alias",
                f"kafka-{number}",
                "--label",
                "observability.test=startup",
                "--memory",
                "768m",
                "--cpus",
                "1",
            ]
            if number == 1:
                args.extend(["--network-alias", "telemetry-kafka-bootstrap"])
            env = {
                "KAFKA_NODE_ID": number,
                "KAFKA_PROCESS_ROLES": "broker,controller",
                "KAFKA_LISTENERS": "PLAINTEXT://:9092,CONTROLLER://:9093",
                "KAFKA_ADVERTISED_LISTENERS": f"PLAINTEXT://kafka-{number}:9092",
                "KAFKA_LISTENER_SECURITY_PROTOCOL_MAP": "CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT",
                "KAFKA_CONTROLLER_QUORUM_VOTERS": voters,
                "KAFKA_CONTROLLER_LISTENER_NAMES": "CONTROLLER",
                "KAFKA_INTER_BROKER_LISTENER_NAME": "PLAINTEXT",
                "KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR": "3",
                "KAFKA_AUTO_CREATE_TOPICS_ENABLE": "false",
                "KAFKA_HEAP_OPTS": "-Xms128m -Xmx256m",
            }
            for key, value in env.items():
                args.extend(["-e", f"{key}={value}"])
            containers.append(command([*args, values["images"]["kafkaCli"]]))

        def topics(*args):
            return command(
                [
                    "docker",
                    "exec",
                    containers[0],
                    "/opt/kafka/bin/kafka-topics.sh",
                    "--bootstrap-server",
                    "telemetry-kafka-bootstrap:9092",
                    *args,
                ]
            )

        # Successful RF=3 creation proves the missing-topic case isn't just absent brokers.
        wait(
            lambda: (
                topics(
                    "--create",
                    "--if-not-exists",
                    "--topic",
                    "readiness",
                    "--partitions",
                    "1",
                    "--replication-factor",
                    "3",
                )
                is not None
            ),
            "three Kafka brokers",
            seconds=120,
        )
        env = {
            "EXPECTED_PARTITIONS": "3",
            "KAFKA_HEAP_OPTS": "-Xms32m -Xmx128m",
            "KAFKA_OPTS": "-Djava.io.tmpdir=/tmp -Dkafka.logs.dir=/tmp",
        }
        if check_gate(prefix, values["images"]["kafkaCli"], "kafka", env=env).returncode == 0:
            raise AssertionError("Missing logs topic passed")
        topics(
            "--create",
            "--topic",
            "logs",
            "--partitions",
            "3",
            "--replication-factor",
            "3",
            "--config",
            "min.insync.replicas=2",
        )
        wait(
            lambda: (
                check_gate(prefix, values["images"]["kafkaCli"], "kafka", env=env).returncode == 0
            ),
            "replicated logs topic gate",
            seconds=90,
        )
        if (
            check_gate(
                prefix,
                values["images"]["kafkaCli"],
                "kafka",
                env={**env, "EXPECTED_PARTITIONS": "4"},
            ).returncode
            == 0
        ):
            raise AssertionError("Undersized topic passed")
        print(
            "PASS: Kafka gate rejects missing/undersized topics "
            "and accepts three real ISR replicas."
        )
    finally:
        for container in reversed(containers):
            subprocess.run(["docker", "rm", "-fv", container], capture_output=True, timeout=30)
        subprocess.run(["docker", "network", "rm", prefix], capture_output=True, timeout=30)

    with ClickHouseCluster(replicas=1) as fleet:
        fleet.grow(2)
        address = command(
            [
                "docker",
                "inspect",
                "--format",
                "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                fleet.nodes[0, 0],
            ]
        )
        env = {"EXPECTED_REPLICAS": "2", "CLICKHOUSE_PASSWORD": "validation"}

        def schema(environment=env):
            return check_gate(
                fleet.prefix,
                values["images"]["clickhouse"],
                "schema",
                env=environment,
                extra=["--add-host", "clickhouse-observability:" + address],
            )

        result = schema()
        if result.returncode:
            raise AssertionError(
                "Complete two-shard schema failed the actual read-only client gate"
            )
        if schema({**env, "EXPECTED_REPLICAS": "3"}).returncode == 0:
            raise AssertionError("Missing expected replica passed")
        fleet.sql("DROP VIEW observability.log_events_mv", node=(1, 0))
        if schema().returncode == 0:
            raise AssertionError("Missing derived MV on second shard passed")
        fleet.sql("CREATE VIEW observability.log_events_mv AS SELECT 1", node=(1, 0))
        if schema().returncode == 0:
            raise AssertionError("Wrong derived MV engine on second shard passed")
        print(
            "PASS: schema gate accepts complete schema "
            "and rejects missing replica/MV or wrong engine."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
