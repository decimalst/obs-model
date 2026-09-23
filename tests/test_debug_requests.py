import datetime as dt
import math
import unittest

from scripts.debug_grants import FROM, UNTIL, UTC
from scripts.debug_request import apply, authorize


class DebugRequestTests(unittest.TestCase):
    def setUp(self):
        self.binding = dict(
            actor="alice", region="us-east-1", cluster="existing-eks", namespace="shop"
        )
        self.policy = {"version": 1, "owners": {"payments": [self.binding]}}
        self.services = [{"name": "checkout", "namespace": "shop", "owner": "payments"}]

    def test_scope_mismatches_and_overlong_grants_are_denied(self):
        for key, bad in [
            ("actor", "bob"),
            ("region", "us-west-2"),
            ("cluster", "another"),
            ("namespace", "other"),
        ]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                authorize(
                    self.policy,
                    self.services,
                    **{**self.binding, key: bad},
                    service="checkout",
                    hours=8,
                )
        for hours in [0, -1, 24.01, math.nan, math.inf]:
            with self.assertRaises(ValueError):
                authorize(
                    self.policy, self.services, **self.binding, service="checkout", hours=hours
                )
        self.assertEqual(
            authorize(self.policy, self.services, **self.binding, service="checkout", hours=24)[
                "owner"
            ],
            "payments",
        )
        with self.assertRaises(ValueError):
            authorize(
                {"version": 1, "owners": {}},
                self.services,
                **self.binding,
                service="checkout",
                hours=8,
            )

    def test_workload_identity_checked_before_mutation_and_other_pods_untouched(self):
        workload = {
            "metadata": {"name": "checkout-v2", "uid": "own"},
            "spec": {"template": {"metadata": {"labels": {"observability/service": "checkout"}}}},
        }
        pods = [
            {
                "metadata": {
                    "name": name,
                    "uid": name,
                    "ownerReferences": [{"uid": owner, "controller": True}],
                }
            }
            for name, owner in [("ours", "own"), ("other", "foreign")]
        ]
        writes = []

        class Kube:
            def request(self, kind, name=""):
                if name:
                    return workload
                return {"items": pods if kind == "pods" else []}

            def patch(self, kind, obj, desired):
                writes.append((kind, obj["metadata"]["name"], desired))

        now = dt.datetime(2026, 9, 23, tzinfo=UTC)
        with self.assertRaises(ValueError):
            apply(Kube(), "deployment", "checkout-v2", "unauthorized", "grant", 8, now)
        self.assertEqual(writes, [])
        granted = apply(Kube(), "deployment", "checkout-v2", "checkout", "grant", 8, now)
        self.assertIn(FROM, granted)
        self.assertIn(UNTIL, granted)
        self.assertEqual([w[1] for w in writes], ["checkout-v2", "ours"])
        writes.clear()
        apply(Kube(), "deployment", "checkout-v2", "checkout", "revoke", 8, now)
        self.assertTrue(all(w[2] == {} for w in writes))
