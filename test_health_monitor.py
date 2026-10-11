
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import health_monitor


class SystemDiagnosticsTests(unittest.TestCase):
    def test_degraded_systemd_state(self):
        mocked_result = unittest.mock.Mock(
            returncode=1,
            stdout="degraded\n",
            stderr="",
        )

        with patch(
            "health_monitor.subprocess.run",
            return_value=mocked_result,
        ), patch(
            "health_monitor.run_command",
            return_value="",
        ):
            result = health_monitor.get_system_diagnostics()

        self.assertEqual(result["systemd_status"], "degraded")

    def test_health_thresholds(self):
        thresholds = {
            "cpu": {"warning": 70.0, "critical": 90.0},
            "memory": {"warning": 75.0, "critical": 90.0},
            "disk": {"warning": 80.0, "critical": 95.0},
        }

        report = {
            "cpu": {"usage_percent": 75.0},
            "memory": {"usage_percent": 50.0},
            "disks": [],
        }

        result = health_monitor.assess_health(report, thresholds)

        self.assertEqual(result["status"], "WARNING")
        self.assertEqual(result["checks"][0]["status"], "WARNING")
        self.assertEqual(result["checks"][0]["warning_threshold"], 70.0)
        self.assertEqual(result["checks"][0]["critical_threshold"], 90.0)

    def test_critical_cpu_threshold(self):
        thresholds = {
            "cpu": {"warning": 70.0, "critical": 90.0},
            "memory": {"warning": 75.0, "critical": 90.0},
            "disk": {"warning": 80.0, "critical": 95.0},
        }

        report = {
            "cpu": {"usage_percent": 90.0},
            "memory": {"usage_percent": 20.0},
            "disks": [],
        }

        result = health_monitor.assess_health(report, thresholds)

        self.assertEqual(result["status"], "CRITICAL")
        self.assertEqual(result["checks"][0]["status"], "CRITICAL")

    def test_resource_specific_thresholds(self):
        thresholds = {
            "cpu": {"warning": 70.0, "critical": 90.0},
            "memory": {"warning": 75.0, "critical": 90.0},
            "disk": {"warning": 80.0, "critical": 95.0},
        }

        report = {
            "cpu": {"usage_percent": 50.0},
            "memory": {"usage_percent": 76.0},
            "disks": [
                {
                    "mountpoint": "/",
                    "usage_percent": 81,
                }
            ],
        }

        result = health_monitor.assess_health(report, thresholds)
        checks = {check["name"]: check for check in result["checks"]}

        self.assertEqual(checks["cpu"]["status"], "OK")
        self.assertEqual(checks["memory"]["status"], "WARNING")
        self.assertEqual(checks["disk:/"]["status"], "WARNING")
        self.assertEqual(checks["memory"]["warning_threshold"], 75.0)
        self.assertEqual(checks["disk:/"]["warning_threshold"], 80.0)

    def test_load_thresholds_from_config(self):
        config = {
            "thresholds": {
                "cpu": {"warning": 70, "critical": 90},
                "memory": {"warning": 75, "critical": 90},
                "disk": {"warning": 80, "critical": 95},
            }
        }

        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(
                json.dumps(config),
                encoding="utf-8",
            )

            thresholds = health_monitor.load_thresholds(config_path)

        self.assertEqual(thresholds["cpu"]["warning"], 70.0)
        self.assertEqual(thresholds["memory"]["warning"], 75.0)
        self.assertEqual(thresholds["disk"]["critical"], 95.0)

    def test_invalid_threshold_order(self):
        config = {
            "thresholds": {
                "cpu": {"warning": 90, "critical": 70},
                "memory": {"warning": 75, "critical": 90},
                "disk": {"warning": 80, "critical": 95},
            }
        }

        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(
                json.dumps(config),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                health_monitor.load_thresholds(config_path)


class LoggingTests(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger("health_monitor")
        self.original_handlers = self.logger.handlers[:]
        self.original_level = self.logger.level
        self.original_propagate = self.logger.propagate

        for handler in self.logger.handlers[:]:
            self.logger.removeHandler(handler)
            handler.close()

        self.temp_directory = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.temp_directory.name)
        self.log_file = self.log_dir / "health_monitor.log"

    def tearDown(self):
        for handler in self.logger.handlers[:]:
            self.logger.removeHandler(handler)
            handler.close()

        for handler in self.original_handlers:
            self.logger.addHandler(handler)

        self.logger.setLevel(self.original_level)
        self.logger.propagate = self.original_propagate
        self.temp_directory.cleanup()

    def test_setup_logging_writes_to_file(self):
        with (
            patch.object(health_monitor, "LOG_DIR", self.log_dir),
            patch.object(health_monitor, "LOG_FILE", self.log_file),
        ):
            logger = health_monitor.setup_logging()
            logger.info("logging test message")

            for handler in logger.handlers:
                handler.flush()

            self.assertTrue(self.log_file.exists())

            log_content = self.log_file.read_text(encoding="utf-8")
            self.assertIn("INFO", log_content)
            self.assertIn("logging test message", log_content)

    def test_logging_rotates_old_files(self):
        with (
            patch.object(health_monitor, "LOG_DIR", self.log_dir),
            patch.object(health_monitor, "LOG_FILE", self.log_file),
            patch.object(health_monitor, "LOG_MAX_BYTES", 200),
            patch.object(health_monitor, "LOG_BACKUP_COUNT", 2),
        ):
            logger = health_monitor.setup_logging()

            for index in range(30):
                logger.info("Test log entry %03d: %s", index, "X" * 40)

            for handler in logger.handlers:
                handler.flush()

            rotated_file = Path(f"{self.log_file}.1")
            self.assertTrue(
                rotated_file.exists(),
                "Expected a rotated log file to be created.",
            )


    def test_logging_falls_back_to_stderr(self):
        with (
            patch(
                "pathlib.Path.mkdir",
                side_effect=PermissionError("Permission denied"),
            ),
            patch("health_monitor.sys.stderr") as mocked_stderr,
        ):
            logger = health_monitor.setup_logging()

            self.assertTrue(
                any(
                    isinstance(handler, logging.StreamHandler)
                    for handler in logger.handlers
                )
            )

            logger.warning("Fallback logging test")
            mocked_stderr.write.assert_called()


class HtmlReportTests(unittest.TestCase):
    def test_html_report_displays_resource_and_systemd_status(self):
        report = {
            "timestamp_utc": "2026-10-11T10:00:00Z",
            "system": {"hostname": "test-server"},
            "health": {"status": "OK"},
            "system_diagnostics": {"systemd_status": "degraded"},
        }

        result = health_monitor.generate_html_report(report)

        self.assertIn("Resource health: OK", result)
        self.assertIn("Systemd status: degraded", result)
        self.assertIn("test-server", result)
        self.assertIn("View complete JSON report", result)

    def test_html_report_escapes_untrusted_content(self):
        report = {
            "timestamp_utc": "2026-10-11T10:00:00Z",
            "system": {"hostname": "<script>alert(1)</script>"},
            "health": {"status": "OK"},
            "system_diagnostics": {
                "systemd_status": "healthy",
                "errors": {
                    "messages": ["<img src=x onerror=alert(1)>"],
                },
            },
        }

        result = health_monitor.generate_html_report(report)

        self.assertNotIn("<script>alert(1)</script>", result)
        self.assertNotIn("<img src=x onerror=alert(1)>", result)
        self.assertIn("&lt;script&gt;", result)
        self.assertIn("&lt;img", result)


    def test_dashboard_shows_failed_units(self):
        report = {
            "monitor_version": "0.5.0",
            "timestamp_utc": "2026-10-11T10:00:00Z",
            "system": {"hostname": "test-server"},
            "cpu": {"usage_percent": 42},
            "memory": {"usage_percent": 50},
            "disks": [
                {"mountpoint": "/", "usage_percent": 40, "type": "ext4"}
            ],
            "health": {
                "status": "OK",
                "checks": [
                    {
                        "name": "cpu",
                        "status": "OK",
                        "value": 42,
                        "warning_threshold": 70,
                        "critical_threshold": 90,
                    }
                ],
            },
            "system_diagnostics": {
                "systemd_status": "degraded",
                "failed_units": [
                    {
                        "unit": "demo.service",
                        "active": "failed",
                        "description": "Demo failure",
                    }
                ],
                "errors": {"status": "ok", "messages": ["Example error"]},
                "warnings": {"status": "ok", "messages": ["Example warning"]},
            },
        }

        result = health_monitor.generate_html_report(report)

        self.assertIn("Server Health Dashboard", result)
        self.assertIn("demo.service", result)
        self.assertIn("Demo failure", result)
        self.assertIn("Example error", result)
        self.assertIn("Example warning", result)


if __name__ == "__main__":
    unittest.main()

