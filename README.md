# Linux Server Health Monitor

A Python-based Linux system monitoring and diagnostics tool that collects system metrics, evaluates resource health, inspects system services, and generates JSON and HTML reports.

Designed for Linux troubleshooting, system administration, and infrastructure monitoring practice.

## Features

- **System Resources:** CPU usage, logical CPU count, RAM, swap, disk utilization, load average, and uptime.
- **Network Diagnostics:** Network interfaces, IP addresses, and listening TCP/UDP sockets.
- **Process Monitoring:** Identifies top processes by CPU usage.
- **Service Diagnostics:** Checks configured services, systemd state, and failed units when available.
- **Log Diagnostics:** Collects recent system errors and warnings when supported by the environment.
- **Health Checks:** Evaluates resource usage against configurable thresholds.
- **JSON Reports:** Prints JSON output or saves it to a file.
- **HTML Dashboard:** Generates a standalone HTML report for readable system-health summaries.
- **Logging:** Supports rotating log files with a fallback to standard error.
- **Automated Tests:** Unit tests for monitoring, diagnostics, reporting, and logging.
- **Code Quality:** Ruff linting and GitHub Actions continuous integration.

## Requirements

- Python 3
- Linux, or Ubuntu under WSL2
- Git
- `jq` (optional, for inspecting JSON reports)
- `systemd` and other Linux utilities for certain diagnostic features

Some features depend on available system utilities, permissions, and the host environment.

## Installation

Clone the repository:

```bash
git clone https://github.com/masry1234/linux-server-health-monitor-.git
cd linux-server-health-monitor-
```

Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

No additional Python package installation is required unless the project dependencies change.

## Usage

Run the monitor and display the report:

```bash
python health_monitor.py
```

Print the report as JSON:

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

Generate both JSON and HTML reports:

```bash
python health_monitor.py --output report.json --html report.html
```

Display available command-line options:

```bash
python health_monitor.py --help
```

### Inspect JSON Reports

If `jq` is installed, inspect selected report sections:

```bash
jq '.health' report.json
jq '.listening_sockets' report.json
jq '.system_diagnostics.errors' report.json
```

## Testing

Run the automated unit tests:

```bash
python -m unittest -v test_health_monitor.py
```

Run Ruff lint checks if installed:

```bash
python -m ruff check .
```

Check the latest workflow results in the repository's **Actions** tab on GitHub.

## Platform Compatibility

The project is currently developed and tested in Ubuntu under Windows Subsystem for Linux 2 (WSL2).

WSL2 does not expose every hardware metric, kernel interface, or systemd feature available on a native Linux server. Some diagnostics may therefore be unavailable or reflect WSL2 limitations rather than actual server faults.

For production Linux environments, validate the results against the target distribution, available permissions, and installed system utilities.

## Security Considerations

- Run with the minimum privileges required.
- Review reports and logs before sharing them publicly.
- Do not commit credentials, access tokens, or sensitive infrastructure data.
- Treat process details, IP addresses, listening sockets, and system logs as potentially sensitive information.
- Review any commands before running them with elevated privileges.

## Roadmap

- [ ] Validate compatibility across additional Linux distributions.
- [ ] Add optional alert delivery through email or other notification channels.
- [ ] Improve integration testing on native Linux systems.
- [ ] Add historical metric storage and trend analysis.
- [ ] Document deployment and scheduling options for periodic monitoring.

## License

No license has been selected yet. All rights reserved by default until a license is added to the repository.
