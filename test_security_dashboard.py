import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DASHBOARD = (ROOT / "live_dashboard.py").read_text(encoding="utf-8")
HTML = (ROOT / "health_monitor.py").read_text(encoding="utf-8")
import security_monitor



class SecurityDashboardTests(unittest.TestCase):
    def test_security_page_is_in_navigation_and_has_event_table(self):
        self.assertIn('data-page="security"', HTML)
        self.assertIn('id="security-events-body"', HTML)
        self.assertIn('id="security-event-filter"', HTML)

    def test_security_api_routes_are_registered(self):
        self.assertIn('/api/security/events', DASHBOARD)
        self.assertIn('/api/security/status', DASHBOARD)

    def test_security_page_fetches_events_and_fail2ban_status(self):
        self.assertIn('refreshSecurityEvents()', DASHBOARD)
        self.assertIn('fetch("/api/security/status"', DASHBOARD)

    def test_security_monitor_is_integrated_into_collection_loop(self):
        self.assertIn('security_monitor.collect_security_events', DASHBOARD)
        self.assertIn('dispatch_alert_async(notification_event)', DASHBOARD)

    def test_security_configuration_is_present(self):
        self.assertEqual(security_monitor.DEFAULT_CONFIG["failed_login_threshold"], 5)
        self.assertIn("/etc/ssh/sshd_config", security_monitor.DEFAULT_CONFIG["sensitive_files"])
        self.assertIn("/var/log/auth.log", security_monitor.DEFAULT_CONFIG["auth_log_paths"])


if __name__ == "__main__":
    unittest.main()
