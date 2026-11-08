# Linux Server Health Monitor

A Python-based Linux system monitoring and diagnostics tool designed to collect system metrics, evaluate resource health, inspect system services, analyze disk I/O activity, and generate structured JSON and HTML reports.

Built for Linux system administration, server troubleshooting, infrastructure monitoring, and security operations.

## Key Features

### System Resource Monitoring
- CPU utilization and logical CPU count.
- CPU I/O wait monitoring.
- RAM and Swap utilization.
- Disk capacity and filesystem utilization.
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

### Process Monitoring
- Top processes by CPU utilization.
- Process-level resource information.

### System Service Diagnostics
- Configurable service monitoring.
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
- Historical health data stored in JSONL format.

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
| JSON / JSONL | Data storage and reporting |
| HTML / CSS | Health dashboard |
| unittest | Automated testing |
| Ruff | Code quality |
| GitHub Actions | Continuous integration |

## Project Structure

```text
linux-server-health-monitor/
│
├── .github/
│   └── workflows/
│
├── logs/
│
├── health_monitor.py
├── test_health_monitor.py
├── config.json
├── health_history.jsonl
├── report.json
├── report.html
├── README.md
└── .gitignore
```

## Requirements

- Python 3
- Linux or Ubuntu under WSL2
- Git
- systemd and standard Linux utilities for supported diagnostics
- jq (optional, for JSON inspection)

Some monitoring features depend on kernel interfaces, system permissions, and available system utilities.

## Installation

Clone the repository:

```bash
git clone https://github.com/masry1234/linux-server-health-monitor-.git

cd linux-server-health-monitor-
```

Create a virtual environment:

```bash
python3 -m venv .venv

source .venv/bin/activate
```

## Usage

Run the monitoring tool:

```bash
python health_monitor.py
```

Display JSON output:

```bash
python health_monitor.py --json
```

Save a JSON report:

```bash
python health_monitor.py --output report.json
```

Generate an HTML dashboard:

```bash
python health_monitor.py --html report.html
```

Generate both reports:

```bash
python health_monitor.py --output report.json --html report.html
```

Display available options:

```bash
python health_monitor.py --help
```

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

The current development test suite contains 38 unit tests.

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

## License

No license has been selected yet.

Until a license is added, the project remains under the default copyright terms.
