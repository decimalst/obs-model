#!/usr/bin/env python3
"""Local log-path acceptance with real Vector, Valkey, Kafka, and ClickHouse.

Uses one broker/database/Keeper to bound laptop cost. This does NOT certify AZ
failover. The production normalization, gateway/writer configs, SQL, and quota
image are used; only local discovery and replication topology are substituted.
"""

import datetime as dt
import json
import subprocess
import time
import urllib.request
import uuid

import yaml
from render import ROOT, render


def command(args, **kwargs):
    kwargs.setdefault("timeout", 30)
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs).stdout.strip()


def wait(probe, description, timeout=45):
    deadline = time.monotonic() + timeout
    while True:
        try:
            if probe():
                return
        except (subprocess.CalledProcessError, OSError):
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Timed out: " + description)
        time.sleep(min(1, remaining))


def diagnostic(label, probe):
    try:
        print(label + ":", probe(), flush=True)
    except Exception as error:
        print(label + " unavailable:", error, flush=True)


def delivery_diagnostics(sql, quota_addr, kafka):
    diagnostic(
        "Stored records by service",
        lambda: sql(
            "SELECT service_name,count(),uniqExact(event_id) "
            "FROM observability.logs GROUP BY service_name"
        ),
    )

    def quota_metrics():
        with urllib.request.urlopen(f"http://{quota_addr}/metrics", timeout=5) as response:
            return response.read().decode()

    diagnostic("Quota metrics", quota_metrics)
    diagnostic(
        "Kafka consumer offsets",
        lambda: command(
            [
                "docker",
                "exec",
                kafka,
                "/opt/kafka/bin/kafka-consumer-groups.sh",
                "--bootstrap-server",
                "localhost:9092",
                "--describe",
                "--group",
                "clickhouse-writer-v1",
            ]
        ),
    )


