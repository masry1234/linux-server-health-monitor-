
import argparse
import json
import logging
import math
import os
import platform
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

VERSION = "0.5.0"
MAX_LOG_MESSAGES = 20

LOG_DIR = Path(__file__).with_name("logs")
LOG_FILE = LOG_DIR / "health_monitor.log"
LOG_MAX_BYTES = 1_048_576
LOG_BACKUP_COUNT = 5

DEFAULT_THRESHOLDS = {
    "cpu": {"warning": 80.0, "critical": 95.0},
    "memory": {"warning": 80.0, "critical": 95.0},
    "disk": {"warning": 80.0, "critical": 95.0},
}


def setup_logging():
    """Configure rotating file logging with a stderr fallback."""
    logger = logging.getLogger("health_monitor")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
    except OSError as error:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.warning(
            "File logging unavailable; using stderr instead: %s",
            error,
        )
        return logger

    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger

def run_command(command, timeout=8):
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass

    return None


def load_thresholds(config_path=None):
    """Load and validate resource thresholds from JSON configuration."""
    config_file = (
        Path(config_path)
        if config_path is not None
        else Path(__file__).with_name("config.json")
    )

    thresholds = {
        resource: values.copy()
        for resource, values in DEFAULT_THRESHOLDS.items()
    }

    if not config_file.exists():
        return thresholds

    try:
        with config_file.open(encoding="utf-8") as file:
            config = json.load(file)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(
            f"Cannot read configuration file {config_file}: {error}"
        ) from error

    if not isinstance(config, dict):
        raise TypeError("Configuration must be a JSON object.")

    configured_thresholds = config.get("thresholds")

    if not isinstance(configured_thresholds, dict):
        raise TypeError(
            "Configuration must contain a 'thresholds' object."
        )

    for resource in DEFAULT_THRESHOLDS:
        values = configured_thresholds.get(resource)

        if not isinstance(values, dict):
            raise TypeError(
                f"Missing or invalid thresholds for '{resource}'."
            )

        warning = values.get("warning")
        critical = values.get("critical")

        for name, value in (
            ("warning", warning),
            ("critical", critical),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 0 <= value <= 100
            ):
                raise ValueError(
                    f"{resource}.{name} must be a finite number "
                    "from 0 to 100."
                )

        if warning >= critical:
            raise ValueError(
                f"{resource}: warning must be lower than critical."
            )

        thresholds[resource] = {
            "warning": float(warning),
            "critical": float(critical),
        }

    return thresholds


def get_cpu_times():
    try:
        with open("/proc/stat", encoding="utf-8") as file:
            fields = file.readline().split()

        if not fields or fields[0] != "cpu":
            return None

        values = [int(value) for value in fields[1:]]
        idle = values[3] if len(values) > 3 else 0
        iowait = values[4] if len(values) > 4 else 0

        return sum(values), idle + iowait

    except (OSError, ValueError, IndexError):
        return None


def get_cpu_usage():
    first = get_cpu_times()
    if first is None:
        return None

    time.sleep(1)
    second = get_cpu_times()
    if second is None:
        return None

    total_delta = second[0] - first[0]
    idle_delta = second[1] - first[1]

    if total_delta <= 0 or idle_delta < 0:
        return None

    usage = 100 * (1 - idle_delta / total_delta)
    return round(max(0.0, min(100.0, usage)), 1)


