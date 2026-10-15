import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from integration_test import delivery_diagnostics, wait


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


class DeliveryDiagnostics(unittest.TestCase):
    def test_failure_in_any_probe_does_not_skip_the_others(self):
        for failed_probe in range(3):
            with self.subTest(failed_probe=failed_probe):
                sql = Mock(return_value="quiet\t1\t1")
                with (
                    patch("integration_test.urllib.request.urlopen") as request,
                    patch("integration_test.command", return_value="offsets") as command,
                    patch("builtins.print") as output,
                ):
                    response = request.return_value.__enter__.return_value
                    response.read.return_value = b"log_quota_accepted_total 7\n"
                    [sql, request, command][failed_probe].side_effect = OSError("unreachable")
                    delivery_diagnostics(sql, "127.0.0.1:8080", "fixture-kafka")
                sql.assert_called_once()
                request.assert_called_once_with("http://127.0.0.1:8080/metrics", timeout=5)
                command.assert_called_once()
                self.assertEqual(output.call_count, 3)
                self.assertEqual(
                    sum("unavailable" in call.args[0] for call in output.call_args_list), 1
                )

    def test_startup_failure_before_clients_exist_is_reported_without_raising(self):
        with (
            patch("integration_test.urllib.request.urlopen", side_effect=OSError("not started")),
            patch("integration_test.command", side_effect=OSError("not started")),
            patch("builtins.print") as output,
        ):
            delivery_diagnostics(None, "", "")
        self.assertEqual(output.call_count, 3)
        self.assertTrue(all("unavailable" in call.args[0] for call in output.call_args_list))
