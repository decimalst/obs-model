#!/usr/bin/env python3
"""Actual replicated/sharded inserts, cluster-aware queries, failures and expansion."""

import datetime as dt
import json
import subprocess
import uuid

from clickhouse_fixture import ClickHouseCluster, command, wait
from render import render


def rows(start, count, cluster):
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    return [
        {
            "cluster_name": cluster,
            "event_id": str(uuid.UUID(int=i + 1)),
            "timestamp": now,
            "service_key": "apps/hot",
            "service_name": "hot",
            "namespace": "apps",
            "level": "ERROR",
            "message": "fixture",
            "trace_id": "",
        }
        for i in range(start, start + count)
    ]


def insert(fleet, records):
    fleet.sql(
        "INSERT INTO observability.logs FORMAT JSONEachRow",
        stdin="\n".join(json.dumps(r) for r in records),
    )


def main():
    render()
    with ClickHouseCluster() as fleet:
        fleet.grow(2)
        insert(fleet, rows(0, 120, "cluster-a") + rows(120, 120, "cluster-b"))
        fleet.sync()
        for cluster in ["cluster-a", "cluster-b"]:
            # Even a single busy service in a single cluster uses BOTH shards.
            counts = [
                int(
                    fleet.sql(
                        "SELECT count() FROM observability.logs_local "
                        f"WHERE cluster_name='{cluster}'",
                        (s, 0),
                    )
                )
                for s in range(2)
            ]
            assert sum(counts) == 120 and all(25 < n < 95 for n in counts), counts
        assert fleet.sql("SELECT count() FROM observability.logs", user="reader") == "240"
        assert fleet.sql("SELECT count() FROM observability.log_events", user="reader") == "2"
        assert fleet.sql("SELECT sum(count) FROM observability.log_events", user="reader") == "240"
        assert (
            fleet.sql("SELECT count() FROM clusterAllReplicas('logs', observability.logs_local)")
            == "720"
        )
        # One lost replica in a shard must not prevent quorum writes or complete reads.
        command(["docker", "stop", fleet.nodes[1, 2]])
        insert(fleet, rows(240, 60, "cluster-a"))
        assert fleet.sql("SELECT count() FROM observability.logs", user="reader") == "300"
        command(["docker", "start", fleet.nodes[1, 2]])
        wait(lambda: fleet.sql("SELECT 1", (1, 2)) == "1", "replica restart")
        fleet.sync()
        before = [
            int(fleet.sql("SELECT count() FROM observability.logs_local", (s, 0))) for s in range(2)
        ]
        # Reconciliation expands the remote topology and creates schema on new nodes.
        fleet.grow(3)
        assert fleet.sql("SELECT count() FROM observability.logs_local", (2, 0)) == "0"
        assert fleet.sql("SELECT count() FROM observability.logs") == "300"
        insert(fleet, rows(300, 180, "cluster-b"))
        fleet.sync()
        after = [
            int(fleet.sql("SELECT count() FROM observability.logs_local", (s, 0))) for s in range(3)
        ]
        assert (
            sum(after) == 480 and all(after[s] > before[s] for s in range(2)) and after[2] > 20
        ), after
        assert fleet.sql("SELECT count() FROM observability.logs", user="reader") == "480"
        assert fleet.sql("SELECT sum(count) FROM observability.log_events", user="reader") == "480"
        # The schema hook must reject a legacy local table instead of claiming success.
        fleet.sql("RENAME TABLE observability.logs TO observability.logs_saved")
        fleet.sql("CREATE TABLE observability.logs (x UInt8) ENGINE=Memory")
        try:
            fleet.sql((fleet.directory.parent / "configs/clickhouse-schema/schema.sql").read_text())
        except subprocess.CalledProcessError as error:
            assert "Legacy log schema" in error.stderr, error.stderr
        else:
            raise AssertionError("Legacy schema was silently accepted")
        finally:
            fleet.sql(
                "DROP TABLE observability.logs; "
                "RENAME TABLE observability.logs_saved TO observability.logs"
            )
        # Select a new event definitely assigned to shard 3, then require its write to fail.
        candidates = rows(480, 100, "cluster-b")
        candidate_ids = ",".join("'" + row["event_id"] + "'" for row in candidates)
        chosen = fleet.sql(
            "SELECT arrayFilter(x -> cityHash64('cluster-b', 'apps/hot', toUUID(x)) % 3 = 2, ["
            + candidate_ids
            + "])[1]"
        )
        unavailable_record = next(r for r in candidates if r["event_id"] == chosen)
        # No silent partial answers when an ENTIRE shard is unavailable.
        for r in range(3):
            command(["docker", "stop", fleet.nodes[2, r]])
        try:
            fleet.sql("SELECT count() FROM observability.logs", user="reader")
        except subprocess.CalledProcessError:
            pass
        else:
            raise AssertionError("Unavailable shard was silently omitted")
        try:
            insert(fleet, [unavailable_record])
        except subprocess.CalledProcessError:
            pass
        else:
            raise AssertionError("Write acknowledged before an unavailable destination stored it")
        print(
            "PASS: 2 shards x 3 replicas; hot-service spreading; cluster-aware queries; "
            "quorum writes after replica loss; expansion to 3 shards; "
            "legacy schema rejected; unavailable-shard reads and writes fail closed."
        )


if __name__ == "__main__":
    main()
