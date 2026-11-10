"""Read-only Linux security event collection for ServerPulse.

Supports Ubuntu/Debian auth logs and optional auditd/fail2ban/UFW logs.
The collector never changes firewall, account, SSH, or service configuration.
"""
import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "health_history.sqlite3"
STATE_FILE = BASE_DIR / "security_monitor_state.json"
BASELINE_FILE = BASE_DIR / "security_file_baseline.json"
DEFAULT_CONFIG = {
    "enabled": True,
    "auth_log_paths": ["/var/log/auth.log", "/var/log/secure"],
    "audit_log_paths": ["/var/log/audit/audit.log"],
    "fail2ban_log_paths": ["/var/log/fail2ban.log"],
    "firewall_log_paths": ["/var/log/ufw.log"],
    "sensitive_files": [
        "/etc/passwd", "/etc/shadow", "/etc/group", "/etc/gshadow",
        "/etc/sudoers", "/etc/ssh/sshd_config", "/etc/ufw/user.rules",
        "/etc/nftables.conf",
    ],
    "failed_login_threshold": 5,
    "failed_login_window_minutes": 5,
    "max_events_per_scan": 500,
}
SSH_FAILED = re.compile(r"(?P<method>Failed password|authentication failure|Invalid user|Failed publickey)", re.I)
SSH_SUCCESS = re.compile(r"Accepted (?:password|publickey|keyboard-interactive)", re.I)
SUDO_COMMAND = re.compile(r"sudo:\s+(?P<user>[^:\s]+)\s*:.*?COMMAND=(?P<command>.+)$", re.I)
IP_PATTERN = re.compile(r"(?:from\s*|rhost=|SRC=)(?P<ip>(?:\d{1,3}\.){3}\d{1,3}|[0-9a-fA-F:]{3,})", re.I)
USER_ACTION = re.compile(
    r"\b(?P<action>new user|new group|delete user|delete group|modified user|modified group|"
    r"USER_ADD|USER_DEL|USER_MOD|GROUP_ADD|GROUP_DEL|GROUP_MOD)\b.*?"
    r"(?:name=|user=|group=|account\s+)(?P<account>[A-Za-z0-9_.-]+)?",
    re.I,
)
FIREWALL_CHANGE = re.compile(
    r"(?:COMMAND=.*(?:ufw|iptables|ip6tables|nft)\s+(?:add|delete|insert|flush|set|enable|disable)|"
    r"type=EXECVE.*(?:ufw|iptables|ip6tables|nft)|"
    r"(?:UFW|iptables|nftables).*(?:ruleset|rules).*(?:changed|updated|reload))",
    re.I,
)
UFW_EVENT = re.compile(r"\[UFW\s+(?P<action>BLOCK|ALLOW|REJECT)\]", re.I)
FAIL2BAN_ACTION = re.compile(r"\[(?P<jail>[^\]]+)\]\s+(?P<action>Ban|Unban)\s+(?P<ip>[0-9a-fA-F:.]+)", re.I)


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _read_json(path, default):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else default.copy()
    except (OSError, json.JSONDecodeError):
        return default.copy()


def _write_json(path, value):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(target)


