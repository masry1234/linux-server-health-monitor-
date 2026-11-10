import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import health_monitor


class HistoricalAnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "history.sqlite3"
        self.jsonl_path = Path(self.temp_dir.name) / "history.jsonl"
        self.db_patch = patch.object(health_monitor, "HISTORY_DB_FILE", self.db_path)
        self.json_patch = patch.object(health_monitor, "HISTORY_FILE", self.jsonl_path)
        self.db_patch.start()
        self.json_patch.start()

    def tearDown(self):
        self.json_patch.stop()
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def add_report(self, minutes_ago, cpu, memory=40, read_bps=0, write_bps=0):
        timestamp = (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()
        report = {
            "timestamp_utc": timestamp,
            "cpu": {"usage_percent": cpu},
            "memory": {"usage_percent": memory},
            "disk_io": {"sda": {
                "read_bytes_per_sec": read_bps,
                "write_bytes_per_sec": write_bps,
            }},
            "network_io": {"rx_mbps": 1.5, "tx_mbps": 0.5},
        }
        health_monitor.save_report_history(report)

    def test_returns_aggregates_and_detects_cpu_outlier(self):
        for index in range(9):
            self.add_report(9 - index, 20)
        self.add_report(0, 90, read_bps=2 * 1024 * 1024, write_bps=1024 * 1024)

        result = health_monitor.get_history_analytics(60)

        self.assertEqual(result["sample_count"], 10)
        self.assertEqual(result["summary"]["cpu_percent"]["average"], 27.0)
        self.assertEqual(result["summary"]["disk_read_mb_s"]["maximum"], 2.0)
        self.assertEqual(result["summary"]["disk_write_mb_s"]["maximum"], 1.0)
        self.assertEqual(result["summary"]["network_rx_mb_s"]["average"], 1.5)
        self.assertTrue(any(item["metric"] == "cpu_percent" for item in result["anomalies"]))

    def test_ignores_small_cpu_spikes_on_idle_baseline(self):
        for index in range(9):
            self.add_report(9 - index, 0, memory=11)
        self.add_report(0, 13.4, memory=11)

        result = health_monitor.get_history_analytics(60)

        self.assertFalse(
            any(item["metric"] == "cpu_percent" for item in result["anomalies"])
        )

    def test_requires_minimum_baseline_sample_count(self):
        for index in range(8):
            self.add_report(8 - index, 10, memory=20)
        self.add_report(0, 90, memory=20)

        result = health_monitor.get_history_analytics(60)

        self.assertEqual(result["sample_count"], 9)
        self.assertFalse(result["anomalies"])

    def test_downsamples_series_to_requested_limit(self):
        for index in range(40):
            self.add_report(39 - index, index, memory=30)
        result = health_monitor.get_history_analytics(60, max_points=10)
        self.assertEqual(result["sample_count"], 40)
        self.assertEqual(result["returned_points"], 10)
        self.assertEqual(len(result["series"]), 10)

    def test_ignores_reports_outside_requested_range(self):
        self.add_report(120, 99)
        self.add_report(1, 25)
        result = health_monitor.get_history_analytics(60)
        self.assertEqual(result["sample_count"], 1)
        self.assertEqual(result["summary"]["cpu_percent"]["average"], 25.0)

    def test_rejects_unsupported_range(self):
        with self.assertRaises(ValueError):
            health_monitor.get_history_analytics(30)

    def test_rejects_invalid_point_limit(self):
        with self.assertRaises(ValueError):
            health_monitor.get_history_analytics(60, max_points=2)


if __name__ == "__main__":
    unittest.main()


class AnalyticsApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        import threading
        import live_dashboard

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), live_dashboard.DashboardHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_analytics_endpoint_returns_json(self):
        from http.client import HTTPConnection
        from unittest.mock import patch
        import live_dashboard

        expected = {"range_minutes": 60, "sample_count": 4, "series": []}
        with patch.object(live_dashboard.monitor, "get_history_analytics", return_value=expected):
            connection = HTTPConnection("127.0.0.1", self.port, timeout=2)
            connection.request("GET", "/api/analytics?range_minutes=60")
            response = connection.getresponse()
            payload = json.loads(response.read())
            connection.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["sample_count"], 4)

    def test_analytics_endpoint_rejects_invalid_range(self):
        from http.client import HTTPConnection

        connection = HTTPConnection("127.0.0.1", self.port, timeout=2)
        connection.request("GET", "/api/analytics?range_minutes=30")
        response = connection.getresponse()
        response.read()
        connection.close()
        self.assertEqual(response.status, 400)
