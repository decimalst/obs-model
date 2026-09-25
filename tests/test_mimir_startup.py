import copy
import subprocess
import time
import unittest
from unittest.mock import patch

from scripts.mimir_startup_test import Fixture, local_config, reader_evidence


class MimirStartup(unittest.TestCase):
    def test_fixture_preserves_runtime_paths_ingest_mode_and_limits(self):
        rendered = {
            "common": {"storage": {"backend": "s3", "s3": {"endpoint": "example"}}},
            "blocks_storage": {
                "backend": "s3",
                "s3": {"bucket_name": "example"},
                "tsdb": {"dir": "/data/tsdb"},
            },
            "ingester": {"ring": {"tokens_file_path": "/data/tokens"}},
            "activity_tracker": {"filepath": "/active-query-tracker/activity.log"},
            "runtime_config": {"file": "/var/mimir/runtime.yaml"},
            "ingest_storage": {
                "enabled": True,
                "kafka": {
                    "address": "production:9092",
                    "topic": "mimir-ingest",
                    "auto_create_topic_enabled": False,
                },
            },
            "limits": {"max_global_series_per_user": 1000000},
            "memberlist": {"join_members": ["production"], "compression_enabled": False},
        }
        before = copy.deepcopy(rendered)
        result = local_config(rendered, ["mimir-ingester-a-0"])
        self.assertEqual(rendered, before)
        for key in ("ingester", "activity_tracker", "runtime_config", "limits"):
            self.assertEqual(result[key], before[key])
        self.assertEqual(result["blocks_storage"]["tsdb"], before["blocks_storage"]["tsdb"])
        self.assertEqual(result["blocks_storage"]["backend"], "filesystem")
        self.assertNotIn("s3", result["blocks_storage"])
        self.assertEqual(result["common"]["storage"], {"backend": "filesystem"})
        self.assertTrue(result["ingest_storage"]["enabled"])
        self.assertFalse(result["ingest_storage"]["kafka"]["auto_create_topic_enabled"])
        self.assertFalse(result["usage_stats"]["enabled"])

    @staticmethod
    def metrics(fetches="3", errors="0", partition="0", topic="mimir-ingest"):
        return (
            f'cortex_ingest_storage_reader_last_committed_offset{{partition="{partition}"}} -1\n'
            f'cortex_ingest_storage_reader_last_produced_offset_requests_total{{partition="{partition}",topic="{topic}"}} {fetches}\n'
            f'cortex_ingest_storage_reader_last_produced_offset_failures_total{{partition="{partition}",topic="{topic}"}} {errors}\n'
        )

    def test_readiness_alone_or_config_print_is_not_reader_evidence(self):
        for value in ("ready", "ingest_storage:\n  enabled: true", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                reader_evidence(value)
        self.assertEqual(
            reader_evidence(self.metrics()),
            {"partition": 0, "offsetRequests": 3, "offsetErrors": 0},
        )

    def test_inactive_failed_wrong_partition_and_nonfinite_metrics_rejected(self):
        for options in (
            {"fetches": "0"},
            {"errors": "3"},
            {"errors": "-1"},
            {"partition": "1"},
            {"topic": "wrong"},
            {"fetches": "NaN"},
            {"errors": "+Inf"},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                reader_evidence(self.metrics(**options))
        with self.assertRaises(ValueError):
            reader_evidence(self.metrics() + self.metrics())

    @patch("scripts.mimir_startup_test.subprocess.run")
    def test_timed_out_launch_is_registered_and_cleanup_continues_on_failure(self, run):
        fixture = object.__new__(Fixture)
        fixture.name = "fixture"
        fixture.containers = []
        fixture.deadline = time.monotonic() + 60
        run.side_effect = subprocess.TimeoutExpired("docker run", 1)
        with self.assertRaises(subprocess.TimeoutExpired):
            fixture.start("ingester", "grafana/mimir:3.2.0")
        self.assertEqual(fixture.containers, ["fixture-ingester"])
        run.reset_mock()
        run.side_effect = [
            subprocess.TimeoutExpired("docker rm", 1),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
        with self.assertRaisesRegex(RuntimeError, "fixture-ingester"):
            fixture.close()
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.args[0], ["docker", "network", "rm", "fixture"])


if __name__ == "__main__":
    unittest.main()