def _connect(db_path=None):
    connection = sqlite3.connect(str(db_path or DB_FILE), timeout=10)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS security_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT NOT NULL UNIQUE,
            timestamp_utc TEXT NOT NULL,
            event_type TEXT NOT NULL,
            severity TEXT NOT NULL,
            source TEXT NOT NULL,
            username TEXT,
            source_ip TEXT,
            details TEXT NOT NULL
        )
    """)
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_security_events_time ON security_events(timestamp_utc)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_security_events_type ON security_events(event_type)"
    )
    return connection


def _event(event_type, severity, source, message, username=None, source_ip=None, **extra):
    details = {"message": message}
    details.update(extra)
    stamp = _utc_now()
    identity = json.dumps(
        [stamp, event_type, source, username, source_ip, details],
        sort_keys=True, ensure_ascii=False,
    )
    key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return {
        "event_key": key, "timestamp_utc": stamp, "event_type": event_type,
        "severity": severity, "source": source, "username": username,
        "source_ip": source_ip, "details": details,
    }


def save_events(events, db_path=None):
    if not events:
        return 0
    with closing(_connect(db_path)) as connection, connection:
        before = connection.total_changes
        connection.executemany(
            """INSERT OR IGNORE INTO security_events
            (event_key,timestamp_utc,event_type,severity,source,username,source_ip,details)
            VALUES (:event_key,:timestamp_utc,:event_type,:severity,:source,:username,:source_ip,:details)""",
            [
                {**event, "details": json.dumps(event["details"], ensure_ascii=False)}
                for event in events
            ],
        )
        return connection.total_changes - before


def get_events(limit=200, event_type=None, db_path=None):
    limit = max(1, min(int(limit), 2000))
    query = "SELECT timestamp_utc,event_type,severity,source,username,source_ip,details FROM security_events"
    args = []
    if event_type:
        query += " WHERE event_type = ?"
        args.append(event_type)
    query += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with closing(_connect(db_path)) as connection:
        rows = connection.execute(query, args).fetchall()
    results = []
    for row in rows:
        results.append({
            "timestamp_utc": row[0], "event_type": row[1], "severity": row[2],
            "source": row[3], "username": row[4], "source_ip": row[5],
            "details": json.loads(row[6]),
        })
    return results


def parse_auth_line(line, source="auth.log"):
    """Convert one auth/audit syslog line into a normalized event, or None."""
    text = line.strip()
    if not text:
        return None
    ip_match = IP_PATTERN.search(text)
    source_ip = ip_match.group("ip") if ip_match else None
    ssh_user = re.search(r"(?:for|user)\s+(?:invalid user\s+)?([A-Za-z0-9_.-]+)", text, re.I)
    username = ssh_user.group(1) if ssh_user else None

    if SSH_FAILED.search(text):
        return _event("ssh_login_failed", "WARNING", source, text, username, source_ip)
    if SSH_SUCCESS.search(text):
        method = re.search(r"Accepted\s+([a-z-]+)", text, re.I)
        return _event(
            "ssh_root_login" if username == "root" else "ssh_login_success",
            "WARNING" if username == "root" else "INFO",
            source, text, username, source_ip,
            method=method.group(1) if method else "unknown",
        )

    sudo_match = SUDO_COMMAND.search(text)
    if sudo_match:
        user = sudo_match.group("user")
        command = sudo_match.group("command").strip()[:1000]
        firewall_command = bool(re.search(
            r"\b(ufw|iptables|ip6tables|nft)\b.*(?:\b(add|delete|insert|flush|set|enable|disable|allow|deny|reject)\b|\s-[ADIRF]\b)",
            command, re.I,
        ))
        return _event(
            "sudo_command", "INFO", source, text, username=user,
            command=command, security_tag="firewall_change" if firewall_command else None,
        )

    audit_action = re.search(r"type=(USER_ADD|USER_DEL|USER_MGMT|GROUP_ADD|GROUP_DEL|GROUP_MGMT)", text, re.I)
    action_match = USER_ACTION.search(text)
    command_action = re.search(r"\b(useradd|userdel|usermod|groupadd|groupdel|groupmod)\b", text)
    if audit_action or action_match or command_action:
        action = (
            audit_action.group(1).lower() if audit_action
            else action_match.group("action").lower().replace(" ", "_") if action_match
            else command_action.group(1).lower()
        )
        account_match = re.search(r"(?:acct|name|user|group)=['\\\"]?([A-Za-z0-9_.-]+)", text, re.I)
        action_account = re.search(
            r"(?:new|delete|modified)\s+(?:user|group)\s+[\'\\\"]?([A-Za-z0-9_.-]+)",
            text, re.I,
        )
        if account_match:
            account = account_match.group(1)
        elif action_account:
            account = action_account.group(1)
        elif action_match:
            account = action_match.group("account")
        else:
            account = None
        event_type = "group_account_change" if "group" in action.lower() else "user_account_change"
        return _event(event_type, "WARNING", source, text, username=account, action=action)

    if FIREWALL_CHANGE.search(text):
        return _event("firewall_change", "WARNING", source, text, username=username)
    return None


def parse_fail2ban_line(line, source="fail2ban"):
    match = FAIL2BAN_ACTION.search(line)
    if not match:
        return None
    action = match.group("action").lower()
    return _event(
        "fail2ban_ban" if action == "ban" else "fail2ban_unban",
        "WARNING" if action == "ban" else "INFO",
        source, line.strip(), source_ip=match.group("ip"), jail=match.group("jail"),
    )


def parse_firewall_line(line, source="firewall"):
    match = UFW_EVENT.search(line)
    if not match:
        return None
    ip_match = IP_PATTERN.search(line)
    return _event(
        "firewall_packet_event", "WARNING" if match.group("action").upper() != "ALLOW" else "INFO",
        source, line.strip(), source_ip=ip_match.group("ip") if ip_match else None,
        action=match.group("action").upper(),
    )


def _tail_new_lines(path, state):
    """Read only bytes appended since last scan; tolerate rotation/truncation."""
    path = Path(path)
    try:
        stat = path.stat()
        if not path.is_file():
            return []
        key = str(path)
        entry = state.get(key, {})
        offset = entry.get("offset", 0) if entry.get("inode") == stat.st_ino else 0
        if offset > stat.st_size:
            offset = 0
        with path.open("rb") as file:
            file.seek(offset)
            content = file.read()
            state[key] = {"inode": stat.st_ino, "offset": file.tell()}
        return content.decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []


def scan_log_paths(paths, parser, state, source_prefix):
    events = []
    for raw_path in paths:
        path = str(raw_path)
        for line in _tail_new_lines(path, state):
            parsed = parser(line, source=path if source_prefix == "auth" else source_prefix)
            if parsed:
                events.append(parsed)
                if parsed.get("security_tag") == "firewall_change":
                    events.append(_event(
                        "firewall_change", "WARNING", parsed["source"],
                        parsed["details"].get("command", line.strip()),
                        username=parsed.get("username"),
                    ))
    return events


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def initialize_file_baseline(paths, baseline_path=None):
    """Explicitly establish a baseline; this does not alter monitored files."""
    baseline = {}
    for raw_path in paths:
        path = str(raw_path)
        try:
            stat = os.stat(path, follow_symlinks=False)
            if not os.path.isfile(path) or os.path.islink(path):
                continue
            baseline[path] = {
                "sha256": _sha256_file(path), "size": stat.st_size,
                "mtime_ns": getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9)),
            }
        except OSError:
            continue
    _write_json(baseline_path or BASELINE_FILE, baseline)
    return baseline


def check_sensitive_files(paths, baseline_path=None, state_path=None, state_data=None):
    """Compare configured files to a SHA-256 baseline and emit only state transitions."""
    baseline_file = baseline_path or BASELINE_FILE
    baseline = _read_json(baseline_file, {})
    state_file = state_path or STATE_FILE
    state = state_data if state_data is not None else _read_json(state_file, {})
    reported = state.setdefault("integrity_reported", {})
    events = []
    if not baseline:
        return events
    for raw_path in paths:
        path = str(raw_path)
        previous = baseline.get(path)
        if previous is None:
            continue
        try:
            stat = os.stat(path, follow_symlinks=False)
            if os.path.islink(path) or not os.path.isfile(path):
                current = None
            else:
                current = {
                    "sha256": _sha256_file(path), "size": stat.st_size,
                    "mtime_ns": getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9)),
                }
        except FileNotFoundError:
            current = None
        except OSError as error:
            marker = "unreadable"
            if reported.get(path) != marker:
                events.append(_event("sensitive_file_unreadable", "WARNING", "file_integrity", str(error), path=path))
                reported[path] = marker
            continue

        if current is None:
            marker = "missing"
            if reported.get(path) != marker:
                events.append(_event("sensitive_file_deleted", "CRITICAL", "file_integrity", "Sensitive file is missing or is a symlink", path=path))
            reported[path] = marker
        elif current["sha256"] != previous.get("sha256"):
            marker = current["sha256"]
            if reported.get(path) != marker:
                events.append(_event(
                    "sensitive_file_modified", "CRITICAL", "file_integrity",
                    "SHA-256 differs from baseline", path=path,
                    previous_sha256=previous.get("sha256"), current_sha256=current["sha256"],
                ))
            reported[path] = marker
        else:
            reported.pop(path, None)
    if state_data is None:
        _write_json(state_file, state)
    return events


def detect_anomalies(events, failed_threshold=5, window_minutes=5):
    """Flag repeated SSH failures by source IP in the supplied recent event batch."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=max(1, int(window_minutes)))
    counts = {}
    for event in events:
        if event.get("event_type") != "ssh_login_failed" or not event.get("source_ip"):
            continue
        try:
            stamp = datetime.fromisoformat(event["timestamp_utc"].replace("Z", "+00:00"))
        except (KeyError, ValueError, TypeError):
            continue
        if stamp >= cutoff:
            counts[event["source_ip"]] = counts.get(event["source_ip"], 0) + 1
    anomalies = []
    for ip, count in counts.items():
        if count >= max(1, int(failed_threshold)):
            anomalies.append(_event(
                "ssh_bruteforce_suspected", "CRITICAL", "anomaly_detection",
                f"{count} failed SSH attempts from {ip} within {window_minutes} minutes",
                source_ip=ip, attempts=count, window_minutes=window_minutes,
            ))
    return anomalies


