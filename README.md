# Linux Server Health Monitor

A Python-based Linux system monitoring and diagnostics tool designed to collect system metrics, evaluate resource health, inspect system services, analyze disk I/O activity, and generate structured JSON and HTML reports.

Built for Linux system administration, server troubleshooting, infrastructure monitoring, and security operations.

## Key Features

### System Resource Monitoring
- CPU utilization, per-logical-core utilization, and logical CPU count.
- CPU I/O wait monitoring.
- RAM and Swap utilization.
- Disk capacity, filesystem utilization, and inode utilization.
- System load average and uptime.

### Disk I/O Monitoring
- Disk read/write throughput.
- Read/write operation counters.
- Read/write time metrics.
- Disk I/O rate calculations.
- Configurable disk I/O health thresholds.
- Handling of unavailable or invalid metric data.

### Network Diagnostics
- Network interface information.
- IP address detection.
- Listening TCP and UDP sockets.
- Established TCP connection endpoints and per-interface packet/error/drop counters.

### Process Monitoring
- Top 25 processes by CPU utilization with dashboard-side search.
- Process-level resource information.

### System Service Diagnostics
- Configurable service monitoring.
- Configurable dashboard refresh interval (1–300 seconds).
- Systemd service status checks.
- Failed systemd unit detection.
- System health degradation assessment.

### Health Assessment
- Configurable CPU, memory, disk, and disk I/O thresholds.
- Warning and critical health states.
- Historical health data collection.
- Health status evaluation.

### Reporting
- JSON report generation.
- Standalone HTML health dashboard.
- System diagnostics summaries.
- Historical health data stored in SQLite with a 10,000-record retention limit.
- Automatic one-time import of valid legacy JSONL history; the original JSONL file is preserved.

### Logging and Reliability
- Rotating log files.
- Standard error fallback.
- Graceful handling of unavailable system metrics.

### Development and Quality
- Automated Python unit tests.
- Ruff linting.
- GitHub Actions continuous integration.

## Technology Stack

| Technology | Purpose |
|---|---|
| Python 3 | Core application |
| Linux / Ubuntu | Operating environment |
| Bash | System administration |
| `/proc` | System metrics collection |
| systemctl | Service diagnostics |
| ip / ss | Network diagnostics |
| df / free | Resource monitoring |
| JSON / JSONL / SQLite | Configuration, legacy import, history storage, and reporting |
| HTML / CSS | Health dashboard |
| unittest | Automated testing |
| Ruff | Code quality |
| GitHub Actions | Continuous integration |

## Project Structure

```text
linux-server-health-monitor/
├── .github/workflows/
├── config.json
├── config.example.json
├── security_config.example.json
├── .env.example
├── requirements.txt
├── requirements-dev.txt
├── install.sh
├── start.sh
├── stop.sh
├── uninstall.sh
├── health_monitor.py
├── live_dashboard.py
├── security_monitor.py
├── notification_service.py
├── test_*.py
├── CHANGELOG.md
└── README.md
```

## Requirements

- Python 3.10 or newer, including the `venv` module.
- Ubuntu 20.04/22.04/24.04 or another modern Linux distribution; WSL2 is suitable for development.
- Git (for cloning and updates).
- systemd and standard Linux utilities for supported diagnostics; `jq` is optional.
- No third-party Python runtime packages are currently required.

Some monitoring features depend on kernel interfaces, system permissions, and available system utilities.

## Installation

Clone the repository and run the installer:

```bash
git clone https://github.com/masry1234/linux-server-health-monitor-.git
cd linux-server-health-monitor-
chmod +x install.sh start.sh stop.sh uninstall.sh
./install.sh
```

The installer checks the Python version, creates `.venv`, installs the runtime requirements, creates the log directory, preserves existing configuration, and performs a Python compilation check. If Ubuntu cannot create a virtual environment, install the missing package and rerun:

```bash
sudo apt update
sudo apt install -y python3-venv
./install.sh
```

Start the dashboard:

```bash
./start.sh
```

Open `http://127.0.0.1:8000`. Stop a foreground dashboard with `CTRL+C`.

### Optional systemd user service

To install and start ServerPulse as a user-level systemd service:

```bash
./install.sh --service
systemctl --user status serverpulse.service
```

To stop it:

```bash
./stop.sh
```

The service runs as the installing user, not as root. Some security log and system diagnostics may therefore require additional read permissions. The installer does not change firewall rules, SSH settings, users, or system-wide permissions.

### Uninstall

Remove the virtual environment and optional user service while preserving configuration and collected data:

```bash
./uninstall.sh
```

To explicitly remove runtime databases, state files, and logs as well:

