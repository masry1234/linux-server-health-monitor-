## Advanced Security Monitoring - initial implementation

- Added incremental, rotation-aware auth/audit/firewall/Fail2ban log collection.
- Added normalized SSH failed/successful/root login, sudo, user/group, UFW, firewall change, and Fail2ban ban/unban events.
- Added SQLite `security_events` table in the existing database file without altering telemetry tables.
- Added SSH brute-force threshold detection across collection cycles with alert deduplication.
- Added SHA-256 sensitive-file baseline and change/deletion detection.
- Added read-only Fail2ban status inspection and Security dashboard page/API.
- Added critical security event dispatch through the existing notification channels.
- Added unit tests for parsing, incremental reads, persistence, integrity checks, anomaly detection, and dashboard integration.


