#!/usr/bin/env python3
"""Exercise real partition ownership, backlog drain and writer group rebalancing.

Docker resizes the consumers explicitly; Kubernetes/KEDA reconciliation is a
separate cluster acceptance gate. Uses production writer config and SQL with
one replica per shard and one broker/Keeper to bound local test resources.
"""

import datetime as dt
import json
import subprocess
import time
import uuid

import yaml
from clickhouse_fixture import ClickHouseCluster, command, wait
from render import ROOT, render


def main():
    render()
    values = yaml.safe_load((ROOT / "charts/platform/values.yaml").read_text())
    partitions = values["scaling"]["logPartitions"]
    with ClickHouseCluster(replicas=1, insert_quorum=1) as cluster:
        cluster.grow(2)
        kafka_env = {
            "KAFKA_NODE_ID": "1",
            "KAFKA_PROCESS_ROLES": "broker,controller",
            "KAFKA_LISTENERS": "PLAINTEXT://:9092,CONTROLLER://:9093",
            "KAFKA_ADVERTISED_LISTENERS": "PLAINTEXT://kafka:9092",
            "KAFKA_LISTENER_SECURITY_PROTOCOL_MAP": "CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT",
            "KAFKA_CONTROLLER_QUORUM_VOTERS": "1@kafka:9093",
            "KAFKA_CONTROLLER_LISTENER_NAMES": "CONTROLLER",
            "KAFKA_INTER_BROKER_LISTENER_NAME": "PLAINTEXT",
            "KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR": "1",
            "KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS": "1000",
            "KAFKA_HEAP_OPTS": "-Xms256m -Xmx512m",
        }
        kafka = cluster.run(
            "kafka",
            "apache/kafka:4.2.1",
            *[arg for key, value in kafka_env.items() for arg in ["-e", f"{key}={value}"]],
        )

        def tool(name, *args, stdin=None):
            return command(
                [
                    "docker",
                    "exec",
                    "-i",
                    kafka,
                    f"/opt/kafka/bin/kafka-{name}.sh",
                    "--bootstrap-server",
                    "localhost:9092",
                    *args,
                ],
                **({"input": stdin} if stdin is not None else {}),
            )

        wait(lambda: tool("topics", "--list") is not None, "Kafka startup")
        tool(
            "topics",
            "--create",
            "--topic",
            "logs",
            "--partitions",
            str(partitions),
            "--replication-factor",
            "1",
        )
        config = yaml.safe_load((ROOT / "build/configs/vector-writer/vector.yaml").read_text())
        config["sources"]["kafka"]["bootstrap_servers"] = "kafka:9092"
        config["sinks"]["clickhouse"]["endpoint"] = "http://ch-0-0:8123"
        path = cluster.directory / "writer.yaml"
        path.write_text(yaml.safe_dump(config))

        def writer(number):
            return cluster.run(
                f"writer-{number}",
                values["images"]["vector"],
                "-e",
                "CLICKHOUSE_PASSWORD=validation",
                "-v",
                f"{path}:/etc/vector/vector.yaml:ro",
            )

        def produce(start, count):
            records = [
                {
                    "cluster_name": "hot-cluster",
                    "event_id": str(uuid.UUID(int=i + 1)),
                    "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "service_key": "apps/hot",
                    "service_name": "hot",
                    "namespace": "apps",
                    "level": "ERROR",
                    "message": f"event {i}",
                    "trace_id": "",
                }
                for i in range(start, start + count)
            ]
            tool(
                "console-producer",
                "--topic",
                "logs",
                "--property",
                "parse.key=true",
                "--property",
                "key.separator=|",
                stdin="\n".join(r["event_id"] + "|" + json.dumps(r) for r in records) + "\n",
            )

        def offsets():
            raw = tool("consumer-groups", "--describe", "--group", "clickhouse-writer-v1")
            return [
                line.split()
                for line in raw.splitlines()
                if line.startswith("clickhouse-writer-v1") and "logs" in line
            ]

        def stable(members, drained=False):
            rows = offsets()
            return (
                len(rows) == partitions
                and len({row[6] for row in rows if row[6] != "-"}) == members
                and all(row[6] != "-" for row in rows)
                and (not drained or all(row[5] == "0" for row in rows))
            )

        # Commit every partition once, then accumulate a known durable backlog.
        first = writer(0)
        produce(0, 3600)
        wait(lambda: stable(1, True), "single consumer drains and commits all partitions")
        command(["docker", "stop", "-t", "60", first])
        produce(3600, 12000)
        rows = offsets()
        assert sum(int(row[5]) for row in rows) == 12000, rows
        writers = [writer(i) for i in [1, 2, 3]]
        wait(lambda: stable(3, True), "three consumer members own partitions and drain backlog")
        assert cluster.sql("SELECT uniqExact(event_id) FROM observability.logs") == "15600"
        assert cluster.sql("SELECT count() FROM observability.logs") == "15600"
        # No offsets may be committed while ClickHouse is unable to acknowledge.
        paused = []
        try:
            for node in cluster.nodes.values():
                command(["docker", "pause", node])
                paused.append(node)
            produce(15600, 3600)
            time.sleep(8)  # exceed Vector's batch and Kafka commit intervals
            assert sum(int(row[5]) for row in offsets()) == 3600
        finally:
            for node in paused:
                command(["docker", "unpause", node])
        wait(lambda: stable(3, True), "backlog drains after ClickHouse resumes", seconds=180)
        # Graceful downscale must release partitions to the remaining consumers.
        command(["docker", "stop", "-t", "60", writers[-1]])
        produce(19200, 3600)
        wait(lambda: stable(2, True), "two remaining consumers drain every partition")
        assert cluster.sql("SELECT uniqExact(event_id) FROM observability.logs") == "22800"
        assert cluster.sql("SELECT sum(count) FROM observability.log_events") == "22800"
        for shard in range(2):
            assert (
                int(cluster.sql("SELECT count() FROM observability.logs_local", (shard, 0))) > 8000
            )
        print(
            f"PASS: {partitions} partitions, 12000 queued records, consumer group 1 -> 3 -> 2; "
            "all partitions assigned; offsets held during sink outage; "
            "22800 distinct events and aggregates "
            "stored across 2 shards. KEDA controller reconciliation not exercised."
        )


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        print(exc.stderr)
        raise