```bash
./uninstall.sh --purge-data
```

This destructive option does not remove `config.json` or `.env`.

## Configuration

Monitoring thresholds are configurable through:

```text
config.json
```

This allows resource health evaluation to be adjusted according to monitoring requirements.

## Example JSON Inspection

Using jq:

```bash
jq '.health' report.json

jq '.listening_sockets' report.json

jq '.system_diagnostics.errors' report.json
```

## Testing

Run the automated unit tests:

```bash
python -m unittest -v
```

Run code quality checks:

```bash
ruff check .
```

The current development test suite contains 97 unit tests.

## Platform Compatibility

The project is currently developed and tested using Ubuntu on Windows Subsystem for Linux 2 (WSL2).

WSL2 may not expose all hardware metrics, kernel interfaces, or systemd capabilities available on native Linux servers.

For production environments, validate monitoring results against the target Linux distribution and available system permissions.

## Security Considerations

- Run with the minimum privileges required.
- Review reports before sharing them.
- Never commit credentials, access tokens, or sensitive infrastructure information.
- Treat IP addresses, process details, listening sockets, and system logs as potentially sensitive.
- Review commands before executing them with elevated privileges.

## Roadmap

- [ ] Expand compatibility testing across Linux distributions.
- [ ] Add optional Telegram and email notifications.
- [ ] Improve native Linux integration testing.
- [ ] Add advanced historical trend visualization.
- [ ] Add deployment and scheduling documentation.
- [ ] Expand monitoring coverage and diagnostic capabilities.

## Deployment and network exposure

The dashboard binds to `127.0.0.1` by default. This avoids exposing its unauthenticated HTTP interface to other machines. `SERVERPULSE_HOST` and `SERVERPULSE_PORT` can be set in `.env` for local runs. Do not bind to `0.0.0.0` or expose port 8000 to an untrusted network until authentication, TLS, and access controls are implemented. The optional systemd user service intentionally overrides the bind address to `127.0.0.1`.

## License

No license has been selected yet.

Until a license is added, the project remains under the default copyright terms.

## ServerPulse Dashboard 0.9.0 additions

The live dashboard now provides dedicated in-page views for Dashboard, Metrics, Alerts, Disks, Network, Services, Processes, Logs, History, and Settings. Sidebar navigation updates the URL hash, supports browser back/forward navigation, and does not reload the monitoring process.

The Metrics view adds live CPU I/O wait, RAM and swap details, active connection count, and a per-device disk I/O table. The Network view retains the live RX/TX chart, cumulative interface counters, peak throughput statistics, and per-interface inventory.

## ServerPulse Dashboard 0.7.0 additions

The 0.7 dashboard keeps the existing local dashboard and JSON APIs, and adds:

- Editable CPU, memory, and disk warning/critical thresholds through the Settings section (`GET/POST /api/config`).
- Configurable excluded mount points. WSL Windows drive mounts (`9p` under `/mnt/<letter>`) remain visible in inventory but are excluded from Linux server capacity health and the disk KPI by default.
- Extended telemetry cards for CPU I/O wait, swap utilization, and free space on `/`.
- Search and severity filtering for alert history, plus CSV export for alerts and health history.
- Live history table refresh, including readable local timestamps.
- WSL-only filtering for known host-integration noise in journal samples; application and service errors remain visible.
- Inline SVG interface icons; no external icon image files or network-loaded assets are required.

Run locally:

```bash
.venv/bin/python -m py_compile health_monitor.py live_dashboard.py
.venv/bin/python -m unittest -q
.venv/bin/python live_dashboard.py
```

Open `http://localhost:8000`. Settings are written to `config.json`; keep a backup before editing configuration manually.

### Platform notes

Network throughput is calculated from Linux interface byte counters and needs at least two collection samples. A displayed zero can be correct when the interface is idle. WSL exposes host-mounted Windows drives as `9p`; those mounts are retained in the inventory but not treated as Linux server disks. systemd and journal availability still depends on the host's systemd configuration and permissions.


## ServerPulse 0.8.0 network observability

The dashboard now includes historical network RX/TX throughput, peak download/upload rates for the selected chart range, cumulative received/sent byte counters, and per-interface traffic statistics. The network history is derived from the existing JSONL health report history; cumulative byte counters represent interface counters since the operating system last reset them.

Network throughput requires at least two samples. The first sample establishes a baseline and can correctly show zero throughput.


## Current development status

The `1.0.0-dev` core branch adds per-core CPU, filesystem inode metrics, expanded network telemetry, established TCP connection details, process search, configurable refresh, and SQLite history with legacy JSONL migration. This is a development build: validate it on the target Ubuntu Server version before production deployment.

