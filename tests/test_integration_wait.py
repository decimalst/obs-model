import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from integration_test import wait


class IntegrationWait(unittest.TestCase):
    def test_delayed_delivery_can_use_the_full_budget(self):
        probe = Mock(side_effect=[False, False, True])
        with (
            patch("integration_test.time.monotonic", side_effect=[0, 46, 119]),
            patch("integration_test.time.sleep"),
        ):
            wait(probe, "delivery", timeout=120)
        self.assertEqual(probe.call_count, 3)

    def test_partial_delivery_still_times_out_without_resending(self):
        probe = Mock(return_value=False)
        with (
            patch("integration_test.time.monotonic", side_effect=[0, 119, 120]),
            patch("integration_test.time.sleep"),
            self.assertRaisesRegex(RuntimeError, "Timed out: delivery"),
        ):
            wait(probe, "delivery", timeout=120)
        self.assertEqual(probe.call_count, 2)

    def test_transient_probe_errors_do_not_reset_deadline(self):
        probe = Mock(side_effect=OSError("not ready"))
        with (
            patch("integration_test.time.monotonic", side_effect=[0, 45]),
            patch("integration_test.time.sleep") as sleep,
            self.assertRaisesRegex(RuntimeError, "Timed out: startup"),
        ):
            wait(probe, "startup")
        sleep.assert_not_called()

    def test_unexpected_probe_error_is_not_hidden(self):
        with self.assertRaises(ValueError):
            wait(Mock(side_effect=ValueError("invalid count")), "delivery")