def get_memory():
    info = {}

    try:
        with open("/proc/meminfo", encoding="utf-8") as file:
            for line in file:
                key, value = line.split(":", 1)
                info[key] = int(value.strip().split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        return {
            "status": "unavailable",
            "total_bytes": None,
            "available_bytes": None,
            "used_bytes": None,
            "usage_percent": None,
            "swap_total_bytes": None,
            "swap_used_bytes": None,
            "swap_usage_percent": None,
        }

    total = info.get("MemTotal")
    available = info.get("MemAvailable")
    swap_total = info.get("SwapTotal")
    swap_free = info.get("SwapFree")

    if total is None or available is None or total <= 0:
        used = usage = None
    else:
        used = max(0, total - available)
        usage = round(100 * used / total, 1)

    if swap_total is None or swap_free is None:
        swap_used = swap_usage = None
    else:
        swap_used = max(0, swap_total - swap_free)
        swap_usage = (
            round(100 * swap_used / swap_total, 1)
            if swap_total
            else 0.0
        )

    return {
        "status": "ok",
        "total_bytes": total,
        "available_bytes": available,
        "used_bytes": used,
        "usage_percent": usage,
        "swap_total_bytes": swap_total,
        "swap_used_bytes": swap_used,
        "swap_usage_percent": swap_usage,
    }


def get_disks():
    output = run_command([
        "df", "-PT",
        "-x", "tmpfs",
        "-x", "devtmpfs",
        "-x", "overlay",
        "-x", "squashfs",
        "-x", "9p",
        "-x", "drvfs",
    ])

    if output is None:
        return []

    disks = []

    for line in output.splitlines()[1:]:
        fields = line.split(None, 6)
        if len(fields) != 7:
            continue

        filesystem, fs_type, total, used, available, percent, mount = fields

        try:
            disks.append({
                "filesystem": filesystem,
                "type": fs_type,
                "mountpoint": mount,
                "total_bytes": int(total) * 1024,
                "used_bytes": int(used) * 1024,
                "available_bytes": int(available) * 1024,
                "usage_percent": int(percent.rstrip("%")),
            })
        except ValueError:
            continue

    return disks


def get_network():
    output = run_command(["ip", "-j", "address", "show"])
    if output is None:
        return None

    try:
        items = json.loads(output)
    except (ValueError, TypeError):
        return None

    interfaces = []

    for item in items:
        addresses = item.get("addr_info", [])

        interfaces.append({
            "name": item.get("ifname"),
            "state": item.get("operstate", "UNKNOWN"),
            "mac_address": item.get("address"),
            "ipv4": [
                address["local"]
                for address in addresses
                if address.get("family") == "inet"
                and "local" in address
            ],
            "ipv6": [
                address["local"]
                for address in addresses
                if address.get("family") == "inet6"
                and "local" in address
            ],
        })

    return interfaces


def get_listening_sockets():
    output = run_command(["ss", "-H", "-lntup"])

    if output is None:
        return {
            "status": "unavailable",
            "count": None,
            "items": [],
        }

    sockets = []

    for line in output.splitlines():
        fields = line.split(None, 6)
        if len(fields) < 5:
            continue

        sockets.append({
            "protocol": fields[0].lower(),
            "state": fields[1],
            "local_address": fields[4],
            "peer_address": fields[5] if len(fields) > 5 else None,
            "process": fields[6] if len(fields) > 6 else None,
        })

    return {
        "status": "ok",
        "count": len(sockets),
        "items": sockets,
    }


def get_services():
    names = ("ssh", "sshd", "docker", "nginx")
    services = {}

    if run_command(["systemctl", "--version"]) is None:
        return {name: "UNKNOWN" for name in names}

    for service in names:
        output = run_command([
            "systemctl", "show", service,
            "--property=LoadState,ActiveState",
            "--no-pager",
        ])

        if output is None:
            services[service] = "UNKNOWN"
            continue

        properties = dict(
            line.split("=", 1)
            for line in output.splitlines()
            if "=" in line
        )

        load_state = properties.get("LoadState", "unknown")
        active_state = properties.get("ActiveState", "unknown")

        if load_state == "not-found":
            services[service] = "NOT_INSTALLED"
        elif active_state == "active":
            services[service] = "OK"
        elif active_state == "inactive":
            services[service] = "INACTIVE"
        else:
            services[service] = active_state.upper()

    return services


def get_top_processes():
    output = run_command([
        "ps", "-eo", "pid,comm,pcpu,pmem",
        "--sort=-pcpu",
    ])

    if output is None:
        return []

    processes = []

    for line in output.splitlines()[1:6]:
        fields = line.split(None, 3)
        if len(fields) != 4:
            continue

        try:
            processes.append({
                "pid": int(fields[0]),
                "name": fields[1],
                "cpu_percent": float(fields[2]),
                "memory_percent": float(fields[3]),
            })
        except ValueError:
            continue

    return processes


def get_os_info():
    info = {}

    try:
        with open("/etc/os-release", encoding="utf-8") as file:
            for line in file:
                if "=" in line:
                    key, value = line.rstrip().split("=", 1)
                    info[key] = value.strip('"')
    except OSError:
        pass

    return {
        "distribution": info.get("PRETTY_NAME", "Unknown"),
        "kernel": platform.release(),
        "architecture": platform.machine(),
        "python_version": platform.python_version(),
    }


def get_uptime():
    try:
        with open("/proc/uptime", encoding="utf-8") as file:
            return int(float(file.read().split()[0]))
    except (OSError, ValueError, IndexError):
        return None


def get_load_average():
    try:
        return list(os.getloadavg())
    except (OSError, AttributeError):
        return None


def get_system_diagnostics():
    """Collect systemd state, failed units, and bounded journal samples."""
    try:
        result = subprocess.run(
            ["systemctl", "is-system-running"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )

        systemd_state = (
            result.stdout.strip() or result.stderr.strip()
        ).lower()

        valid_states = {
            "running",
            "degraded",
            "starting",
            "stopping",
            "offline",
        }

        if systemd_state in valid_states:
            systemd_status = systemd_state
        else:
            systemd_status = "unavailable"

    except (OSError, subprocess.TimeoutExpired):
        systemd_status = "unavailable"

    failed_output = run_command([
        "systemctl", "--failed",
        "--no-legend", "--plain",
    ])

    if failed_output is None:
        failed_units_status = "unavailable"
        failed_units = []
    else:
        failed_units_status = "ok"
        failed_units = []

        for line in failed_output.splitlines():
            fields = line.split(None, 4)
            if not fields:
                continue

            failed_units.append({
                "unit": fields[0],
                "load": fields[1] if len(fields) > 1 else None,
                "active": fields[2] if len(fields) > 2 else None,
                "sub": fields[3] if len(fields) > 3 else None,
                "description": fields[4] if len(fields) > 4 else None,
            })

    def read_journal(priority):
        output = run_command([
            "journalctl",
            "-p", priority,
            "-n", str(MAX_LOG_MESSAGES),
            "--no-pager",
            "-o", "short-iso",
        ])

        if output is None:
            return {
                "status": "unavailable",
                "count": None,
                "messages": [],
            }

        messages = [
            line for line in output.splitlines()
            if line.strip()
        ]

        return {
            "status": "ok",
            "count": len(messages),
            "messages": messages,
        }

    return {
        "systemd_status": systemd_status,
        "failed_units_status": failed_units_status,
        "failed_units": failed_units,
        "errors": read_journal("err"),
        "warnings": read_journal("warning"),
        "journal_note": (
            "Journal visibility depends on system configuration "
            "and the permissions of the current user."
        ),
    }


def assess_health(report, thresholds=None):
    checks = []
    thresholds = thresholds or DEFAULT_THRESHOLDS

    def add_check(name, value, resource):
        limits = thresholds[resource]

        if value is None:
            status = "UNKNOWN"
        elif value >= limits["critical"]:
            status = "CRITICAL"
        elif value >= limits["warning"]:
            status = "WARNING"
        else:
            status = "OK"

        checks.append({
            "name": name,
            "status": status,
            "value": value,
            "warning_threshold": limits["warning"],
            "critical_threshold": limits["critical"],
        })

    add_check("cpu", report["cpu"]["usage_percent"], "cpu")
    add_check(
        "memory",
        report["memory"].get("usage_percent"),
        "memory",
    )

    for disk in report["disks"]:
        add_check(
            f"disk:{disk['mountpoint']}",
            disk["usage_percent"],
            "disk",
        )

    statuses = [item["status"] for item in checks]

    if "CRITICAL" in statuses:
        overall = "CRITICAL"
    elif "WARNING" in statuses:
        overall = "WARNING"
    elif "UNKNOWN" in statuses or not statuses:
        overall = "UNKNOWN"
    else:
        overall = "OK"

    return {
        "status": overall,
        "checks": checks,
    }


def collect_report(logger=None):
    logger = logger or logging.getLogger("health_monitor")
    logger.info("Starting health report collection")

    try:
        thresholds = load_thresholds()
    except (ValueError, TypeError) as error:
        logger.error("Configuration validation failed: %s", error)
        raise

    report = {
        "schema_version": "1.0",
        "monitor_version": VERSION,
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "system": {
            "hostname": socket.gethostname(),
            "os": get_os_info(),
            "uptime_seconds": get_uptime(),
            "cpu_count": os.cpu_count(),
            "load_average": get_load_average(),
        },
        "cpu": {
            "usage_percent": get_cpu_usage(),
        },
        "memory": get_memory(),
        "disks": get_disks(),
        "network": get_network(),
        "listening_sockets": get_listening_sockets(),
        "services": get_services(),
        "top_processes": get_top_processes(),
        "system_diagnostics": get_system_diagnostics(),
    }

    report["thresholds"] = thresholds
    report["health"] = assess_health(report, thresholds)

    health_status = report["health"]["status"]
    if health_status == "CRITICAL":
        logger.critical("Health assessment status: %s", health_status)
    elif health_status == "WARNING" or health_status == "UNKNOWN":
        logger.warning("Health assessment status: %s", health_status)
    else:
        logger.info("Health assessment status: %s", health_status)

    logger.info("Health report collection completed")
    return report


def print_report(report):
    system = report["system"]
    memory = report["memory"]
    diagnostics = report["system_diagnostics"]

    print("\n" + "=" * 52)
    print("          LINUX SERVER HEALTH REPORT")
    print("=" * 52)
    print(f"Version:       {report['monitor_version']}")
    print(f"Hostname:      {system['hostname']}")
    print(f"OS:            {system['os']['distribution']}")
    print(f"Kernel:        {system['os']['kernel']}")
    print(f"CPU cores:     {system['cpu_count']}")
    print(f"CPU usage:     {report['cpu']['usage_percent']}%")
    print(f"RAM usage:     {memory.get('usage_percent')}%")
    print(f"Swap usage:    {memory.get('swap_usage_percent')}%")
    print(f"Load average:  {system['load_average']}")
    print(f"Uptime:        {system['uptime_seconds']} seconds")

    print("\nRESOURCE HEALTH")
    print(f"Overall: {report['health']['status']}")

    for check in report["health"]["checks"]:
        print(
            f"{check['name']:<24} {check['status']:<8} "
            f"value={check['value']} "
            f"(warning={check['warning_threshold']}%, "
            f"critical={check['critical_threshold']}%)"
        )

    print("\nCONFIGURED THRESHOLDS")
    for resource, limits in report["thresholds"].items():
        print(
            f"{resource:<10} warning={limits['warning']}% "
            f"critical={limits['critical']}%"
        )

    print("\nSYSTEM DIAGNOSTICS")
    print(f"systemd: {diagnostics['systemd_status']}")
    print(f"Failed units check: {diagnostics['failed_units_status']}")

    for unit in diagnostics["failed_units"]:
        print(
            f"FAILED: {unit['unit']} "
            f"| {unit.get('description') or ''}"
        )

    print("\nRECENT ERROR LOGS")
    errors = diagnostics["errors"]

    if errors["status"] != "ok":
        print("Error logs unavailable")
    elif not errors["messages"]:
        print("No error messages returned")
    else:
        for message in errors["messages"]:
            print(message)

    print("\nRECENT WARNING LOGS")
    warnings = diagnostics["warnings"]

    if warnings["status"] != "ok":
        print("Warning logs unavailable")
    elif not warnings["messages"]:
        print("No warning messages returned")
    else:
        for message in warnings["messages"]:
            print(message)

    print("\nDISKS")
    for disk in report["disks"]:
        print(
            f"{disk['mountpoint']}: {disk['usage_percent']}% used "
            f"({disk['type']})"
        )

    print("\nNETWORK")
    if report["network"] is None:
        print("Network information unavailable")
    else:
        for interface in report["network"]:
            addresses = interface["ipv4"] + interface["ipv6"]
            print(
                f"{interface['name']}: {interface['state']} "
                f"| IP: {', '.join(addresses) or 'None'}"
            )

    print("\nLISTENING TCP / UDP SOCKETS")
    sockets = report["listening_sockets"]

    if sockets["status"] != "ok":
        print("Socket information unavailable")
    elif not sockets["items"]:
        print("No sockets reported")
    else:
        for item in sockets["items"]:
            print(
                f"{item['protocol'].upper():<5} "
                f"{item['state']:<8} "
                f"{item['local_address']:<28} "
                f"process={item['process'] or 'unknown'}"
            )

    print(f"Total sockets reported: {sockets['count']}")

    print("\nSERVICES")
    for name, status in report["services"].items():
        print(f"{name:<10} {status}")

    print("\nTOP PROCESSES BY CPU")
    for process in report["top_processes"]:
        print(
            f"PID {process['pid']:<7} {process['name']:<20} "
            f"CPU {process['cpu_percent']}% "
            f"RAM {process['memory_percent']}%"
        )

    print("\n" + "=" * 52)



def generate_html_report(report):
    """Generate a standalone HTML health report."""
    import html

    diagnostics = report.get("system_diagnostics") or {}
    health = report.get("health") or {}
    system = report.get("system") or {}

    resource_status = html.escape(str(health.get("status", "unknown")))
    systemd_status = html.escape(
        str(diagnostics.get("systemd_status", "unknown"))
    )
    generated = html.escape(str(report.get("timestamp_utc", "unknown")))
    hostname = html.escape(str(system.get("hostname", "unknown")))
    data = html.escape(json.dumps(report, indent=2, ensure_ascii=False))

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Linux Server Health Report</title>
<style>
:root {{
  color-scheme: dark;
  font-family: system-ui, sans-serif;
  background: #0b1325;
  color: #e8edf7;
}}
body {{ max-width: 1100px; margin: 0 auto; padding: 2rem; }}
header, section {{ background: #131f35; border: 1px solid #2b3b56;
  border-radius: 12px; padding: 1.25rem; margin-bottom: 1rem; }}
h1, h2 {{ margin-top: 0; }}
.status {{ font-size: 1.1rem; font-weight: 700; }}
small {{ color: #aab8ce; }}
pre {{ white-space: pre-wrap; overflow-wrap: anywhere; }}
</style>
</head>
<body>
<header>
  <h1>Linux Server Health Report</h1>
  <p>Hostname: {hostname}</p>
  <p class="status">Resource health: {resource_status}</p>
  <p class="status">Systemd status: {systemd_status}</p>
  <small>Generated: {generated}</small>
</header>
<section>
  <h2>Full diagnostic report</h2>
  <pre>{data}</pre>
</section>
</body>
</html>
"""

def main():
    parser = argparse.ArgumentParser(
        description="Linux Server Health Monitor"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print report as JSON",
    )
    parser.add_argument(
        "--output",
        help="Save report to a JSON file",
    )
    parser.add_argument(
        "--html",
        help="Save report as an HTML file",
    )

    args = parser.parse_args()
    logger = setup_logging()
    logger.info("Linux Server Health Monitor v%s started", VERSION)

    try:
        report = collect_report(logger)
    except (ValueError, TypeError) as error:
        logger.error("Unable to generate health report: %s", error)
        parser.error(str(error))
    except Exception:
        logger.exception("Unexpected error while generating health report")
        raise

    if args.html:
        try:
            Path(args.html).write_text(
                generate_html_report(report),
                encoding="utf-8",
            )
            logger.info("HTML report saved to %s", args.html)
        except OSError:
            logger.exception("Failed to save HTML report")
            raise

    if args.output:
        try:
            Path(args.output).write_text(
                json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            logger.info("JSON report saved to %s", args.output)
        except OSError:
            logger.exception("Failed to save JSON report")
            raise

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print_report(report)

        if args.output:
            print(f"\nJSON saved to: {args.output}")

    logger.info("Linux Server Health Monitor finished")


if __name__ == "__main__":
    main()