## Telegram and Email Notifications

ServerPulse can send every alert state transition (WARNING, CRITICAL, and RECOVERED) to Telegram and/or email. Delivery runs in a background thread so SMTP or network timeouts do not block metric collection. Existing alert-state deduplication prevents repeated notifications while a resource remains in the same state.

Credentials are read only from process environment variables. They are not stored in `config.json` or exposed through the dashboard API. Start from the example file:

```bash
cp .env.example .env
chmod 600 .env
```

Edit `.env` with the values for the channels you want to enable. Load it into the current shell before starting the dashboard:

```bash
set -a
. ./.env
set +a
python live_dashboard.py
```

Set `TELEGRAM_ENABLED=true` and provide `TELEGRAM_BOT_TOKEN` plus `TELEGRAM_CHAT_ID` to enable Telegram. Set `EMAIL_ENABLED=true` and provide `SMTP_HOST`, `SMTP_FROM`, and comma-separated `SMTP_TO` recipients to enable email. Authenticated SMTP uses `SMTP_USERNAME` and `SMTP_PASSWORD`; port 587 with STARTTLS is the default. For implicit TLS, use port 465 with `SMTP_USE_SSL=true` and `SMTP_USE_TLS=false`.

For systemd deployments, use a root-owned environment file outside the repository and reference it with `EnvironmentFile=/etc/serverpulse/serverpulse.env` in the service unit. Restrict its permissions (for example, `chmod 600`) and never commit `.env` or real credentials. `.env` is ignored by Git; `.env.example` contains placeholders only.

Delivery failures are logged without exposing tokens or SMTP passwords. Missing credentials skip only the affected channel. A failed delivery does not stop monitoring or alter the alert state.


## Historical Analytics

The dashboard's Performance Analytics now queries SQLite history for selectable periods (5 minutes, 15 minutes, 1 hour, 6 hours, 24 hours, and 7 days). The analytics response includes sample count, bounded/downsampled time-series, average/minimum/maximum values for CPU, memory, disk I/O, and network throughput, plus statistical CPU/memory outliers. Outlier detection uses a median/MAD baseline and a minimum 10-percentage-point deviation; it is a heuristic for investigation, not a predictive model or a security verdict.

The API endpoint is `GET /api/analytics?range_minutes=60`. Supported ranges are `5`, `15`, `60`, `360`, `1440`, and `10080`. The endpoint returns at most 300 chart points by default while preserving the total `sample_count` and aggregate calculations over all samples in the selected period. Historical retention remains limited to the latest 10,000 reports.


## Advanced Security Monitoring (Ubuntu 20.04 / 22.04 / 24.04)

ServerPulse includes a read-only host security collector for:
- SSH failed, successful, and root login events.
- Sudo command auditing, including commands that mutate firewall rules.
- User and group creation, deletion, and modification events from available auth/audit logs.
- UFW packet events and firewall changes recorded by sudo/audit logs.
- Fail2ban ban/unban log events and a read-only `fail2ban-client status` check.
- SHA-256 integrity checks for selected sensitive system files.
- SSH brute-force detection based on failed attempts accumulated across collection cycles.

Security events are stored in the existing `health_history.sqlite3` database in a separate `security_events` table. The existing telemetry schema and history are not modified.

### File integrity baseline

Create the initial baseline deliberately, after verifying the host is in a trusted state:

```bash
sudo .venv/bin/python security_monitor.py --init-baseline
```

The baseline is stored in `security_file_baseline.json`. Changes are reported; the baseline is not automatically rewritten after a change. Review the event and reinitialize the baseline only after validating the change.

### Permissions

The collector needs read access to the configured authentication/audit/firewall/Fail2ban logs. Access to files such as `/etc/shadow` also requires elevated read permission. For production, run the collector under a dedicated, tightly controlled service identity with only the required read permissions; do not expose the loopback dashboard to untrusted networks.

### API

- `GET /api/security/events?limit=200`
- `GET /api/security/events?event_type=ssh_login_failed`
- `GET /api/security/status`

The Security page in the dashboard displays recent events and Fail2ban availability. No account, firewall, SSH, or Fail2ban settings are changed automatically.

### Log-source limitations

- Ubuntu uses `/var/log/auth.log`; `/var/log/secure` is included for later RHEL compatibility.
- Auditd-specific user/group and firewall command events require auditd rules to be installed and enabled. ServerPulse only reads the audit log; it does not install or change audit rules.
- Firewall packet events are parsed from UFW log entries. nftables/iptables configuration changes are detected when recorded by sudo or auditd.
- If the host does not have Fail2ban or a given firewall/log source installed, that source is reported unavailable rather than treated as a failure.