def get_fail2ban_status():
    """Read Fail2ban status when installed; never starts/stops or changes jails."""
    try:
        result = subprocess.run(
            ["fail2ban-client", "status"], capture_output=True, text=True,
            timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"installed": False, "available": False, "jails": [], "error": "fail2ban-client unavailable"}
    if result.returncode != 0:
        return {"installed": True, "available": False, "jails": [], "error": result.stderr.strip()[:300]}
    match = re.search(r"Jail list:\s*(.*)", result.stdout)
    jails = [item.strip() for item in match.group(1).split(",") if item.strip()] if match else []
    return {"installed": True, "available": True, "jails": jails}


def collect_security_events(config=None, logger=None, state_path=None, db_path=None):
    """Incrementally collect supported security logs and persist normalized events."""
    logger = logger or logging.getLogger("serverpulse.security")
    options = dict(DEFAULT_CONFIG)
    if isinstance(config, dict):
        options.update(config)
    if not options.get("enabled", True):
        return []
    state_file = state_path or STATE_FILE
    state = _read_json(state_file, {})
    events = []
    events.extend(scan_log_paths(options.get("auth_log_paths", []), parse_auth_line, state, "auth"))
    events.extend(scan_log_paths(options.get("audit_log_paths", []), parse_auth_line, state, "audit"))
    events.extend(scan_log_paths(options.get("fail2ban_log_paths", []), parse_fail2ban_line, state, "fail2ban"))
    events.extend(scan_log_paths(options.get("firewall_log_paths", []), parse_firewall_line, state, "firewall"))
    baseline_file = Path(options.get("baseline_file", BASELINE_FILE))
    integrity_interval = max(60, int(options.get("file_integrity_interval_seconds", 300)))
    try:
        last_check = datetime.fromisoformat(state.get("last_integrity_check", "1970-01-01T00:00:00+00:00"))
    except (TypeError, ValueError):
        last_check = datetime(1970, 1, 1, tzinfo=timezone.utc)
    if baseline_file.exists() and datetime.now(timezone.utc) - last_check >= timedelta(seconds=integrity_interval):
        events.extend(check_sensitive_files(
            options.get("sensitive_files", []), baseline_path=baseline_file,
            state_path=state_file, state_data=state,
        ))
        state["last_integrity_check"] = _utc_now()
    max_events = max(1, min(int(options.get("max_events_per_scan", 500)), 5000))
    events = events[:max_events]
    try:
        save_events(events, db_path=db_path)
        recent_failures = get_events(
            limit=2000, event_type="ssh_login_failed", db_path=db_path
        )
        anomalies = detect_anomalies(
            recent_failures,
            options.get("failed_login_threshold", 5),
            options.get("failed_login_window_minutes", 5),
        )
        alert_state = state.setdefault("last_bruteforce_alerts", {})
        window_seconds = max(60, int(options.get("failed_login_window_minutes", 5)) * 60)
        accepted_anomalies = []
        for anomaly in anomalies:
            ip = anomaly.get("source_ip")
            previous = alert_state.get(ip)
            try:
                previous_time = datetime.fromisoformat(previous) if previous else None
            except (TypeError, ValueError):
                previous_time = None
            if previous_time and datetime.now(timezone.utc) - previous_time < timedelta(seconds=window_seconds):
                continue
            accepted_anomalies.append(anomaly)
            alert_state[ip] = anomaly["timestamp_utc"]
        events.extend(accepted_anomalies)
        save_events(accepted_anomalies, db_path=db_path)
        _write_json(state_file, state)
    except (OSError, sqlite3.Error, TypeError, ValueError):
        logger.exception("Security event collection persistence failed")
    return events


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="ServerPulse read-only Linux security monitor")
    parser.add_argument("--init-baseline", action="store_true", help="Create SHA-256 baseline for configured sensitive files")
    parser.add_argument("--config", default=str(BASE_DIR / "config.json"), help="Dashboard config.json path")
    args = parser.parse_args()
    config_data = _read_json(args.config, {})
    security_config = config_data.get("security", {})
    if args.init_baseline:
        paths = security_config.get("sensitive_files", DEFAULT_CONFIG["sensitive_files"])
        baseline = initialize_file_baseline(paths)
        print(f"Initialized SHA-256 baseline for {len(baseline)} files: {BASELINE_FILE}")
    else:
        events = collect_security_events(security_config)
        print(f"Collected {len(events)} new security events.")
