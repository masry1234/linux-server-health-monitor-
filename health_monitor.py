
import argparse
import json
import os
import platform
import socket
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

VERSION = "0.3.0"
WARNING_THRESHOLD = 80.0
CRITICAL_THRESHOLD = 95.0
MAX_LOG_MESSAGES = 20


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
            if swap_total else 0.0
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
                a["local"] for a in addresses
                if a.get("family") == "inet" and "local" in a
            ],
            "ipv6": [
                a["local"] for a in addresses
                if a.get("family") == "inet6" and "local" in a
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
            result.stdout.strip()
            or result.stderr.strip()
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


def assess_health(report):
    checks = []

    def add_check(name, value):
        if value is None:
            status = "UNKNOWN"
        elif value >= CRITICAL_THRESHOLD:
            status = "CRITICAL"
        elif value >= WARNING_THRESHOLD:
            status = "WARNING"
        else:
            status = "OK"

        checks.append({
            "name": name,
            "status": status,
            "value": value,
        })

    add_check("cpu", report["cpu"]["usage_percent"])
    add_check("memory", report["memory"].get("usage_percent"))

    for disk in report["disks"]:
        add_check(
            f"disk:{disk['mountpoint']}",
            disk["usage_percent"],
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


def collect_report():
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

    report["health"] = assess_health(report)
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
            f"value={check['value']}"
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


def main():
    parser = argparse.ArgumentParser(
        description="Linux Server Health Monitor"
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Print report as JSON",
    )
    parser.add_argument(
        "--output",
        help="Save report to a JSON file",
    )

    args = parser.parse_args()
    report = collect_report()

    if args.output:
        Path(args.output).write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print_report(report)

        if args.output:
            print(f"\nJSON saved to: {args.output}")


if __name__ == "__main__":
    main()

