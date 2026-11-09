"""Regression tests for the ServerPulse 0.7 dashboard additions."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import health_monitor
import live_dashboard


class WSLMountMonitoringTests(unittest.TestCase):
    def test_windows_drive_mount_is_excluded_from_health_checks(self):
        report = {
            "cpu": {"usage_percent": 5},
            "memory": {"usage_percent": 10},
            "disks": [
                {"mountpoint": "/", "type": "ext4", "usage_percent": 20},
                {
                    "mountpoint": "/mnt/c", "type": "9p",
                    "usage_percent": 99, "monitored": False,
                },
            ],
        }
        result = health_monitor.assess_health(report)
        names = [check["name"] for check in result["checks"]]
        self.assertIn("disk:/", names)
        self.assertNotIn("disk:/mnt/c", names)
        disk_check = next(check for check in result["checks"] if check["name"] == "disk:/")
        self.assertEqual(disk_check["status"], "OK")

    def test_wsl_host_noise_markers_are_recognized(self):
        self.assertTrue(
            health_monitor.is_wsl_host_noise(
                "kernel: hv_balloon: Hyper-V balloon warning"
            )
        )
        self.assertFalse(
            health_monitor.is_wsl_host_noise(
                "sshd: Failed password for invalid user"
            )
        )


class DashboardSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp_dir.name) / "config.json"
        self.config_path.write_text(
            json.dumps({
                "thresholds": {
                    "cpu": {"warning": 70, "critical": 90},
                    "memory": {"warning": 75, "critical": 90},
                    "disk": {"warning": 80, "critical": 95},
                    "disk_io": {
                        "read_warning_mbps": 100,
                        "read_critical_mbps": 200,
                        "write_warning_mbps": 100,
                        "write_critical_mbps": 200,
                    },
                },
                "health": {"exclude_mountpoints": ["/mnt/c"]},
                "services": {
                    "ssh": {"enabled": True, "unit": "ssh"},
                    "sshd": {"enabled": False, "unit": "sshd"},
                    "docker": {"enabled": True, "unit": "docker"},
                    "nginx": {"enabled": True, "unit": "nginx"},
                },
            }),
            encoding="utf-8",
        )
        self.config_patch = patch.object(
            live_dashboard, "CONFIG_FILE", self.config_path
        )
        self.config_patch.start()

    def tearDown(self):
        self.config_patch.stop()
        self.temp_dir.cleanup()

    def test_valid_settings_are_persisted(self):
        result = live_dashboard.validate_dashboard_config({
            "cpu": {"warning": 65, "critical": 88},
            "memory": {"warning": 70, "critical": 90},
            "disk": {"warning": 75, "critical": 92},
            "exclude_mountpoints": ["/mnt/c", "/backup"],
        })
        self.assertEqual(result["thresholds"]["cpu"]["warning"], 65)
        self.assertEqual(
            result["health"]["exclude_mountpoints"],
            ["/backup", "/mnt/c"],
        )
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["thresholds"]["memory"]["critical"], 90)

    def test_invalid_threshold_order_is_rejected(self):
        with self.assertRaises(ValueError):
            live_dashboard.validate_dashboard_config({
                "cpu": {"warning": 90, "critical": 80},
            })

    def test_relative_excluded_mount_is_rejected(self):
        with self.assertRaises(ValueError):
            live_dashboard.validate_dashboard_config({
                "exclude_mountpoints": ["mnt/c"],
            })

    def test_service_monitoring_can_be_enabled_or_disabled(self):
        result = live_dashboard.validate_dashboard_config({
            "services": {"docker": False, "nginx": True},
        })
        self.assertFalse(result["services"]["docker"]["enabled"])
        self.assertTrue(result["services"]["nginx"]["enabled"])


class DashboardNavigationTests(unittest.TestCase):
    def test_dashboard_renders_independent_navigation_targets(self):
        report = {
            "monitor_version": "0.9.0",
            "timestamp_utc": "2026-11-09T04:00:00Z",
            "system": {"hostname": "test-server"},
            "cpu": {"usage_percent": 5, "iowait_percent": 0},
            "memory": {
                "usage_percent": 20, "used_bytes": 200, "total_bytes": 1000,
                "swap_used_bytes": 0, "swap_total_bytes": 0,
            },
            "disks": [{
                "mountpoint": "/", "usage_percent": 10, "type": "ext4",
                "monitored": True,
            }],
            "disk_io": {"sda": {
                "read_bytes_per_sec": 1024, "write_bytes_per_sec": 2048,
                "reads_per_sec": 1, "writes_per_sec": 2,
            }},
            "network": [],
            "network_io": {"rx_mbps": 0, "tx_mbps": 0, "interfaces": {}},
            "services": {},
            "top_processes": [],
            "listening_sockets": {"count": 0, "items": []},
            "system_diagnostics": {
                "systemd_status": "running", "failed_units": [],
                "errors": {"messages": []}, "warnings": {"messages": []},
            },
            "health": {"status": "OK", "checks": []},
            "thresholds": {},
        }
        html = health_monitor.generate_html_report(report, [])
        for page in (
            "dashboard", "metrics", "alerts", "disks", "network",
            "services", "processes", "logs", "history", "settings",
        ):
            self.assertIn(f'data-page="{page}"', html)
        self.assertIn('id="metrics-detail"', html)
        self.assertIn('id="disk-io-device-body"', html)
        self.assertIn('id="sockets"', html)
        self.assertIn('id="failed-units"', html)

    def test_live_dashboard_contains_page_switching_and_metrics_refresh(self):
        self.assertIn('function setActivePage(page, updateHash = false)', live_dashboard.LIVE_SCRIPT)
        self.assertIn('updateMetricsDetails(report)', live_dashboard.LIVE_SCRIPT)
        self.assertIn('data-page', live_dashboard.LIVE_SCRIPT)


if __name__ == "__main__":
    unittest.main()
