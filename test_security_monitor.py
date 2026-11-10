import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import security_monitor as security


class SecurityParserTests(unittest.TestCase):
    def test_parses_failed_ssh_with_username_and_ip(self):
        event = security.parse_auth_line(
            "Nov 10 10:10:10 host sshd[123]: Failed password for invalid user admin from 203.0.113.7 port 2222 ssh2"
        )
        self.assertEqual(event["event_type"], "ssh_login_failed")
        self.assertEqual(event["username"], "admin")
        self.assertEqual(event["source_ip"], "203.0.113.7")

    def test_parses_successful_ssh(self):
        event = security.parse_auth_line(
            "Nov 10 10:10:10 host sshd[123]: Accepted publickey for deploy from 203.0.113.8 port 22 ssh2"
        )
        self.assertEqual(event["event_type"], "ssh_login_success")
        self.assertEqual(event["username"], "deploy")
        self.assertEqual(event["source_ip"], "203.0.113.8")
        self.assertEqual(event["details"]["method"], "publickey")

    def test_parses_sudo_command(self):
        event = security.parse_auth_line(
            "Nov 10 host sudo: alice : TTY=pts/0 ; PWD=/home/alice ; USER=root ; COMMAND=/usr/bin/systemctl restart nginx"
        )
        self.assertEqual(event["event_type"], "sudo_command")
        self.assertEqual(event["username"], "alice")
        self.assertIn("systemctl restart nginx", event["details"]["command"])

    def test_tags_sudo_firewall_mutation(self):
        event = security.parse_auth_line(
            "Nov 10 host sudo: alice : TTY=pts/0 ; PWD=/home/alice ; USER=root ; COMMAND=/usr/sbin/ufw allow 22"
        )
        self.assertEqual(event["event_type"], "sudo_command")
        self.assertEqual(event["details"]["security_tag"], "firewall_change")

    def test_parses_user_creation(self):
        event = security.parse_auth_line(
            "Nov 10 host useradd[123]: new user: name=bob, UID=1001, GID=1001"
        )
        self.assertEqual(event["event_type"], "user_account_change")
        self.assertEqual(event["username"], "bob")

    def test_parses_firewall_packet_event(self):
        event = security.parse_firewall_line(
            "Nov 10 host kernel: [UFW BLOCK] IN=eth0 SRC=198.51.100.9 DST=192.0.2.10"
        )
        self.assertEqual(event["event_type"], "firewall_packet_event")
        self.assertEqual(event["source_ip"], "198.51.100.9")

    def test_parses_fail2ban_ban_and_unban(self):
        banned = security.parse_fail2ban_line("Nov 10 fail2ban.actions: [sshd] Ban 203.0.113.4")
        unbanned = security.parse_fail2ban_line("Nov 10 fail2ban.actions: [sshd] Unban 203.0.113.4")
        self.assertEqual(banned["event_type"], "fail2ban_ban")
        self.assertEqual(unbanned["event_type"], "fail2ban_unban")

    def test_root_ssh_login_is_warning(self):
        event = security.parse_auth_line(
            "sshd: Accepted publickey for root from 192.0.2.10 port 22"
        )
        self.assertEqual(event["event_type"], "ssh_root_login")
        self.assertEqual(event["severity"], "WARNING")