def main():
    render()
    prefix = "olly-test-" + uuid.uuid4().hex[:8]
    network = prefix
    containers = []
    sql, quota_addr, kafka = None, "", ""
    directory = ROOT / "build/integration"
    directory.mkdir(exist_ok=True, parents=True)
    values = yaml.safe_load((ROOT / "charts/platform/values.yaml").read_text())
    command(["docker", "network", "create", "--label", "observability.test=logs", network])

    def start(name, image, *options):
        full = prefix + "-" + name
        result = command(
            [
                "docker",
                "run",
                "-d",
                "--name",
                full,
                "--network",
                network,
                "--network-alias",
                name,
                "--label",
                "observability.test=logs",
                *options,
                image,
            ]
        )
        containers.append(result)
        return result

    try:
        start("valkey", "valkey/valkey:8.0.6-alpine")
        kafka = start(
            "telemetry-kafka-bootstrap",
            "apache/kafka:4.2.1",
            "-e",
            "KAFKA_NODE_ID=1",
            "-e",
            "KAFKA_PROCESS_ROLES=broker,controller",
            "-e",
            "KAFKA_LISTENERS=PLAINTEXT://:9092,CONTROLLER://:9093",
            "-e",
            "KAFKA_ADVERTISED_LISTENERS=PLAINTEXT://telemetry-kafka-bootstrap:9092",
            "-e",
            "KAFKA_LISTENER_SECURITY_PROTOCOL_MAP=CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT",
            "-e",
            "KAFKA_CONTROLLER_QUORUM_VOTERS=1@telemetry-kafka-bootstrap:9093",
            "-e",
            "KAFKA_CONTROLLER_LISTENER_NAMES=CONTROLLER",
            "-e",
            "KAFKA_INTER_BROKER_LISTENER_NAME=PLAINTEXT",
            "-e",
            "KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR=1",
            "-e",
            "KAFKA_HEAP_OPTS=-Xms256m -Xmx512m",
        )
        (directory / "keeper.xml").write_text(
            """<clickhouse><logger><level>warning</level><console>1</console></logger><listen_host>0.0.0.0</listen_host><keeper_server><tcp_port>9181</tcp_port><server_id>1</server_id><log_storage_path>/var/lib/clickhouse-keeper/log</log_storage_path><snapshot_storage_path>/var/lib/clickhouse-keeper/snapshots</snapshot_storage_path><raft_configuration><server><id>1</id><hostname>keeper</hostname><port>9234</port></server></raft_configuration></keeper_server></clickhouse>"""
        )
        keeper = command(
            [
                "docker",
                "run",
                "-d",
                "--network",
                network,
                "--network-alias",
                "keeper",
                "--label",
                "observability.test=logs",
                "-v",
                f"{directory}/keeper.xml:/conf/keeper.xml:ro",
                "-e",
                "KEEPER_CONFIG=/conf/keeper.xml",
                "-e",
                "CLICKHOUSE_DATA_DIR=/var/lib/clickhouse-keeper",
                values["images"]["keeper"],
            ]
        )
        containers.append(keeper)
        (directory / "cluster.xml").write_text(
            """<clickhouse><remote_servers><logs><secret>validation-cluster</secret><shard><internal_replication>true</internal_replication><replica><host>127.0.0.1</host><port>9000</port><user>writer</user><password>validation</password></replica></shard></logs></remote_servers><zookeeper><node><host>keeper</host><port>9181</port></node></zookeeper><macros><shard>01</shard><replica>one</replica></macros></clickhouse>"""
        )
        (directory / "profiles.xml").write_text(
            "<clickhouse><profiles><default><distributed_foreground_insert>1</distributed_foreground_insert>"
            "<insert_quorum>1</insert_quorum></default></profiles></clickhouse>"
        )
        ch = start(
            "clickhouse-observability",
            values["images"]["clickhouse"],
            "-e",
            "CLICKHOUSE_USER=writer",
            "-e",
            "CLICKHOUSE_PASSWORD=validation",
            "-e",
            "CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1",
            "-v",
            f"{directory}/cluster.xml:/etc/clickhouse-server/config.d/cluster.xml:ro",
            "-v",
            f"{directory}/profiles.xml:/etc/clickhouse-server/users.d/profiles.xml:ro",
        )

        def sql(query):
            return command(
                [
                    "docker",
                    "exec",
                    "-i",
                    ch,
                    "clickhouse-client",
                    "--user",
                    "writer",
                    "--password",
                    "validation",
                    "--multiquery",
                ],
                input=query,
            )

        wait(lambda: sql("SELECT 1") == "1", "ClickHouse startup")
        wait(
            lambda: (
                command(
                    [
                        "docker",
                        "exec",
                        kafka,
                        "/opt/kafka/bin/kafka-topics.sh",
                        "--bootstrap-server",
                        "localhost:9092",
                        "--list",
                    ]
                )
                is not None
            ),
            "Kafka startup",
        )
        command(
            [
                "docker",
                "exec",
                kafka,
                "/opt/kafka/bin/kafka-topics.sh",
                "--bootstrap-server",
                "localhost:9092",
                "--create",
                "--topic",
                "logs",
                "--partitions",
                "3",
                "--replication-factor",
                "1",
            ]
        )
        wait(
            lambda: sql("SELECT count() FROM system.zookeeper WHERE path = '/'") is not None,
            "Keeper quorum",
        )
        sql((ROOT / "build/configs/clickhouse-schema/schema.sql").read_text())
        for role in ["vector-gateway", "vector-writer"]:
            start(
                role,
                values["images"]["vector"],
                "-e",
                "CLICKHOUSE_PASSWORD=validation",
                "-v",
                f"{ROOT}/build/configs/{role}/vector.yaml:/etc/vector/vector.yaml:ro",
            )
        # Use the chart's actual integer environment strings, then lower only
        # event budgets for the bounded flood fixture.
        quota_workload = next(
            d
            for d in yaml.safe_load_all((ROOT / "build/platform.yaml").read_text())
            if d and d["kind"] == "Rollout" and d["metadata"]["name"] == "log-quota"
        )
        rendered_env = quota_workload["spec"]["template"]["spec"]["containers"][0]["env"]
        quota_env = {
            e["name"]: e["value"]
            for e in rendered_env
            if "value" in e and (e["name"].endswith("PER_SECOND") or e["name"].endswith("BURST"))
        }
        quota_env.update(
            EVENTS_PER_SECOND="1",
            EVENT_BURST="5",
            DEBUG_EVENTS_PER_SECOND="1",
            DEBUG_EVENT_BURST="2",
        )
        quota_args = [arg for k, v in quota_env.items() for arg in ["-e", f"{k}={v}"]]
        quota = start(
            "log-quota",
            "observability/log-quota:0.1.0",
            "-e",
            "REDIS_ADDR=valkey:6379",
            "-e",
            "REDIS_TLS=false",
            "-e",
            "VECTOR_URL=http://vector-gateway:8080",
            *quota_args,
            "-p",
            "127.0.0.1::8080",
        )
        config = yaml.safe_load((ROOT / "build/configs/vector-agent/vector.yaml").read_text())
        config["sources"]["kubernetes"] = {
            "type": "http_server",
            "address": "0.0.0.0:9001",
            "decoding": {"codec": "json"},
            "framing": {"method": "newline_delimited"},
        }
        (directory / "agent.yaml").write_text(yaml.safe_dump(config))
        agent = start(
            "vector-agent",
            values["images"]["vector"],
            "-v",
            f"{directory}/agent.yaml:/etc/vector/vector.yaml:ro",
            "-p",
            "127.0.0.1::9001",
        )
        quota_addr = command(["docker", "port", quota, "8080/tcp"])
        agent_addr = command(["docker", "port", agent, "9001/tcp"])
        wait(
            lambda: urllib.request.urlopen(f"http://{quota_addr}/ready", timeout=2).status == 200,
            "quota readiness",
        )
        now = dt.datetime.now(dt.timezone.utc)

        def event(service, level, tags=None):
            return {
                "message": json.dumps({"message": "integration", "level": level}),
                "kubernetes": {
                    "pod_namespace": "apps",
                    "pod_labels": {"observability/service": service},
                    "pod_annotations": tags or {},
                },
            }

        prefix_tag = "observability.good-olly.io/debug-"
        active = {
            prefix_tag + "from": (now - dt.timedelta(hours=1)).isoformat(),
            prefix_tag + "until": (now + dt.timedelta(hours=7)).isoformat(),
        }
        expired = {
            prefix_tag + "from": (now - dt.timedelta(hours=9)).isoformat(),
            prefix_tag + "until": (now - dt.timedelta(hours=1)).isoformat(),
        }
        events = [event("flood", "ERROR") for _ in range(20)] + [
            event("quiet", "INFO"),
            event("debug", "DEBUG", active),
            event("expired", "DEBUG", expired),
        ]
        body = "".join(json.dumps(e) + "\n" for e in events).encode()
        wait(
            lambda: (
                urllib.request.urlopen(
                    urllib.request.Request(f"http://{agent_addr}", data=b"", method="POST"),
                    timeout=2,
                ).status
                == 200
            ),
            "agent readiness",
        )
        with urllib.request.urlopen(
            urllib.request.Request(f"http://{agent_addr}", data=body, method="POST"), timeout=20
        ) as response:
            assert response.status == 200
        # Cold Kafka group assignment and sink retries share this bounded delivery
        # budget. Send the fixture once: replaying it would change quota admission.
        wait(
            lambda: int(sql("SELECT count() FROM observability.logs")) >= 7,
            "end-to-end delivery",
            timeout=120,
        )
        counts = dict(
            line.split("\t")
            for line in sql(
                "SELECT service_name,count() FROM observability.logs GROUP BY service_name"
            ).splitlines()
        )
        assert counts == {"debug": "1", "flood": "5", "quiet": "1"}, counts
        assert int(sql("SELECT sum(count) FROM observability.log_events")) == 7
        assert sql("SELECT uniqExact(cluster_name) FROM observability.logs") == "1"
        assert sql("SELECT any(cluster_name) FROM observability.logs") == values["clusterName"]
        assert sql("SELECT uniqExact(event_id) FROM observability.logs") == "7"
        # Source offsets must advance after ClickHouse acknowledges the batch.
        wait(
            lambda: (
                "clickhouse-writer-v1"
                in command(
                    [
                        "docker",
                        "exec",
                        kafka,
                        "/opt/kafka/bin/kafka-consumer-groups.sh",
                        "--bootstrap-server",
                        "localhost:9092",
                        "--list",
                    ]
                )
            ),
            "consumer group",
        )
        with urllib.request.urlopen(f"http://{quota_addr}/metrics", timeout=5) as response:
            metrics = response.read().decode()
        assert "log_quota_accepted_total 7\n" in metrics, metrics
        assert "log_quota_dropped_total 15\n" in metrics, metrics
        print(
            "PASS: Vector → shared quota → Vector → Kafka → Vector → ClickHouse: "
            "5 flood errors, 1 quiet log, 1 granted debug; expired debug absent; 7 derived events."
        )
    except Exception as error:
        # These are disposable fixtures. Preserve evidence of partial delivery
        # before cleanup, including quota rejection/upstream failures.
        delivery_diagnostics(sql, quota_addr, kafka)
        try:
            sql("SYSTEM FLUSH LOGS")
            print(
                sql(
                    "SELECT exception FROM system.query_log "
                    "WHERE type IN ('ExceptionWhileProcessing', 'ExceptionBeforeStart') "
                    "ORDER BY event_time DESC LIMIT 3"
                ),
                flush=True,
            )
        except Exception:
            pass
        if isinstance(error, subprocess.CalledProcessError):
            print(error.stderr, flush=True)
        for container in containers:
            result = subprocess.run(
                ["docker", "logs", "--tail", "20", container], capture_output=True, text=True
            )
            print(container, result.stdout, result.stderr, flush=True)
        raise
    finally:
        for container in reversed(containers):
            subprocess.run(["docker", "rm", "-fv", container], capture_output=True)
        subprocess.run(["docker", "network", "rm", network], capture_output=True)


if __name__ == "__main__":
    main()
