## Advanced Security Monitoring - initial implementation

- Added incremental, rotation-aware auth/audit/firewall/Fail2ban log collection.
- Added normalized SSH failed/successful/root login, sudo, user/group, UFW, firewall change, and Fail2ban ban/unban events.
- Added SQLite `security_events` table in the existing database file without altering telemetry tables.
- Added SSH brute-force threshold detection across collection cycles with alert deduplication.
- Added SHA-256 sensitive-file baseline and change/deletion detection.
- Added read-only Fail2ban status inspection and Security dashboard page/API.
- Added critical security event dispatch through the existing notification channels.
- Added unit tests for parsing, incremental reads, persistence, integrity checks, anomaly detection, and dashboard integration.




## Unreleased — Installation and portability

- Added a one-command Linux installer with Python/venv checks and runtime setup.
- Added start/stop helpers and optional systemd user-service installation.
- Added safe uninstall behavior; runtime data is only purged with an explicit flag.
- Changed the dashboard default bind address to loopback (`127.0.0.1`) and made host/port configurable through environment variables.
- Removed a duplicate nested JavaScript `setMetric` helper that caused the dashboard feature test to fail.
- Added portable default configuration without WSL-specific mount exclusions.
- Excluded local runtime logs and security state from the source package.
