#!/usr/bin/env python3
"""Start rendered Mimir ingesters against disposable local Kafka, without AWS.

Three logical zones consume partition zero. Only Kafka/DNS coordinates, local
object storage and outbound usage reporting are substituted. A single broker
with replication one bounds fixture resources; this does not test Kafka quorum,
Strimzi reconciliation, EBS, Pod Identity, S3, or stored metric delivery.
"""

import copy
import json
import math
import re
import subprocess
import time
import uuid
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TIMEOUT = 300


def local_config(rendered, aliases):
    config = copy.deepcopy(rendered)
    config["common"]["storage"] = {"backend": "filesystem"}
    config["blocks_storage"].pop("s3", None)
    config["blocks_storage"].update(
        backend="filesystem", filesystem={"dir": "/data/fixture-blocks"}
    )
    config["memberlist"]["join_members"] = [name + ":7946" for name in aliases]
    config["ingest_storage"]["kafka"]["address"] = "kafka:9092"
    config.setdefault("usage_stats", {})["enabled"] = False
    return config


def reader_evidence(metrics):
    """Require live Kafka offset polling by partition zero, not config output.

    An empty topic has no records to consume, so its record-fetch counter can
    remain zero even with a healthy reader. Offset polling still reaches Kafka.
    """

    def samples(name):
        found = []
        for labels, value in re.findall(
            rf"^{name}(\{{[^\n]*\}})?\s+([^\s]+)$", metrics, flags=re.MULTILINE
        ):
            number = float(value)
            if not math.isfinite(number):
                raise ValueError("Nonfinite partition-reader metric")
            found.append((labels, number))
        if len(found) != 1:
            raise ValueError("Missing or ambiguous partition-reader metric: " + name)
        return found[0]

    labels, offset = samples("cortex_ingest_storage_reader_last_committed_offset")
    if not re.search(r'(?:\{|,)partition="0"(?:,|\})', labels) or offset < -1:
        raise ValueError("Expected the partition-zero offset reader")
    labels, requests = samples("cortex_ingest_storage_reader_last_produced_offset_requests_total")
    if not re.search(r'(?:\{|,)partition="0"(?:,|\})', labels) or not re.search(
        r'(?:\{|,)topic="mimir-ingest"(?:,|\})', labels
    ):
        raise ValueError("Expected polling of the exact Mimir topic and partition")
    _, errors = samples("cortex_ingest_storage_reader_last_produced_offset_failures_total")
    if requests <= 0 or errors != 0:
        raise ValueError("Partition reader has not polled Kafka successfully")
    return {"offsetRequests": requests, "offsetErrors": errors, "partition": 0}


class Fixture:
    def __init__(self):
        self.name = "olly-mimir-startup-" + uuid.uuid4().hex[:8]
        self.directory = ROOT / "build" / self.name
        self.directory.mkdir(parents=True)
        self.containers = []
        self.deadline = time.monotonic() + TIMEOUT

    def command(self, args):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Mimir startup fixture exceeded its time budget")
        return subprocess.run(
            args, check=True, capture_output=True, text=True, timeout=min(40, remaining)
        ).stdout.strip()

    def start(self, alias, image, args=(), options=()):
        name = self.name + "-" + alias
        # Register before docker run: a client timeout can leave a created container.
        self.containers.append(name)
        self.command(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "--network",
                self.name,
                "--network-alias",
                alias,
                "--hostname",
                alias,
                "--label",
                "observability.test=mimir-startup",
                *options,
                image,
                *args,
            ]
        )
        return name

    def wait(self, probe, description, seconds=150):
        deadline = min(self.deadline, time.monotonic() + seconds)
        while time.monotonic() < deadline:
            try:
                result = probe()
                if result:
                    return result
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
            time.sleep(0.5)
        raise AssertionError("Timed out: " + description)

    def close(self):
        # Cleanup has its own bounded budget even after startup's deadline expires.
        failures = []
        for args in [
            *[["docker", "rm", "-fv", name] for name in reversed(self.containers)],
            ["docker", "network", "rm", self.name],
        ]:
            try:
                result = subprocess.run(args, capture_output=True, text=True, timeout=15)
                if result.returncode and not any(
                    missing in result.stderr for missing in ("No such", "not found")
                ):
                    failures.append(args[-1])
            except subprocess.TimeoutExpired:
                failures.append(args[-1])
        if failures:
            raise RuntimeError("Fixture cleanup failed for: " + ", ".join(failures))