class SecurityStorageAndDetectionTests(unittest.TestCase):
    def test_security_events_persist_and_can_be_queried(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "security.sqlite3"
            event = security.parse_auth_line("sshd: Accepted password for root from 192.0.2.4 port 22")
            self.assertEqual(security.save_events([event], db_path=db), 1)
            self.assertEqual(security.save_events([event], db_path=db), 0)
            rows = security.get_events(db_path=db)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["event_type"], "ssh_root_login")

    def test_detects_repeated_ssh_failures_from_same_ip(self):
        stamp = datetime.now(timezone.utc).isoformat()
        events = [
            {"event_type": "ssh_login_failed", "source_ip": "203.0.113.9", "timestamp_utc": stamp}
            for _ in range(5)
        ]
        alerts = security.detect_anomalies(events, failed_threshold=5, window_minutes=5)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["event_type"], "ssh_bruteforce_suspected")
        self.assertEqual(alerts[0]["severity"], "CRITICAL")

    def test_ignores_failures_without_source_ip(self):
        stamp = datetime.now(timezone.utc).isoformat()
        events = [
            {"event_type": "ssh_login_failed", "source_ip": None, "timestamp_utc": stamp}
            for _ in range(10)
        ]
        self.assertEqual(security.detect_anomalies(events), [])

    def test_incremental_log_reader_does_not_repeat_old_lines(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "auth.log"
            state_path = Path(temp) / "state.json"
            state = {}
            path.write_text("sshd: Failed password for root from 192.0.2.5 port 22\n", encoding="utf-8")
            first = security.scan_log_paths([str(path)], security.parse_auth_line, state, "auth")
            self.assertEqual(len(first), 1)
            with path.open("a", encoding="utf-8") as log_file:
                log_file.write("sshd: Accepted password for root from 192.0.2.5 port 22\n")
            second = security.scan_log_paths([str(path)], security.parse_auth_line, state, "auth")
            self.assertEqual(len(second), 1)
            self.assertEqual(second[0]["event_type"], "ssh_root_login")

    def test_file_integrity_detects_change_once_until_next_change(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "sshd_config"
            baseline = Path(temp) / "baseline.json"
            state = Path(temp) / "state.json"
            target.write_text("PermitRootLogin no\n", encoding="utf-8")
            security.initialize_file_baseline([str(target)], baseline_path=baseline)
            target.write_text("PermitRootLogin yes\n", encoding="utf-8")
            first = security.check_sensitive_files([str(target)], baseline_path=baseline, state_path=state)
            second = security.check_sensitive_files([str(target)], baseline_path=baseline, state_path=state)
            self.assertEqual(len(first), 1)
            self.assertEqual(first[0]["event_type"], "sensitive_file_modified")
            self.assertEqual(second, [])

    def test_file_integrity_detects_deleted_file(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "sudoers"
            baseline = Path(temp) / "baseline.json"
            target.write_text("root ALL=(ALL) ALL\n", encoding="utf-8")
            security.initialize_file_baseline([str(target)], baseline_path=baseline)
            target.unlink()
            events = security.check_sensitive_files([str(target)], baseline_path=baseline, state_path=Path(temp) / "state.json")
            self.assertEqual(events[0]["event_type"], "sensitive_file_deleted")

    def test_collector_persists_new_auth_events_incrementally(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            auth_log = root / "auth.log"
            auth_log.write_text(
                "sshd: Failed password for root from 203.0.113.11 port 22\n",
                encoding="utf-8",
            )
            options = {
                "enabled": True, "auth_log_paths": [str(auth_log)],
                "audit_log_paths": [], "fail2ban_log_paths": [],
                "firewall_log_paths": [], "sensitive_files": [],
                "failed_login_threshold": 5, "failed_login_window_minutes": 5,
            }
            state_file = root / "state.json"
            db_file = root / "events.sqlite3"
            first = security.collect_security_events(options, state_path=state_file, db_path=db_file)
            second = security.collect_security_events(options, state_path=state_file, db_path=db_file)
            self.assertEqual(len(first), 1)
            self.assertEqual(second, [])
            self.assertEqual(len(security.get_events(db_path=db_file)), 1)

    def test_bruteforce_threshold_accumulates_across_scans_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            auth_log = root / "auth.log"
            auth_log.write_text(
                "".join(
                    f"sshd: Failed password for root from 203.0.113.22 port {2200 + index}\n"
                    for index in range(3)
                ),
                encoding="utf-8",
            )
            options = {
                "enabled": True, "auth_log_paths": [str(auth_log)],
                "audit_log_paths": [], "fail2ban_log_paths": [],
                "firewall_log_paths": [], "sensitive_files": [],
                "failed_login_threshold": 5, "failed_login_window_minutes": 5,
            }
            state_file = root / "state.json"
            db_file = root / "events.sqlite3"
            security.collect_security_events(options, state_path=state_file, db_path=db_file)
            with auth_log.open("a", encoding="utf-8") as log_file:
                log_file.write(
                    "sshd: Failed password for root from 203.0.113.22 port 2204\n"
                    "sshd: Failed password for root from 203.0.113.22 port 2205\n"
                )
            second = security.collect_security_events(options, state_path=state_file, db_path=db_file)
            self.assertEqual(sum(item["event_type"] == "ssh_bruteforce_suspected" for item in second), 1)
            third = security.collect_security_events(options, state_path=state_file, db_path=db_file)
            self.assertEqual(sum(item["event_type"] == "ssh_bruteforce_suspected" for item in third), 0)


if __name__ == "__main__":
    unittest.main()
