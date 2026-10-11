
import unittest
from unittest.mock import patch, Mock

import health_monitor


class SystemDiagnosticsTests(unittest.TestCase):

    @patch("health_monitor.subprocess.run")
    @patch("health_monitor.run_command")
    def test_degraded_systemd_state(self, mock_command, mock_run):
        mock_command.return_value = ""

        mock_run.return_value = None

        result = Mock()
        result.stdout = "degraded\n"
        result.stderr = ""
        result.returncode = 1
        mock_run.return_value = result

        # The implementation uses subprocess.run directly
        # for systemctl is-system-running.
        with patch(
            "health_monitor.subprocess.run",
            return_value=result,
        ):
            diagnostics = health_monitor.get_system_diagnostics()

        self.assertEqual(
            diagnostics["systemd_status"],
            "degraded",
        )

    def test_health_thresholds(self):
        self.assertEqual(
            health_monitor.WARNING_THRESHOLD,
            80.0,
        )
        self.assertEqual(
            health_monitor.CRITICAL_THRESHOLD,
            95.0,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