def main():
    fixture = Fixture()
    try:
        rendered = fixture.command(
            ["helm", "template", "olly", str(ROOT / "charts/platform"), "-n", "observability"]
        )
        objects = [d for d in yaml.safe_load_all(rendered) if d]
        kafka_resource = next(d for d in objects if d["kind"] == "Kafka")
        assert kafka_resource["spec"]["kafka"]["version"] == "4.2.1"
        maps = {d["metadata"]["name"]: d["data"] for d in objects if d["kind"] == "ConfigMap"}
        ingesters = [
            d
            for d in objects
            if d["kind"] == "StatefulSet" and d["metadata"]["name"].startswith("mimir-ingester-")
        ]
        assert len(ingesters) == 3 and all(d["spec"]["replicas"] == 1 for d in ingesters)
        aliases = [d["metadata"]["name"] + "-0" for d in ingesters]
        config = local_config(yaml.safe_load(maps["mimir-config"]["mimir.yaml"]), aliases)
        (fixture.directory / "mimir.yaml").write_text(yaml.safe_dump(config))
        (fixture.directory / "runtime.yaml").write_text(maps["mimir-runtime"]["runtime.yaml"])
        fixture.command(
            [
                "docker",
                "network",
                "create",
                "--internal",
                "--label",
                "observability.test=mimir-startup",
                fixture.name,
            ]
        )
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
            "KAFKA_AUTO_CREATE_TOPICS_ENABLE": "false",
            "KAFKA_HEAP_OPTS": "-Xms256m -Xmx512m",
        }
        broker = fixture.start(
            "kafka",
            "apache/kafka:4.2.1",
            options=[arg for key, value in env.items() for arg in ["-e", f"{key}={value}"]],
        )

        def kafka(*args):
            return fixture.command(
                [
                    "docker",
                    "exec",
                    broker,
                    "/opt/kafka/bin/kafka-topics.sh",
                    "--bootstrap-server",
                    "kafka:9092",
                    *args,
                ]
            )

        fixture.wait(lambda: kafka("--list") is not None, "Kafka startup", 75)
        kafka(
            "--create",
            "--topic",
            config["ingest_storage"]["kafka"]["topic"],
            "--partitions",
            "1",
            "--replication-factor",
            "1",
        )
        for deployment, alias in zip(ingesters, aliases, strict=True):
            pod = deployment["spec"]["template"]["spec"]
            container = next(c for c in pod["containers"] if c["name"] == "ingester")
            assert pod["securityContext"]["runAsUser"] == 10001
            assert pod["securityContext"]["fsGroup"] == 10001
            assert container["securityContext"]["readOnlyRootFilesystem"] is True
            assert "-print.config" not in container["args"]
            fixture.start(
                alias,
                container["image"],
                container["args"],
                options=[
                    "--user",
                    "10001:10001",
                    "--read-only",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--memory",
                    "4g",
                    "--tmpfs",
                    "/data:rw,uid=10001,gid=10001,mode=0770",
                    "--tmpfs",
                    "/active-query-tracker:rw,uid=10001,gid=10001,mode=0770",
                    "-v",
                    f"{fixture.directory}:/etc/mimir:ro",
                    "-v",
                    f"{fixture.directory}:/var/mimir:ro",
                    "-e",
                    "GOMAXPROCS=4",
                ],
            )

        support = next(
            d
            for d in objects
            if d["kind"] == "Deployment" and d["metadata"]["name"] == "olly-support"
        )
        probe = fixture.start(
            "probe",
            support["spec"]["template"]["spec"]["containers"][0]["image"],
            ["python", "-c", f"import time; time.sleep({TIMEOUT})"],
            options=[
                "--user",
                "10001:10001",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
            ],
        )

        def fetch(alias, path):
            return fixture.command(
                [
                    "docker",
                    "exec",
                    probe,
                    "python",
                    "-c",
                    "import sys, urllib.request; "
                    "print(urllib.request.urlopen(sys.argv[1], timeout=3).read().decode())",
                    "http://" + alias + ":8080" + path,
                ]
            )

        def ready():
            evidence = {}
            for alias in aliases:
                if fetch(alias, "/ready").strip() != "ready":
                    return None
                evidence[alias] = reader_evidence(fetch(alias, "/metrics"))
            return evidence

        before = fixture.wait(ready, "all three ingesters ready with active partition readers")

        def advancing():
            after = ready()
            return (
                after
                if after
                and all(
                    after[name]["offsetRequests"] > before[name]["offsetRequests"]
                    and after[name]["offsetErrors"] == before[name]["offsetErrors"]
                    for name in aliases
                )
                else None
            )

        evidence = fixture.wait(advancing, "every partition reader continues polling Kafka", 30)
        offsets = kafka("--describe", "--topic", "__consumer_offsets")
        assert "ReplicationFactor: 1" in offsets and "Leader: -1" not in offsets, offsets
    except BaseException:
        # These containers contain only synthetic fixture data and no credentials.
        for name in fixture.containers:
            try:
                logs = subprocess.run(
                    ["docker", "logs", "--tail", "12", name],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                print(name + "\n" + logs.stdout + logs.stderr)
            except subprocess.TimeoutExpired:
                pass
        raise
    finally:
        fixture.close()
    print(
        json.dumps(
            {
                "nativeStartupPassed": True,
                "cleanupPassed": True,
                "ingesters": evidence,
                "scope": "local startup only; no AWS, quorum or stored-signal proof",
            }
        )
    )


if __name__ == "__main__":
    main()
