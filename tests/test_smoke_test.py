import copy
import json
import unittest
from unittest.mock import patch

from scripts.smoke_test import PendingTraceDrain, main, verify_on_delete_sampler, wait_for_sampler


def sampler():
    statefulset = {
        "metadata": {"name": "otel-traces", "uid": "sampler-uid", "generation": 4},
        "spec": {"replicas": 3, "updateStrategy": {"type": "OnDelete"}},
        "status": {
            "observedGeneration": 4,
            "replicas": 3,
            "readyReplicas": 3,
            "updatedReplicas": 3,
            "currentRevision": "revision-4",
            "updateRevision": "revision-4",
        },
    }
    pods = [
        {
            "metadata": {
                "name": f"otel-traces-{i}",
                "labels": {"controller-revision-hash": "revision-4"},
                "ownerReferences": [
                    {
                        "kind": "StatefulSet",
                        "name": "otel-traces",
                        "uid": "sampler-uid",
                        "controller": True,
                    }
                ],
            },
            "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]},
        }
        for i in range(3)
    ]
    return statefulset, pods


class SmokeTest(unittest.TestCase):
    def test_fresh_and_completed_manual_upgrade_are_ready(self):
        sts, pods = sampler()
        verify_on_delete_sampler(sts, pods)
        # Kubernetes does not advance currentRevision for OnDelete; all real
        # pods and updatedReplicas establish that the manual upgrade completed.
        sts["status"]["currentRevision"] = "revision-3"
        verify_on_delete_sampler(sts, pods)

    def test_old_ready_revision_requires_manual_trace_drain(self):
        sts, pods = sampler()
        pods[1]["metadata"]["labels"]["controller-revision-hash"] = "revision-3"
        with self.assertRaisesRegex(PendingTraceDrain, "pending trace drain"):
            verify_on_delete_sampler(sts, pods)

    def test_stale_or_partial_status_and_unready_pods_fail_closed(self):
        for change in (
            lambda s, p: s["status"].update(observedGeneration=3),
            lambda s, p: s["status"].update(updatedReplicas=2),
            lambda s, p: s["status"].update(readyReplicas=2),
            lambda s, p: s["status"].pop("updateRevision"),
            lambda s, p: s["spec"].update(updateStrategy={"type": "RollingUpdate"}),
            lambda s, p: p[0]["metadata"].update(deletionTimestamp="now"),
            lambda s, p: p[0]["status"].update(conditions=[]),
            lambda s, p: p[0]["metadata"]["labels"].pop("controller-revision-hash"),
            lambda s, p: p[0]["metadata"]["ownerReferences"][0].update(uid="foreign"),
            lambda s, p: p[0]["metadata"].update(name="otel-traces-9"),
            lambda s, p: p.pop(),
        ):
            sts, pods = sampler()
            change(sts, pods)
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                verify_on_delete_sampler(sts, pods)

    def test_wait_is_read_only_and_reports_pending_drain_immediately(self):
        sts, pods = sampler()
        pods[0]["metadata"]["labels"]["controller-revision-hash"] = "revision-3"
        with (
            patch(
                "scripts.smoke_test.kubectl",
                side_effect=[json.dumps(sts), json.dumps({"items": pods})],
            ) as kube,
            patch("scripts.smoke_test.time.sleep") as sleep,
        ):
            with self.assertRaises(PendingTraceDrain):
                wait_for_sampler()
            self.assertTrue(all(call.args[0] == "get" for call in kube.call_args_list))
            sleep.assert_not_called()

    def test_startup_waits_for_observed_readiness_and_times_out_without_mutation(self):
        sts, pods = sampler()
        waiting = copy.deepcopy(sts)
        waiting["status"]["readyReplicas"] = 2
        with (
            patch(
                "scripts.smoke_test.kubectl",
                side_effect=[
                    json.dumps(waiting),
                    json.dumps({"items": pods}),
                    json.dumps(sts),
                    json.dumps({"items": pods}),
                ],
            ),
            patch("scripts.smoke_test.time.sleep") as sleep,
        ):
            wait_for_sampler()
            sleep.assert_called_once_with(2)
        with (
            patch(
                "scripts.smoke_test.kubectl",
                side_effect=[json.dumps(waiting), json.dumps({"items": pods})],
            ),
            self.assertRaisesRegex(RuntimeError, "timed out"),
        ):
            wait_for_sampler(timeout=0)

    def test_main_never_requests_rolling_status_for_on_delete_sampler(self):
        sts, pods = sampler()
        calls = []

        def kube(*args):
            calls.append(args)
            if args[:2] == ("get", "rollout"):
                return json.dumps({"spec": {"replicas": 3}, "status": {"readyReplicas": 3}})
            if args[:2] == ("get", "statefulset"):
                return json.dumps(sts)
            if args[:2] == ("get", "pods"):
                return json.dumps({"items": pods})
            if args == ("rollout", "status", "statefulset/keeper", "--timeout=300s"):
                return ""
            raise AssertionError(args)

        with patch("scripts.smoke_test.kubectl", side_effect=kube):
            main()
        self.assertFalse(any("statefulset/otel-traces" in call for call in calls))
        self.assertTrue(all(call[0] in ("get", "rollout") for call in calls))


if __name__ == "__main__":
    unittest.main()
