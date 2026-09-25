import argparse
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import lab_shared_quota as lab


class SharedQuotaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.args = argparse.Namespace(
            context="olly-lab-test",
            cluster="olly-lab-test",
            account="123456789012",
            region="us-east-2",
            confirm_cluster_arn="arn:aws:eks:us-east-2:123456789012:cluster/olly-lab-test",
            output=str(self.root / "evidence.json"),
            timeout=300,
        )
        self.pods = {
            "quota-" + str(index): {
                "uid": f"12345678-1234-1234-1234-12345678901{index}",
                "ip": f"10.72.16.{index + 3}",
                "restarts": 0,
            }
            for index in range(3)
        }
        self.before = {
            pod: {"accepted": 100, "dropped": 200, "unavailable": 0} for pod in self.pods
        }
        self.after = copy.deepcopy(self.before)
        for index, values in enumerate(self.after.values()):
            accepted = 40 if index == 0 else 0
            values["accepted"] += accepted
            values["dropped"] += 100 - accepted
        self.burst = {"statuses": dict.fromkeys(self.pods, 200), "elapsedSeconds": 0.01}
        self.argument = {
            "cluster": self.args.cluster,
            "service": "lab-shared-" + "a" * 32,
            "marker": "lab-shared-quota:" + "a" * 32 + ":",
            "addresses": {pod: state["ip"] for pod, state in self.pods.items()},
        }

    def test_shared_global_bound_rejects_three_independent_bursts(self):
        bound = lab.burst_bound(self.burst, self.pods)
        self.assertEqual(bound, 42)
        delta = lab.quota_delta(self.before, self.after, self.pods, bound)
        self.assertEqual(delta, {"accepted": 40, "dropped": 260, "unavailable": 0})
        lab.stored_evidence(40, delta, bound)
        multiplied = copy.deepcopy(self.before)
        for values in multiplied.values():
            values["accepted"] += 40
            values["dropped"] += 60
        with self.assertRaisesRegex(lab.Rejected, "exceed the shared bound"):
            lab.quota_delta(self.before, multiplied, self.pods, bound)
        with self.assertRaisesRegex(lab.Rejected, "exceed the shared bound"):
            lab.stored_evidence(120, {"accepted": 120}, bound)
        with self.assertRaisesRegex(lab.Rejected, "partial delivery"):
            lab.stored_evidence(20, delta, bound)

    def test_slow_or_invalid_timing_is_inconclusive(self):
        for elapsed in (3.9, 4, 100, 0, -1, float("inf"), float("nan"), True, "0.1"):
            with self.subTest(elapsed=elapsed), self.assertRaises(lab.Rejected):
                lab.burst_bound({**self.burst, "elapsedSeconds": elapsed}, self.pods)
        self.assertEqual(lab.burst_bound({**self.burst, "elapsedSeconds": 3.89}, self.pods), 119)

    def test_partial_failed_or_extra_requests_cannot_pass(self):
        for statuses in (
            {**self.burst["statuses"], "quota-1": 503},
            {**self.burst["statuses"], "quota-1": 0},
            {**self.burst["statuses"], "quota-1": "200"},
            {"quota-0": 200, "quota-1": 200},
            {**self.burst["statuses"], "quota-3": 200},
        ):
            with self.subTest(statuses=statuses), self.assertRaisesRegex(lab.Rejected, "HTTP 200"):
                lab.burst_bound({**self.burst, "statuses": statuses}, self.pods)

    def test_counter_failures_background_traffic_and_resets_are_inconclusive(self):
        for key, value in (
            ("accepted", 141),
            ("unavailable", 1),
            ("dropped", 199),
            ("accepted", float("nan")),
        ):
            changed = copy.deepcopy(self.after)
            changed["quota-0"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(lab.Rejected):
                lab.quota_delta(self.before, changed, self.pods, 42)
        with self.assertRaises(lab.Rejected):
            lab.quota_delta(self.before, self.after, {"foreign": {}}, 42)

    def test_requires_exact_active_service_endpoints_and_projections(self):
        task = lab.SharedQuotaAcceptance(self.args)
        task.pods = Mock(return_value=self.pods)
        rows = "\n".join(f"{p}\t{s['uid']}\t{s['ip']}\ttrue" for p, s in self.pods.items())
        task.kube = Mock(return_value=rows)
        self.assertEqual(task.active_quota(), self.pods)
        args = task.kube.call_args.args
        self.assertEqual(
            args[:4], ("get", "endpointslices", "-l", "kubernetes.io/service-name=log-quota")
        )
        self.assertIn('{"\\t"}', args[-1])
        self.assertNotIn('{"\\\\t"}', args[-1])
        for bad in (
            rows.replace("\ttrue", "\tfalse", 1),
            rows.replace("10.72.16.3", "10.72.16.99"),
            rows + "\n" + rows.splitlines()[0],
        ):
            task.kube.return_value = bad
            with self.assertRaises(lab.Rejected):
                task.active_quota()

    def test_policy_and_byte_guard_prevent_a_different_quota_masking_the_test(self):
        task = lab.SharedQuotaAcceptance(self.args)
        values = {"EVENTS_PER_SECOND": "20", "EVENT_BURST": "40", "BYTE_BURST": "2097152"}
        task.policy_value = Mock(side_effect=lambda pod, key: values[key])
        task.guard_policy(self.pods)
        self.assertEqual(task.policy_value.call_count, 9)
        for key, bad in (
            ("EVENTS_PER_SECOND", "21"),
            ("EVENT_BURST", "41"),
            ("BYTE_BURST", "1024"),
        ):
            original, values[key] = values[key], bad
            with self.subTest(key=key), self.assertRaises(lab.Rejected):
                task.guard_policy(self.pods)
            values[key] = original

    def probe_process(self, mode, agent, argument=None, cluster=None):
        agent_path, config_path = self.root / "agent.py", self.root / "settings.json"
        agent_path.write_text(agent)
        password = self.root / "password"
        password.write_text("SYNTHETIC-SECRET")
        config_path.write_text(
            json.dumps(
                {
                    "cluster": cluster or self.args.cluster,
                    "clickhouse": "http://clickhouse:8123",
                    "clickhousePasswordFile": str(password),
                }
            )
        )
        code = lab.SHARED_PROBE.replace("/config/agent.py", str(agent_path)).replace(
            "/config/settings.json", str(config_path)
        )
        return subprocess.run(
            [sys.executable, "-B", "-", mode, json.dumps(argument or self.argument)],
            input=code,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_probe_sends_three_concurrent_bounded_unique_normalized_batches_no_retries(self):
        agent = f"""
import pathlib, threading
barrier = threading.Barrier(3)
def http(url, payload=None, headers=None):
    barrier.wait(timeout=2)
    assert headers == {{"Content-Type": "application/x-ndjson"}}
    name = url.split("//")[1].split(":")[0]
    pathlib.Path({str(self.root)!r}, name).write_bytes(payload)
    return 200, b"SYNTHETIC-SECRET"
"""
        result = self.probe_process("burst", agent)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("SYNTHETIC-SECRET", result.stdout + result.stderr)
        evidence = json.loads(result.stdout)
        lab.burst_bound(evidence, self.pods)
        records = []
        for state in self.pods.values():
            batch = [
                json.loads(line) for line in (self.root / state["ip"]).read_text().splitlines()
            ]
            self.assertEqual(len(batch), 100)
            records.extend(batch)
        self.assertEqual(len({record["message"] for record in records}), 300)
        self.assertEqual(len({record["event_id"] for record in records}), 300)
        for record in records:
            self.assertEqual(record["level"], "ERROR")
            self.assertEqual(record["cluster_name"], self.args.cluster)
            self.assertEqual(record["namespace"], "observability")
            self.assertEqual(record["service_key"], "observability/" + self.argument["service"])
            self.assertTrue(record["message"].startswith(self.argument["marker"]))

    def test_in_pod_http_error_bodies_and_credentials_never_leave_probe(self):
        result = self.probe_process(
            "burst",
            """
def http(url, payload=None, headers=None):
    if "10.72.16.3" in url:
        raise ValueError("SYNTHETIC-SECRET raw HTTP error body")
    return 200, b"SYNTHETIC-SECRET"
""",
        )
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("SYNTHETIC-SECRET", result.stdout + result.stderr)
        with self.assertRaises(lab.Rejected):
            lab.burst_bound(json.loads(result.stdout), self.pods)
        result = self.probe_process(
            "count",
            """
def http(url, payload=None, headers=None):
    raise ValueError("SYNTHETIC-SECRET raw ClickHouse error body")
""",
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr.strip(), "Shared quota probe unavailable")

    def test_count_query_has_exact_cluster_namespace_service_and_fresh_marker(self):
        agent = f"""
import base64, urllib.parse
def http(url, payload=None, headers=None):
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert query["param_cluster"] == [{self.args.cluster!r}]
    assert query["param_service"] == [{self.argument["service"]!r}]
    assert query["param_marker"] == [{self.argument["marker"]!r}]
    assert "namespace='observability'" in query["query"][0]
    assert "timestamp > now() - INTERVAL 15 MINUTE" in query["query"][0]
    assert "uniqExact(message)" in query["query"][0]
    assert headers["Authorization"] == "Basic " + base64.b64encode(b"canary:SYNTHETIC-SECRET").decode()
    return 200, b'{{"stored":"40"}}'
"""
        result = self.probe_process("count", agent)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"stored": 40})
        self.assertNotIn("SYNTHETIC-SECRET", result.stdout + result.stderr)
        result = self.probe_process("count", agent, cluster="foreign")
        self.assertEqual(result.returncode, 2)
        result = self.probe_process(
            "count", "def http(*args, **kwargs): return 200, b'{\"stored\":1.9}'"
        )
        self.assertEqual(result.returncode, 2)

    def test_full_orchestration_waits_for_all_accepted_records_and_stable_storage(self):
        task = lab.SharedQuotaAcceptance(self.args)
        support = {"support-a": {}, "support-b": {}}
        task.pods = Mock(return_value=support)
        task.active_quota = Mock(return_value=self.pods)
        task.guard_policy = Mock()
        task.probe = Mock(side_effect=[self.before, self.after])
        observed = iter([0, 20, 40, 40, 40, 40, 40])
        clock = [0]
        modes = []

        def probe(pod, mode, arg):
            modes.append(mode)
            return self.burst if mode == "burst" else {"stored": next(observed)}

        def pause():
            clock[0] += 5

        task.shared_probe, task.pause = probe, pause
        with patch.object(lab.time, "monotonic", side_effect=lambda: clock[0]):
            result = task.run_burst()
        self.assertEqual(result["storedDistinct"], 40)
        self.assertEqual(result["stableObservationSeconds"], 20)
        self.assertEqual(clock[0], 30)
        self.assertEqual(modes.count("burst"), 1)
        self.assertEqual(modes.count("count"), 7)
        self.assertIn("node-agent path bypassed", result["scope"])


if __name__ == "__main__":
    unittest.main()
