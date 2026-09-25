"""Disposable real ClickHouse/Keeper cluster for sharding tests, with test-only credentials."""

import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def command(args, **kwargs):
    result = subprocess.run(args, check=True, capture_output=True, text=True, timeout=90, **kwargs)
    return result.stdout.strip()


def wait(probe, message, seconds=90):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if probe():
                return
        except (subprocess.CalledProcessError, ValueError):
            pass
        time.sleep(0.5)
    raise AssertionError(message)


class ClickHouseCluster:
    def __init__(self, replicas=3, insert_quorum=None):
        if insert_quorum is not None and (
            type(insert_quorum) is not int or not 1 <= insert_quorum <= replicas
        ):
            raise ValueError("fixture insert_quorum must be an integer within its replica count")
        self.insert_quorum = insert_quorum
        self.prefix = "olly-shards-" + uuid.uuid4().hex[:8]
        self.directory = ROOT / "build" / self.prefix
        self.directory.mkdir(parents=True)
        self.replicas = replicas
        self.shards = 0
        self.nodes = {}
        self.containers = []
        self.values = yaml.safe_load((ROOT / "charts/platform/values.yaml").read_text())
        self.load_chi((ROOT / "build/platform.yaml").read_text())

    def load_chi(self, rendered):
        self.chi = next(
            d for d in yaml.safe_load_all(rendered) if d and d["kind"] == "ClickHouseInstallation"
        )
        # Reduced-replica fixtures opt in explicitly; normal fixtures retain the
        # production profile verbatim. Reapply only this override after growth.
        if self.insert_quorum is not None:
            self.chi["spec"]["configuration"]["profiles"]["default/insert_quorum"] = (
                self.insert_quorum
            )

    def run(self, name, image, *options):
        result = command(
            [
                "docker",
                "run",
                "-d",
                "--name",
                self.prefix + "-" + name,
                "--network",
                self.prefix,
                "--network-alias",
                name,
                "--label",
                "observability.test=shards",
                *options,
                image,
            ]
        )
        self.containers.append(result)
        return result

    def __enter__(self):
        command(
            ["docker", "network", "create", "--label", "observability.test=shards", self.prefix]
        )
        keeper = self.directory / "keeper.xml"
        keeper.write_text(
            """<clickhouse><logger><level>warning</level><console>1</console></logger><listen_host>0.0.0.0</listen_host><keeper_server><tcp_port>9181</tcp_port><server_id>1</server_id><log_storage_path>/var/lib/clickhouse-keeper/log</log_storage_path><snapshot_storage_path>/var/lib/clickhouse-keeper/snapshots</snapshot_storage_path><raft_configuration><server><id>1</id><hostname>keeper</hostname><port>9234</port></server></raft_configuration></keeper_server></clickhouse>"""
        )
        try:
            self.run(
                "keeper",
                self.values["images"]["keeper"],
                "-v",
                f"{keeper}:/conf/keeper.xml:ro",
                "-e",
                "KEEPER_CONFIG=/conf/keeper.xml",
                "-e",
                "CLICKHOUSE_DATA_DIR=/var/lib/clickhouse-keeper",
            )
            return self
        except Exception:
            self.close()
            raise

    def sql(self, query, node=(0, 0), user="writer", stdin=None):
        return command(
            [
                "docker",
                "exec",
                "-i",
                self.nodes[node],
                "clickhouse-client",
                "--user",
                user,
                "--password",
                "validation",
                "--multiquery",
                "--query",
                query,
            ],
            **({"input": stdin} if stdin is not None else {}),
        )

    def xml(self, shard, replica):
        root = ET.Element("clickhouse")
        ET.SubElement(ET.SubElement(root, "logger"), "level").text = "warning"
        remote = ET.SubElement(ET.SubElement(root, "remote_servers"), "logs")
        ET.SubElement(remote, "secret").text = "fixture-cluster-secret"
        for s in range(self.shards):
            group = ET.SubElement(remote, "shard")
            # Altinity 0.27.3 writes StringBool.String() verbatim to this XML
            # element. Do not normalize unsupported server tokens in fixtures.
            ET.SubElement(group, "internal_replication").text = self.chi["spec"]["configuration"][
                "clusters"
            ][0]["layout"]["shards"][s]["internalReplication"]
            for r in range(self.replicas):
                copy = ET.SubElement(group, "replica")
                ET.SubElement(copy, "host").text = f"ch-{s}-{r}"
                ET.SubElement(copy, "port").text = "9000"
        zk = ET.SubElement(ET.SubElement(root, "zookeeper"), "node")
        ET.SubElement(zk, "host").text = "keeper"
        ET.SubElement(zk, "port").text = "9181"
        macros = ET.SubElement(root, "macros")
        ET.SubElement(macros, "shard").text = str(shard)
        ET.SubElement(macros, "replica").text = str(replica)
        ET.SubElement(root, "interserver_http_host").text = f"ch-{shard}-{replica}"
        return ET.tostring(root, encoding="unicode")

    def grow(self, shards):
        assert shards > self.shards
        self.shards = shards
        # Expansion must exercise the actual newly rendered shard settings too.
        rendered = command(
            [
                "helm",
                "template",
                "olly",
                str(ROOT / "charts/platform"),
                "-n",
                "observability",
                "--set",
                f"clickhouse.shards={shards}",
            ]
        )
        self.load_chi(rendered)
        for s in range(shards):
            for r in range(self.replicas):
                directory = self.directory / f"ch-{s}-{r}"
                directory.mkdir(exist_ok=True)
                (directory / "cluster.xml").write_text(self.xml(s, r))
                if (s, r) in self.nodes:
                    continue
                root = ET.Element("clickhouse")
                profiles = ET.SubElement(root, "profiles")
                for path, value in self.chi["spec"]["configuration"]["profiles"].items():
                    profile, setting = path.split("/", 1)
                    parent = profiles.find(profile)
                    if parent is None:
                        parent = ET.SubElement(profiles, profile)
                    ET.SubElement(parent, setting).text = str(value)
                # Authentication/roles are fixture-only. Production uses referenced Secrets.
                users = ET.SubElement(root, "users")
                reader = ET.SubElement(users, "reader")
                ET.SubElement(reader, "password").text = "validation"
                ET.SubElement(reader, "profile").text = "readonly"
                ET.SubElement(ET.SubElement(reader, "networks"), "ip").text = "::/0"
                ET.SubElement(reader, "quota").text = "default"
                (directory / "profiles.xml").write_text(ET.tostring(root, encoding="unicode"))
                node = self.run(
                    f"ch-{s}-{r}",
                    self.values["images"]["clickhouse"],
                    "--cpus",
                    "1",
                    "--memory",
                    "1g",
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
                self.nodes[s, r] = node
        for key in self.nodes:
            wait(lambda key=key: self.sql("SELECT 1", key) == "1", f"startup {key}")
            wait(
                lambda key=key: self.sql(
                    "SELECT count() FROM system.zookeeper WHERE path='/'", key
                ).isdigit(),
                "Keeper startup",
            )
            self.sql("SYSTEM RELOAD CONFIG", key)
            wait(
                lambda key=key: (
                    self.sql("SELECT count() FROM system.clusters WHERE cluster='logs'", key)
                    == str(shards * self.replicas)
                ),
                f"topology reload {key}",
            )
        self.sql((ROOT / "build/configs/clickhouse-schema/schema.sql").read_text())

    def sync(self):
        for key in self.nodes:
            for table in ["logs_local", "log_events_local"]:
                self.sql(f"SYSTEM SYNC REPLICA observability.{table}", key)

    def close(self):
        for container in reversed(self.containers):
            subprocess.run(["docker", "rm", "-fv", container], capture_output=True, timeout=30)
        subprocess.run(["docker", "network", "rm", self.prefix], capture_output=True, timeout=30)

    def __exit__(self, kind, value, traceback):
        if kind:
            if isinstance(value, subprocess.CalledProcessError):
                print(value.stderr)
            for node in self.containers:
                result = subprocess.run(
                    ["docker", "logs", "--tail", "8", node], capture_output=True, text=True
                )
                print(result.stdout + result.stderr)
        self.close()
