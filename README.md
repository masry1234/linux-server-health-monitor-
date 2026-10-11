# Linux Server Health Monitor

A Python-based Linux system health monitoring tool that collects
system resource metrics, checks resource health, and generates
readable reports for troubleshooting and system administration.

## Overview

Linux Server Health Monitor helps administrators inspect the
current state of a Linux environment from a single command.

The tool is currently tested in Ubuntu running under WSL2.
Some system diagnostics may behave differently in WSL2 than
on a native Linux server.

## Features

- CPU usage and logical CPU core count
- RAM and swap utilization
- Disk usage for mounted filesystems
- System load average and uptime
- Network interface status and IP addresses
- Listening TCP and UDP sockets
- Configurable service availability checks
- Top processes by CPU usage
- Systemd status and failed-unit diagnostics when available
- Recent system error and warning logs when available
- Resource health checks and overall health status
- JSON report generation
- Automated unit tests

## Requirements

- Python 3
- Linux environment or WSL2 with Ubuntu
- Git
- jq for inspecting JSON reports from the terminal

Some diagnostic features depend on the available Linux
utilities and permissions.

## Installation

Clone the repository:

```
git clone https://github.com/masry1234/linux-server-health-monitor.git
cd linux-server-health-monitor
```

Create and activate a virtual environment:

```
python3 -m venv .venv
source .venv/bin/activate
```

Install dependencies if required by the project.

## Usage

Run the health monitor:

```
python health_monitor.py
```

Generate a JSON report:

```
python health_monitor.py --output report.json
```

Inspect the overall health status:

```
jq '.health' report.json
```

Inspect listening sockets:

```
jq '.listening_sockets' report.json
```

Inspect system diagnostic errors:

```
jq '.system_diagnostics.errors' report.json
```

## Testing

Run the unit tests:

```
python -m unittest -v test_health_monitor.py
```

## Example Output

The report includes system information, resource utilization,
health-check results, disk usage, network information,
listening sockets, service status, and top processes.

The exact output depends on the host operating system,
available permissions, and installed system utilities.

## Platform Notes

This project is currently developed and tested in Ubuntu
under Windows Subsystem for Linux 2 (WSL2).

WSL2 may not expose all hardware information or systemd
features available on a native Linux server. Some kernel
messages and service checks may therefore reflect WSL2
limitations rather than actual server faults.

## Security Considerations

- Run with the minimum privileges required.
- Review diagnostic logs before sharing reports publicly.
- Do not commit credentials, tokens, or sensitive system data.
- Avoid running commands that modify the host system unless
  explicitly intended and reviewed.

## Roadmap

- [ ] Improve compatibility with native Linux servers
- [ ] Add configurable CPU, RAM, and disk alert thresholds
- [ ] Add structured logging and clearer error handling
- [ ] Improve socket-to-process identification
- [ ] Add continuous integration with GitHub Actions
- [ ] Expand automated test coverage

## License

License information will be added when a project license
has been selected.
