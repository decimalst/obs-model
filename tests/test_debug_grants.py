import datetime as dt
import unittest

from scripts.debug_grants import FROM, UNTIL, UTC, active, annotation_patch, grant, owned_pods


class DebugGrants(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 9, 21, 12, tzinfo=UTC)

    def test_boundaries(self):
        for hours in [8, 24]:
            tags = grant(hours, self.now)
            self.assertTrue(active(tags, self.now))
            self.assertTrue(active(tags, self.now + dt.timedelta(hours=hours, seconds=-1)))
            self.assertFalse(active(tags, self.now + dt.timedelta(hours=hours)))
            self.assertFalse(active(tags, self.now - dt.timedelta(seconds=1)))

    def test_invalid_grants_fail_closed(self):
        for tags in [
            {},
            {FROM: "oops", UNTIL: "later"},
            {FROM: "2026-09-21T00:00:00", UNTIL: "2026-09-22T00:00:00"},
            {FROM: "2026-09-21T00:00:00Z", UNTIL: "2026-09-23T00:00:00Z"},
        ]:
            self.assertFalse(active(tags, self.now))
        for hours in [0, -1, 25, float("inf"), float("nan")]:
            with self.assertRaises(ValueError):
                grant(hours, self.now)

    def test_patch_is_race_checked_and_preserves_other_annotations(self):
        obj = {
            "metadata": {
                "resourceVersion": "17",
                "annotations": {"keep": "yes", FROM: "old", UNTIL: "old"},
            }
        }
        ops = annotation_patch(obj, {})
        self.assertEqual(ops[0], {"op": "test", "path": "/metadata/resourceVersion", "value": "17"})
        self.assertEqual(len(ops), 3)
        self.assertTrue(all("keep" not in o["path"] for o in ops))
        self.assertEqual(annotation_patch({"metadata": {"resourceVersion": "1"}}, {}), [])

    def test_ownership_not_mutable_labels(self):
        def obj(uid, owner=None):
            return {
                "metadata": {
                    "uid": uid,
                    "ownerReferences": [{"uid": owner, "controller": True}] if owner else [],
                }
            }

        deployment = obj("deployment")
        rs = [obj("correct-rs", "deployment"), obj("wrong-rs", "someone-else")]
        pods = [obj("ours", "correct-rs"), obj("theirs", "wrong-rs")]
        self.assertEqual(owned_pods(deployment, rs, pods), [pods[0]])


if __name__ == "__main__":
    unittest.main()
