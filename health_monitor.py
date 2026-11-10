import argparse
import json
import logging
import math
import os
import platform
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

VERSION = "1.0.0-dev"
MAX_LOG_MESSAGES = 20

LOG_DIR = Path(__file__).with_name("logs")
LOG_FILE = LOG_DIR / "health_monitor.log"
LOG_MAX_BYTES = 1_048_576
LOG_BACKUP_COUNT = 5

DEFAULT_THRESHOLDS = {
    "cpu": {"warning": 80.0, "critical": 95.0},
    "memory": {"warning": 80.0, "critical": 95.0},
    "disk": {"warning": 80.0, "critical": 95.0},
    "disk_io": {
        "read_warning_mbps": 100.0,
        "read_critical_mbps": 200.0,
        "write_warning_mbps": 100.0,
        "write_critical_mbps": 200.0,
    },
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
    thresholds["disk_io"] = DEFAULT_THRESHOLDS["disk_io"].copy()

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

    for resource in ("cpu", "memory", "disk"):
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
    disk_io_values = configured_thresholds.get("disk_io")

    if disk_io_values is not None:
        if not isinstance(disk_io_values, dict):
            raise TypeError(
                "Thresholds for 'disk_io' must be an object."
            )

        disk_io_keys = (
            "read_warning_mbps",
            "read_critical_mbps",
            "write_warning_mbps",
            "write_critical_mbps",
        )

        validated_disk_io = {}

        for key in disk_io_keys:
            value = disk_io_values.get(key)

            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(
                    f"disk_io.{key} must be a finite "
                    "non-negative number."
                )

            validated_disk_io[key] = float(value)

        for operation in ("read", "write"):
            warning = validated_disk_io[f"{operation}_warning_mbps"]
            critical = validated_disk_io[f"{operation}_critical_mbps"]

            if warning >= critical:
                raise ValueError(
                    f"disk_io {operation}: warning must be "
                    "lower than critical."
                )

        thresholds["disk_io"] = validated_disk_io

    return thresholds


def get_cpu_times():
    try:
        with open("/proc/stat", encoding="utf-8") as file:
            fields = file.readline().split()

        if not fields or fields[0] != "cpu":
            return None

        values = [int(value) for value in fields[1:]]

        if len(values) < 5 or any(value < 0 for value in values):
            return None

        idle = values[3]
        iowait = values[4]

        return {
            "total": sum(values),
            "idle": idle + iowait,
            "iowait": iowait,
        }

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

    total_delta = second["total"] - first["total"]
    idle_delta = second["idle"] - first["idle"]

    if total_delta <= 0 or idle_delta < 0:
        return None

    usage = 100 * (1 - idle_delta / total_delta)
    return round(max(0.0, min(100.0, usage)), 1)
def get_cpu_usage_per_core(interval=1.0):
    """Return per-core CPU utilization percentages from /proc/stat."""
    if interval <= 0:
        raise ValueError("interval must be greater than zero")

    def read_cores():
        values = {}
        try:
            with open("/proc/stat", encoding="utf-8") as file:
                for line in file:
                    fields = line.split()
                    if not fields or not fields[0].startswith("cpu"):
                        if values:
                            break
                        continue
                    name = fields[0]
                    if name == "cpu":
                        continue
                    if not name[3:].isdigit() or len(fields) < 5:
                        continue
                    counters = [int(value) for value in fields[1:]]
                    if any(value < 0 for value in counters):
                        continue
                    values[name] = (sum(counters), counters[3] + (counters[4] if len(counters) > 4 else 0))
        except (OSError, ValueError):
            return None
        return values or None

    first = read_cores()
    if first is None:
        return None
    time.sleep(interval)
    second = read_cores()
    if second is None:
        return None

    result = {}
    for name, (total, idle) in second.items():
        previous = first.get(name)
        if previous is None:
            continue
        total_delta = total - previous[0]
        idle_delta = idle - previous[1]
        if total_delta <= 0 or idle_delta < 0:
            continue
        result[name] = round(max(0.0, min(100.0, 100 * (1 - idle_delta / total_delta))), 1)
    return result


def get_cpu_iowait():
    first = get_cpu_times()
    if first is None:
        return None

    time.sleep(1)
    second = get_cpu_times()
    if second is None:
        return None

    total_delta = second["total"] - first["total"]
    iowait_delta = second["iowait"] - first["iowait"]

    if total_delta <= 0 or iowait_delta < 0:
        return None

    percentage = 100 * iowait_delta / total_delta
    return round(max(0.0, min(100.0, percentage)), 1)

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

        # Exclude WSL system mounts and pseudo filesystems.
        if mount == "/init" or mount.startswith("/usr/lib/"):
            continue

        # Keep Windows drive mounts, but ignore other 9p mounts.
        if fs_type == "9p" and not (
            mount.startswith("/mnt/")
            and len(mount) == 6
            and mount[-1].isalpha()
        ):
            continue

        try:
            is_wsl_drive = (
                fs_type == "9p"
                and mount.startswith("/mnt/")
                and len(mount) == 6
                and mount[-1].isalpha()
            )
            disks.append({
                "filesystem": filesystem,
                "type": fs_type,
                "mountpoint": mount,
                "total_bytes": int(total) * 1024,
                "used_bytes": int(used) * 1024,
                "available_bytes": int(available) * 1024,
                "usage_percent": int(percent.rstrip("%")),
                "monitored": not is_wsl_drive,
                "excluded_reason": "WSL Windows drive mount" if is_wsl_drive else None,
            })
        except ValueError:
            continue

    inode_output = run_command(["df", "-Pi", "-x", "tmpfs", "-x", "devtmpfs", "-x", "overlay", "-x", "squashfs"])
    inode_by_mount = {}
    if inode_output:
        for line in inode_output.splitlines()[1:]:
            fields = line.split(None, 5)
            if len(fields) != 6:
                continue
            try:
                inode_by_mount[fields[5]] = {
                    "total": int(fields[1]),
                    "used": int(fields[2]),
                    "available": int(fields[3]),
                    "usage_percent": int(fields[4].rstrip("%")),
                }
            except ValueError:
                continue
    for disk in disks:
        disk["inodes"] = inode_by_mount.get(disk["mountpoint"])

    return disks

def get_disk_io_counters():
    counters = {}

    try:
        with open("/proc/diskstats", encoding="utf-8") as file:
            for line in file:
                fields = line.split()

                if len(fields) < 14:
                    continue

                device = fields[2]
                if device.startswith(("ram", "loop")):
                    continue

                try:
                    reads_completed = int(fields[3])
                    sectors_read = int(fields[5])
                    read_time_ms = int(fields[6])
                    writes_completed = int(fields[7])
                    sectors_written = int(fields[9])
                    write_time_ms = int(fields[10])
                    io_time_ms = int(fields[12])
                except ValueError:
                    continue

                values = (
                    reads_completed,
                    sectors_read,
                    read_time_ms,
                    writes_completed,
                    sectors_written,
                    write_time_ms,
                    io_time_ms,
                )
                if any(value < 0 for value in values):
                    continue

                counters[device] = {
                    "read_bytes": sectors_read * 512,
                    "write_bytes": sectors_written * 512,
                    "reads_completed": reads_completed,
                    "read_time_ms": read_time_ms,
                    "writes_completed": writes_completed,
                    "write_time_ms": write_time_ms,
                    "io_time_ms": io_time_ms,
                }

    except OSError:
        return None

    return counters


def get_disk_io_rates(interval=1):
    if interval <= 0:
        raise ValueError("interval must be greater than zero")

    first = get_disk_io_counters()
    if first is None:
        return None

    time.sleep(interval)

    second = get_disk_io_counters()
    if second is None:
        return None

    rates = {}

    for device, current in second.items():
        previous = first.get(device)
        if previous is None:
            continue

        read_delta = current["read_bytes"] - previous["read_bytes"]
        write_delta = current["write_bytes"] - previous["write_bytes"]
        reads_delta = (
            current["reads_completed"] - previous["reads_completed"]
        )
        writes_delta = (
            current["writes_completed"] - previous["writes_completed"]
        )
        read_time_delta = current["read_time_ms"] - previous["read_time_ms"]
        write_time_delta = (
            current["write_time_ms"] - previous["write_time_ms"]
        )
        io_time_delta = current["io_time_ms"] - previous["io_time_ms"]

        deltas = (
            read_delta,
            write_delta,
            reads_delta,
            writes_delta,
            read_time_delta,
            write_time_delta,
            io_time_delta,
        )

        if any(delta < 0 for delta in deltas):
            continue

        interval_ms = interval * 1000

        rates[device] = {
            "read_bytes_per_sec": round(read_delta / interval, 1),
            "write_bytes_per_sec": round(write_delta / interval, 1),
            "read_latency_ms": (
                round(read_time_delta / reads_delta, 2)
                if reads_delta > 0
                else None
            ),
            "write_latency_ms": (
                round(write_time_delta / writes_delta, 2)
                if writes_delta > 0
                else None
            ),
            "io_utilization_percent": round(
                io_time_delta / interval_ms * 100,
                2,
            ),
        }

    return rates

# Network byte counters are sampled between dashboard collection cycles.
_network_sample_lock = __import__("threading").Lock()
_network_previous = {}
_network_previous_time = None

def get_network_io_rates():
    """Return aggregate RX/TX throughput in MB/s from /proc/net/dev."""
    global _network_previous, _network_previous_time
    now = time.monotonic()
    current = {}
    try:
        with open("/proc/net/dev", encoding="utf-8") as file:
            for line in file.readlines()[2:]:
                if ":" not in line:
                    continue
                name, values = line.split(":", 1)
                fields = values.split()
                if len(fields) < 9:
                    continue
                interface = name.strip()
                if interface == "lo":
                    continue
                if len(fields) < 16:
                    continue
                current[interface] = {
                    "rx_bytes": int(fields[0]),
                    "rx_packets": int(fields[1]),
                    "rx_errors": int(fields[2]),
                    "rx_dropped": int(fields[3]),
                    "tx_bytes": int(fields[8]),
                    "tx_packets": int(fields[9]),
                    "tx_errors": int(fields[10]),
                    "tx_dropped": int(fields[11]),
                }
    except (OSError, ValueError):
        return {"rx_mbps": 0.0, "tx_mbps": 0.0, "interfaces": {}, "sample_ready": False}

    with _network_sample_lock:
        previous = _network_previous
        previous_time = _network_previous_time
        elapsed = now - previous_time if previous_time is not None else 0
        rx_total = tx_total = 0.0
        per_interface = {}
        for name, values in current.items():
            old = previous.get(name, values)
            rx = values["rx_bytes"]
            tx = values["tx_bytes"]
            rx_rate = max(0, rx - old["rx_bytes"]) / elapsed / 1_000_000 if elapsed > 0 else 0.0
            tx_rate = max(0, tx - old["tx_bytes"]) / elapsed / 1_000_000 if elapsed > 0 else 0.0
            per_interface[name] = {
                "rx_mbps": round(rx_rate, 3),
                "tx_mbps": round(tx_rate, 3),
                "total_rx_bytes": rx,
                "total_tx_bytes": tx,
                "rx_packets": values["rx_packets"],
                "tx_packets": values["tx_packets"],
                "rx_errors": values["rx_errors"],
                "tx_errors": values["tx_errors"],
                "rx_dropped": values["rx_dropped"],
                "tx_dropped": values["tx_dropped"],
            }
            rx_total += rx_rate
            tx_total += tx_rate
        _network_previous = current
        _network_previous_time = now
    return {
        "rx_mbps": round(rx_total, 3),
        "tx_mbps": round(tx_total, 3),
        "interfaces": per_interface,
        "total_rx_bytes": sum(item["rx_bytes"] for item in current.values()),
        "total_tx_bytes": sum(item["tx_bytes"] for item in current.values()),
        "sample_ready": elapsed > 0,
    }


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


def get_network_connections(limit=500):
    """Return established TCP connections with local/peer endpoints."""
    output = run_command(["ss", "-H", "-tn", "state", "established"])
    if output is None:
        return {"status": "unavailable", "count": None, "items": []}

    items = []
    total_count = 0
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        total_count += 1
        if len(items) >= limit:
            continue
        items.append({
            "state": "ESTABLISHED",
            "recv_q": fields[0],
            "send_q": fields[1],
            "local_address": fields[2],
            "peer_address": fields[3],
        })
    return {"status": "ok", "count": total_count, "items": items, "truncated": total_count > len(items)}


def get_active_connection_count():
    """Count established TCP connections; None means ss is unavailable."""
    connections = get_network_connections()
    return connections["count"]


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


def get_services(service_config=None):
    if service_config is None:
        service_config = {
            "ssh": {"enabled": True, "unit": "ssh"},
            "sshd": {"enabled": True, "unit": "sshd"},
            "docker": {"enabled": True, "unit": "docker"},
            "nginx": {"enabled": True, "unit": "nginx"},
        }

    services = {}
    enabled_services = {
        name: settings
        for name, settings in service_config.items()
        if settings.get("enabled", True)
    }

    if not enabled_services:
        return {}

    if run_command(["systemctl", "--version"]) is None:
        return {name: "UNKNOWN" for name in enabled_services}

    for name, settings in enabled_services.items():
        unit = settings.get("unit", name)
        output = run_command([
            "systemctl", "show", unit,
            "--property=LoadState,ActiveState",
            "--no-pager",
        ])

        if output is None:
            services[name] = "UNKNOWN"
            continue

        properties = dict(
            line.split("=", 1)
            for line in output.splitlines()
            if "=" in line
        )

        load_state = properties.get("LoadState", "unknown")
        active_state = properties.get("ActiveState", "unknown")

        if load_state == "not-found":
            services[name] = "NOT_INSTALLED"
        elif active_state == "active":
            services[name] = "OK"
        elif active_state == "inactive":
            services[name] = "INACTIVE"
        else:
            services[name] = active_state.upper()

    return services



def get_top_processes():
    output = run_command([
        "ps", "-eo", "pid,user,pcpu,pmem,args",
        "--sort=-pcpu",
    ])

    if output is None:
        return []

    processes = []

    for line in output.splitlines()[1:26]:
        fields = line.split(None, 4)
        if len(fields) != 5:
            continue

        try:
            # args may contain spaces; split leaves the full command in field 4.
            cpu_value, memory_value, command = fields[2], fields[3], fields[4]
            processes.append({
                "pid": int(fields[0]),
                "user": fields[1],
                "name": command.split()[0] if command else "unknown",
                "command": command,
                "cpu_percent": float(cpu_value),
                "memory_percent": float(memory_value),
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




def is_wsl_environment():
    """Return True when the current kernel identifies itself as WSL."""
    try:
        release = Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8").lower()
        return "microsoft" in release or "wsl" in release
    except OSError:
        return False


def is_wsl_host_noise(message):
    """Identify noisy WSL/Hyper-V host integration messages."""
    text = str(message).lower()
    markers = (
        "wsl (104) error: checkconnection",
        "wsl (105) error: checkconnection",
        "hv_balloon:",
        "hyper-v does not support order",
        "no config space access function found",
        "device-mapper: core: config_ima_disable_htable",
        "failed to connect to system scope bus via local transport",
    )
    return any(marker in text for marker in markers)

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

        failed_units_status = (
            "issues_found" if failed_units else "ok"
        )

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

        messages = [line for line in output.splitlines() if line.strip()]
        suppressed = 0
        if is_wsl_environment():
            filtered = [line for line in messages if is_wsl_host_noise(line)]
            suppressed = len(filtered)
            messages = [line for line in messages if not is_wsl_host_noise(line)]

        return {
            "status": "ok",
            "count": len(messages),
            "suppressed_wsl_noise": suppressed,
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


def assess_health(report, thresholds=None, exclude_mountpoints=None):
    checks = []
    thresholds = thresholds or DEFAULT_THRESHOLDS
    exclude_mountpoints = set(exclude_mountpoints or [])
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

    disk_io_limits = thresholds.get("disk_io")
    disk_io = report.get("disk_io")

    if disk_io_limits:
        if not disk_io :
            checks.append({
                "name": "disk_io",
                "status": "UNKNOWN",
                "value": None,
                "warning_threshold": None,
                "critical_threshold": None,
            })
        else:
            for device, metrics in disk_io.items():
                for operation in ("read", "write"):
                    value = metrics.get(f"{operation}_bytes_per_sec")
                    value_mbps = (
                        value / (1024 * 1024)
                        if value is not None else None
                    )
                    warning = disk_io_limits[f"{operation}_warning_mbps"]
                    critical = disk_io_limits[f"{operation}_critical_mbps"]

                    if value_mbps is None:
                        status = "UNKNOWN"
                    elif value_mbps >= critical:
                        status = "CRITICAL"
                    elif value_mbps >= warning:
                        status = "WARNING"
                    else:
                        status = "OK"

                    checks.append({
                        "name": f"disk_io:{device}:{operation}",
                        "status": status,
                        "value": round(value_mbps, 2) if value_mbps is not None else None,
                        "warning_threshold": warning,
                        "critical_threshold": critical,
                    })

    for disk in report["disks"]:
        if disk["mountpoint"] in exclude_mountpoints or disk.get("monitored") is False:
            continue
        if disk.get("type") == "9p" and str(disk.get("mountpoint", "")).startswith("/mnt/"):
            continue

        add_check(
            f"disk:{disk['mountpoint']}",
            disk["usage_percent"],
            "disk",
        )

    diagnostics = report.get("system_diagnostics") or {}
    failed_units_status = diagnostics.get("failed_units_status")
    failed_units = diagnostics.get("failed_units") or []

    if failed_units_status == "issues_found" or failed_units:
        checks.append({
            "name": "failed_systemd_units",
            "status": "WARNING",
            "value": len(failed_units) if failed_units else "issues_found",
            "warning_threshold": 0,
            "critical_threshold": None,
        })

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

HISTORY_FILE = Path(__file__).with_name("health_history.jsonl")
HISTORY_DB_FILE = Path(__file__).with_name("health_history.sqlite3")
HISTORY_RETENTION_RECORDS = 10000


def _connect_history_db():
    connection = sqlite3.connect(HISTORY_DB_FILE, timeout=10)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp_utc TEXT NOT NULL,
            payload TEXT NOT NULL
        )
    """)
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_reports_timestamp ON reports(timestamp_utc)"
    )
    return connection


def _migrate_jsonl_history(connection, logger=None):
    """Import legacy JSONL history once, preserving valid records."""
    count = connection.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
    if count or not HISTORY_FILE.exists():
        return
    logger = logger or logging.getLogger("health_monitor")
    imported = []
    try:
        with HISTORY_FILE.open("r", encoding="utf-8") as file:
            for line in file:
                try:
                    report = json.loads(line)
                    if isinstance(report, dict):
                        stamp = str(report.get("timestamp_utc") or datetime.now(UTC).isoformat())
                        imported.append((stamp, json.dumps(report, ensure_ascii=False, separators=(",", ":"))))
                except json.JSONDecodeError:
                    continue
    except OSError:
        logger.exception("Unable to migrate legacy JSONL history")
        return
    if imported:
        connection.executemany(
            "INSERT INTO reports(timestamp_utc, payload) VALUES (?, ?)", imported
        )
        connection.execute(
            "DELETE FROM reports WHERE id NOT IN (SELECT id FROM reports ORDER BY id DESC LIMIT ?)",
            (HISTORY_RETENTION_RECORDS,),
        )


def save_report_history(report, logger=None):
    """Persist reports in SQLite, migrating legacy JSONL on first write."""
    logger = logger or logging.getLogger("health_monitor")
    try:
        with closing(_connect_history_db()) as connection, connection:
            _migrate_jsonl_history(connection, logger)
            stamp = str(report.get("timestamp_utc") or datetime.now(UTC).isoformat())
            payload = json.dumps(report, ensure_ascii=False, separators=(",", ":"))
            connection.execute(
                "INSERT INTO reports(timestamp_utc, payload) VALUES (?, ?)",
                (stamp, payload),
            )
            connection.execute(
                "DELETE FROM reports WHERE id NOT IN (SELECT id FROM reports ORDER BY id DESC LIMIT ?)",
                (HISTORY_RETENTION_RECORDS,),
            )
    except (OSError, sqlite3.Error, TypeError, ValueError):
        logger.exception("Failed to save health report history")


def show_history(limit=10):
    """Display the most recent health checks from the active history store."""
    reports = load_report_history(max(1, limit))

    if not reports:
        print("No valid health history records found.")
        return

    print(f"\nLatest {min(limit, len(reports))} health checks")
    print("-" * 90)
    print(f"{'Timestamp (UTC)':<33} {'Status':<10} {'CPU %':>8} {'RAM %':>8} {'Disks':>8}")
    print("-" * 90)

    for report in reports[-limit:][::-1]:
        timestamp = report.get("timestamp_utc", "unknown")
        status = report.get("health", {}).get("status", "UNKNOWN")
        cpu = report.get("cpu", {}).get("usage_percent")
        memory = report.get("memory", {}).get("usage_percent")
        disks = report.get("disks", [])

        cpu_text = f"{cpu:.1f}" if isinstance(cpu, (int, float)) else "N/A"
        memory_text = (
            f"{memory:.1f}" if isinstance(memory, (int, float)) else "N/A"
        )

        print(
            f"{timestamp:<33} {status:<10} "
            f"{cpu_text:>8} {memory_text:>8} {len(disks):>8}"
        )

def load_report_history(limit=10):
    """Load recent history from SQLite, falling back to legacy JSONL."""
    if limit < 1:
        return []
    if HISTORY_DB_FILE.exists():
        try:
            with closing(_connect_history_db()) as connection, connection:
                rows = connection.execute(
                    "SELECT payload FROM reports ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            reports = []
            for (payload,) in reversed(rows):
                try:
                    report = json.loads(payload)
                    if isinstance(report, dict):
                        reports.append(report)
                except json.JSONDecodeError:
                    continue
            return reports
        except sqlite3.Error:
            logging.getLogger("health_monitor").exception("Failed to read SQLite history")
    if not HISTORY_FILE.exists():
        return []
    reports = []
    try:
        with HISTORY_FILE.open("r", encoding="utf-8") as file:
            for line in file:
                if not line.strip():
                    continue
                try:
                    report = json.loads(line)
                    if isinstance(report, dict):
                        reports.append(report)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return reports[-limit:]



def _numeric_metric(report, path, default=0.0):
    value = report
    for key in path:
        if not isinstance(value, dict):
            return default
        value = value.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if not math.isfinite(value):
        return default
    return float(value)


def get_history_analytics(range_minutes=60, max_points=300):
    """Return bounded historical time-series, aggregate statistics and outliers."""
    if isinstance(range_minutes, bool) or not isinstance(range_minutes, int):
        raise TypeError("range_minutes must be an integer")
    if range_minutes not in {5, 15, 60, 360, 1440, 10080}:
        raise ValueError("Unsupported analytics range")
    if isinstance(max_points, bool) or not isinstance(max_points, int) or not 10 <= max_points <= 1000:
        raise ValueError("max_points must be between 10 and 1000")

    from datetime import timedelta
    cutoff = (datetime.now(UTC) - timedelta(minutes=range_minutes)).isoformat()
    reports = []
    if HISTORY_DB_FILE.exists():
        try:
            with closing(_connect_history_db()) as connection:
                rows = connection.execute(
                    "SELECT payload FROM reports WHERE timestamp_utc >= ? ORDER BY timestamp_utc ASC",
                    (cutoff,),
                ).fetchall()
            for (payload,) in rows:
                try:
                    report = json.loads(payload)
                    if isinstance(report, dict):
                        reports.append(report)
                except json.JSONDecodeError:
                    continue
        except sqlite3.Error:
            logging.getLogger("health_monitor").exception("Failed to query analytics history")
    else:
        reports = [
            report for report in load_report_history(HISTORY_RETENTION_RECORDS)
            if str(report.get("timestamp_utc", "")) >= cutoff
        ]

    points = []
    for report in reports:
        disk_io = report.get("disk_io") or {}
        read_bps = sum(
            _numeric_metric(device, ("read_bytes_per_sec",))
            for device in disk_io.values() if isinstance(device, dict)
        )
        write_bps = sum(
            _numeric_metric(device, ("write_bytes_per_sec",))
            for device in disk_io.values() if isinstance(device, dict)
        )
        network_io = report.get("network_io") or {}
        points.append({
            "timestamp_utc": report.get("timestamp_utc"),
            "cpu_percent": _numeric_metric(report, ("cpu", "usage_percent")),
            "memory_percent": _numeric_metric(report, ("memory", "usage_percent")),
            "disk_read_mb_s": read_bps / (1024 * 1024),
            "disk_write_mb_s": write_bps / (1024 * 1024),
            "network_rx_mb_s": _numeric_metric(network_io, ("rx_mbps",)),
            "network_tx_mb_s": _numeric_metric(network_io, ("tx_mbps",)),
        })

    metric_keys = (
        "cpu_percent", "memory_percent", "disk_read_mb_s", "disk_write_mb_s",
        "network_rx_mb_s", "network_tx_mb_s",
    )
    summary = {}
    for key in metric_keys:
        values = [point[key] for point in points]
        summary[key] = {
            "average": round(sum(values) / len(values), 3) if values else None,
            "minimum": round(min(values), 3) if values else None,
            "maximum": round(max(values), 3) if values else None,
        }

    anomalies = []
    # Require enough history before statistical detection. A small idle CPU
    # fluctuation should not be treated as an incident just because MAD is 0.
    minimum_baseline_samples = 9
    if len(points) >= minimum_baseline_samples + 1:
        for key in ("cpu_percent", "memory_percent"):
            values = [point[key] for point in points]
            ordered = sorted(values)
            middle = len(ordered) // 2
            median = (ordered[middle] if len(ordered) % 2 else
                      (ordered[middle - 1] + ordered[middle]) / 2)
            deviations = sorted(abs(value - median) for value in values)
            mad_middle = len(deviations) // 2
            mad = (deviations[mad_middle] if len(deviations) % 2 else
                   (deviations[mad_middle - 1] + deviations[mad_middle]) / 2)
            # CPU idle baselines commonly have median/MAD near zero. Use a
            # higher absolute floor for CPU to suppress benign scheduler spikes.
            absolute_floor = 15.0 if key == "cpu_percent" else 10.0
            threshold = median + max(3.0 * mad, absolute_floor)
            for point in points:
                if point[key] > threshold:
                    anomalies.append({
                        "timestamp_utc": point["timestamp_utc"],
                        "metric": key,
                        "value": round(point[key], 3),
                        "baseline_median": round(median, 3),
                        "detection_threshold": round(threshold, 3),
                    })
    anomalies.sort(key=lambda item: item["value"], reverse=True)

    if len(points) > max_points:
        step = (len(points) - 1) / (max_points - 1)
        points = [points[round(index * step)] for index in range(max_points)]

    return {
        "range_minutes": range_minutes,
        "sample_count": len(reports),
        "returned_points": len(points),
        "summary": summary,
        "anomalies": anomalies[:50],
        "series": points,
    }

def collect_report(logger=None):
    logger = logger or logging.getLogger("health_monitor")
    logger.info("Starting health report collection")

    try:
        thresholds = load_thresholds()
        config_path = Path(__file__).with_name("config.json")
        with config_path.open(encoding="utf-8") as file:
            config = json.load(file)

        service_config = config.get("services", {
            "ssh": {"enabled": True, "unit": "ssh"},
            "sshd": {"enabled": True, "unit": "sshd"},
            "docker": {"enabled": True, "unit": "docker"},
            "nginx": {"enabled": True, "unit": "nginx"},
        })
    except (ValueError, TypeError, OSError) as error:
        logger.error("Configuration loading failed: %s", error)
        raise

    disks = get_disks()
    disk_io = get_disk_io_rates()
    network_connections = get_network_connections()
    if isinstance(disk_io, dict):
        for disk in disks:
            source_name = Path(str(disk.get("filesystem", ""))).name
            disk["io"] = disk_io.get(source_name)

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
            "iowait_percent": get_cpu_iowait(),
            "per_core_usage_percent": get_cpu_usage_per_core(),
        },
        "memory": get_memory(),
        "disks": disks,
        "disk_io": disk_io,
        "network": get_network(),
        "network_io": get_network_io_rates(),
        "listening_sockets": get_listening_sockets(),
        "network_connections": network_connections,
        "services": get_services(service_config),
        "top_processes": get_top_processes(),
        "system_diagnostics": get_system_diagnostics(),
        "active_connections": network_connections.get("count"),
    }

    report["thresholds"] = thresholds
    health_config = config.get("health", {})
    exclude_mountpoints = health_config.get(
        "exclude_mountpoints", []
    )
    for disk in report["disks"]:
        if disk.get("mountpoint") in exclude_mountpoints:
            disk["monitored"] = False
            disk["excluded_reason"] = "Configured exclusion"

    report["health"] = assess_health(
        report,
        thresholds,
        exclude_mountpoints,
    )

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
    print(f"CPU iowait:    {report['cpu'].get('iowait_percent')}%")
    print(f"RAM usage:     {memory.get('usage_percent')}%")
    print(f"Swap usage:    {memory.get('swap_usage_percent')}%")
    print(f"Load average:  {system['load_average']}")

    print("\nDISK I/O")
    disk_io = report.get("disk_io")

    if disk_io is None:
        print("Disk I/O data unavailable")
    elif not disk_io:
        print("No disk I/O data available")
    else:
        for device, metrics in disk_io.items():
            read_rate = metrics["read_bytes_per_sec"] / (1024 * 1024)
            write_rate = metrics["write_bytes_per_sec"] / (1024 * 1024)

            print(
                f"{device}: Read {read_rate:.2f} MB/s | "
                f"Write {write_rate:.2f} MB/s"
            )

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
        if resource == "disk_io":
            print(
                "disk_io    "
                f"read_warning={limits['read_warning_mbps']} MB/s, "
                f"read_critical={limits['read_critical_mbps']} MB/s, "
                f"write_warning={limits['write_warning_mbps']} MB/s, "
                f"write_critical={limits['write_critical_mbps']} MB/s"
            )
        else:
            print(
                f"{resource:<10} "
                f"warning={limits['warning']}% "
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



def generate_html_report(report, history=None):
    """Generate a responsive HTML dashboard from a health report."""
    import html

    def esc(value):
        return html.escape(str(value if value is not None else "unknown"))

    def status_class(value):
        normalized = str(value).lower()
        if normalized in {"ok", "healthy", "active"}:
            return "good"
        if normalized in {"warning", "degraded"}:
            return "warning"
        if normalized in {"critical", "failed", "error"}:
            return "critical"
        return "neutral"

    health = report.get("health") or {}
    diagnostics = report.get("system_diagnostics") or {}
    system = report.get("system") or {}
    disks = report.get("disks") or []

    resource_status = health.get("status", "unknown")
    systemd_status = diagnostics.get("systemd_status", "unknown")
    hostname = system.get("hostname", "unknown")
    generated = report.get("timestamp_utc", "unknown")
    checks = health.get("checks") or []

    cards = []
    for check in checks:
        name = str(check.get("name", "Resource"))
        value = check.get("value", "unknown")
        warning = check.get("warning_threshold", "n/a")
        critical = check.get("critical_threshold", "n/a")
        status = check.get("status", "unknown")

        display_name = name
        unit = ""

        if name.startswith("disk_io:"):
            parts = name.split(":")
            if len(parts) == 3:
                _, device, operation = parts
                operation_name = {
                    "read": "Read",
                    "write": "Write",
                }.get(operation, operation.title())
                display_name = (
                    f"Disk {operation_name} Throughput ({device})"
                )
            unit = " MB/s"
        elif name in {"cpu", "memory"} or name.startswith("disk:"):
            unit = "%"

        try:
            numeric_value = float(value)
            if name.startswith("disk_io:"):
                critical_value = float(critical)
                progress = (
                    numeric_value / critical_value * 100
                    if critical_value > 0 else 0
                )
            else:
                progress = numeric_value

            progress = max(0.0, min(100.0, progress))
            progress_width = f"{progress:.1f}"
        except (TypeError, ValueError):
            progress_width = "0"

        cards.append(
            '<article class="metric card">'
            f'<div class="metric-name">{esc(display_name)}</div>'
            f'<div class="metric-value">{esc(value)}{unit}</div>'
            f'<div class="progress-track" role="progressbar" '
            f'aria-label="{esc(display_name)}" aria-valuemin="0" '
            f'aria-valuemax="100" aria-valuenow="{progress_width}">'
            f'<div class="progress-fill {status_class(status)}" '
            f'style="width: {progress_width}%"></div></div>'
            f'<span class="badge {status_class(status)}">{esc(status)}</span>'
            f'<p class="muted">Warning: {esc(warning)}{unit} · '
            f'Critical: {esc(critical)}{unit}</p>'
            '</article>'
        )

    if not cards:
        cards.append('<article class="card">No resource checks available.</article>')

    disk_rows = []
    for disk in disks:
        disk_rows.append(
            "<tr>"
            f"<td>{esc(disk.get('mountpoint'))}</td>"
            f"<td>{esc(disk.get('usage_percent'))}%</td>"
            f"<td>{esc(disk.get('type'))}</td>"
            f"<td>{'Excluded from health' if disk.get('monitored') is False else 'Monitored'}</td>"
            "</tr>"
        )

    if not disk_rows:
        disk_rows.append('<tr><td colspan="4">No disk information available.</td></tr>')
    failed_units = diagnostics.get("failed_units") or []
    unit_rows = []
    for unit in failed_units:
        unit_rows.append(
            "<tr>"
            f"<td>{esc(unit.get('unit'))}</td>"
            f"<td>{esc(unit.get('active'))}</td>"
            f"<td>{esc(unit.get('description'))}</td>"
            "</tr>"
        )
    if not unit_rows:
        unit_rows.append('<tr><td colspan="3">No failed units reported.</td></tr>')
    # Network interfaces
    network_rows = []
    for interface in report.get("network", []):
        net_rates = (report.get("network_io") or {}).get("interfaces", {}).get(interface.get("name"), {})
        network_rows.append(
            "<tr>"
            f"<td>{esc(interface.get('name'))}</td>"
            f"<td>{esc(interface.get('state'))}</td>"
            f"<td>{esc(interface.get('mac_address'))}</td>"
            f"<td>{esc(', '.join(interface.get('ipv4', [])) or 'N/A')}</td>"
            f"<td>{esc(net_rates.get('rx_mbps', 0))} MB/s</td>"
            f"<td>{esc(net_rates.get('tx_mbps', 0))} MB/s</td>"
            "</tr>"
        )

    if not network_rows:
        network_rows.append(
            '<tr><td colspan="6">No network interfaces available.</td></tr>'
        )

    # Listening sockets
    socket_data = report.get("listening_sockets") or {}
    socket_rows = []

    for item in socket_data.get("items", []):
        socket_rows.append(
            "<tr>"
            f"<td>{esc(item.get('protocol'))}</td>"
            f"<td>{esc(item.get('state'))}</td>"
            f"<td>{esc(item.get('local_address'))}</td>"
            f"<td>{esc(item.get('process') or 'Unknown')}</td>"
            "</tr>"
        )

    if not socket_rows:
        socket_rows.append(
            '<tr><td colspan="4">No listening sockets available.</td></tr>'
        )

    connection_rows = []
    connection_data = report.get("network_connections") or {}
    for item in connection_data.get("items", []):
        connection_rows.append(
            "<tr>"
            f"<td>{esc(item.get('state'))}</td>"
            f"<td>{esc(item.get('local_address'))}</td>"
            f"<td>{esc(item.get('peer_address'))}</td>"
            f"<td>{esc(item.get('recv_q'))}</td>"
            f"<td>{esc(item.get('send_q'))}</td>"
            "</tr>"
        )
    if not connection_rows:
        connection_rows.append('<tr><td colspan="5">No established TCP connections.</td></tr>')

    # Services
    service_rows = []
    for name, service in (report.get("services") or {}).items():
        service_status = (
            service.get("status", "UNKNOWN")
            if isinstance(service, dict)
            else service
        )

        service_rows.append(
            "<tr>"
            f"<td>{esc(name)}</td>"
            f"<td>{esc(service_status)}</td>"
            "</tr>"
        )

    if not service_rows:
        service_rows.append(
            '<tr><td colspan="2">No service information available.</td></tr>'
        )

    # Top processes
    process_rows = []
    for process in report.get("top_processes", []):
        process_rows.append(
            "<tr>"
            f"<td>{esc(process.get('pid'))}</td>"
            f"<td>{esc(process.get('user', 'N/A'))}</td>"
            f"<td>{esc(process.get('command', process.get('name')))}</td>"
            f"<td>{esc(process.get('cpu_percent'))}%</td>"
            f"<td>{esc(process.get('memory_percent'))}%</td>"
            "</tr>"
        )

    if not process_rows:
        process_rows.append(
            '<tr><td colspan="5">No process information available.</td></tr>'
        )
    def log_list(section):
        info = diagnostics.get(section) or {}
        messages = info.get("messages") or []
        if info.get("status") != "ok":
            return "<li>Log information unavailable.</li>"
        if not messages:
            return "<li>No messages reported.</li>"
        return "".join(f"<li>{esc(message)}</li>" for message in messages)

    history_rows = []
    valid_history = [
        item for item in (history or [])
        if isinstance(item, dict)
    ]
    for item in valid_history[-10:][::-1]:
        timestamp = item.get("timestamp_utc", "unknown")
        status = item.get("health", {}).get("status", "UNKNOWN")
        cpu = item.get("cpu", {}).get("usage_percent")
        memory = item.get("memory", {}).get("usage_percent")

        cpu_text = f"{cpu:.1f}%" if isinstance(cpu, (int, float)) else "N/A"
        memory_text = (
            f"{memory:.1f}%" if isinstance(memory, (int, float)) else "N/A"
        )

        history_rows.append(
            "<tr>"
            f"<td>{esc(timestamp)}</td>"
            f'<td><span class="badge {status_class(status)}">{esc(status)}</span></td>'
            f"<td>{esc(cpu_text)}</td>"
            f"<td>{esc(memory_text)}</td>"
            "</tr>"
        )

    if not history_rows:
        history_rows.append(
            '<tr><td colspan="4">No health history available.</td></tr>'
        )


    raw_json = esc(json.dumps(report, indent=2, ensure_ascii=False))

    disk_values = [
        item.get("usage_percent")
        for item in disks
        if item.get("monitored") is not False
        and not (item.get("type") == "9p" and str(item.get("mountpoint", "")).startswith("/mnt/"))
        and isinstance(item.get("usage_percent"), (int, float))
    ]
    disk_max = max(disk_values) if disk_values else None
    memory = report.get("memory") or {}
    cpu = report.get("cpu") or {}
    network_count = len(report.get("network") or [])
    uptime = system.get("uptime_seconds")
    uptime_text = (
        f"{uptime // 86400}d {(uptime % 86400) // 3600}h"
        if isinstance(uptime, (int, float)) else "N/A"
    )
    os_name = (system.get("os") or {}).get("distribution", "Unknown")
    kernel = (system.get("os") or {}).get("kernel", "Unknown")
    load = system.get("load_average") or []
    load_text = " · ".join(f"{value:.2f}" for value in load[:3]) or "N/A"

    template = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#0b1220">
<title>Linux Server Health Monitor</title>
<style>
:root {
  color-scheme: dark;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  background: #0b1220; color: #e6edf7;
  --bg:#0b1220; --panel:#111c2d; --panel2:#162338; --line:#263750;
  --muted:#91a2bb; --text:#e6edf7; --blue:#4f9cff; --cyan:#39c6e8;
  --green:#35d39a; --yellow:#f4b84e; --red:#ff647c; --purple:#a78bfa;
}
* {box-sizing:border-box}
html {scroll-behavior:smooth}
body {margin:0;background:var(--bg);color:var(--text)}
button,select {font:inherit}
a {color:inherit;text-decoration:none}
.app {min-height:100vh}
.sidebar {
 position:fixed;inset:0 auto 0 0;width:238px;padding:25px 16px;
 background:#0e1929;border-right:1px solid #223149;display:flex;flex-direction:column;z-index:20
}
.brand {display:flex;align-items:center;gap:11px;padding:2px 9px 28px}
.brand-mark {width:38px;height:38px;border-radius:12px;display:grid;place-items:center;
 background:linear-gradient(135deg,#2878e8,#35c8d9);font-weight:900;color:white;font-size:19px}
.brand-name {font-size:14px;font-weight:800;letter-spacing:.02em}
.brand-sub {font-size:10px;color:var(--muted);margin-top:3px;letter-spacing:.12em}
.nav-label {font-size:10px;font-weight:800;letter-spacing:.15em;color:#687c98;padding:13px 11px 9px}
.nav {display:grid;gap:5px}
.nav a {display:flex;align-items:center;gap:12px;padding:11px 12px;border-radius:9px;
 color:#9eafc6;font-size:13px;transition:.15s}
.nav a:hover,.nav a.active {background:#192b43;color:#f4f8ff}
.nav a.active {box-shadow:inset 3px 0 var(--blue)}
.nav-icon {width:18px;text-align:center;color:#7faef2;font-size:15px}
.sidebar-footer {margin-top:auto;padding:15px 10px;border-top:1px solid #223149;color:#71849e;font-size:11px;line-height:1.7}
.main {margin-left:238px;padding:27px 32px 45px;max-width:1800px}
.topbar {display:flex;align-items:center;justify-content:space-between;gap:18px;margin-bottom:23px}
.eyebrow {font-size:10px;color:#7eaef0;letter-spacing:.16em;font-weight:800}
h1 {font-size:25px;letter-spacing:-.04em;margin:6px 0 0}
.top-actions {display:flex;align-items:center;gap:10px}
.live-pill {display:inline-flex;align-items:center;gap:8px;border:1px solid #245d4d;background:#102c28;
 color:#5ee0ae;border-radius:30px;padding:8px 12px;font-size:11px;font-weight:800}
.live-dot {width:7px;height:7px;border-radius:50%;background:#35d39a;box-shadow:0 0 10px #35d39a}
.control {background:#142239;color:#dce8f8;border:1px solid #2b405d;border-radius:8px;padding:9px 12px;font-size:11px}
.panel {background:var(--panel);border:1px solid var(--line);border-radius:13px;padding:19px;min-width:0}
.server-overview {display:flex;justify-content:space-between;align-items:center;gap:20px;margin-bottom:17px;
 background:linear-gradient(110deg,#142640,#111c2d 72%);border-color:#2b4566}
.server-id {display:flex;align-items:center;gap:14px}
.server-icon {width:46px;height:46px;display:grid;place-items:center;border-radius:12px;background:#1d3858;color:#72b5ff;font-size:21px}
.server-id h2 {font-size:16px;margin:0 0 5px}
.server-meta {font-size:11px;color:var(--muted);line-height:1.7}
.health-state {text-align:right}
.health-state .state {font-size:19px;font-weight:850;letter-spacing:.02em}
.health-state small {display:block;color:var(--muted);font-size:10px;margin-top:5px}
.state.good {color:var(--green)} .state.warning {color:var(--yellow)} .state.critical {color:var(--red)} .state.neutral {color:#9fb0c7}
.kpi-grid {display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin-bottom:17px}
.kpi {position:relative;overflow:hidden;padding:17px 18px}
.kpi-top {display:flex;align-items:center;justify-content:space-between;color:#a6b6cc;font-size:11px;font-weight:650}
.kpi-icon {width:30px;height:30px;display:grid;place-items:center;border-radius:9px;background:#1b3452;color:#78b6ff;font-size:14px}
.kpi-value {font-size:27px;font-weight:800;letter-spacing:-.05em;margin:13px 0 5px;font-variant-numeric:tabular-nums}
.kpi-foot {font-size:10px;color:var(--muted)}
.kpi-bar {height:4px;background:#263750;border-radius:9px;margin-top:13px;overflow:hidden}
.kpi-bar span {display:block;height:100%;border-radius:9px;background:var(--blue)}
.kpi:nth-child(2) .kpi-bar span {background:var(--purple)}
.kpi:nth-child(3) .kpi-bar span {background:var(--yellow)}
.kpi:nth-child(4) .kpi-bar span {background:var(--cyan)}
.section-head {display:flex;align-items:center;justify-content:space-between;gap:15px;margin-bottom:15px}
.section-head h2 {font-size:14px;margin:0;font-weight:750}
.section-note {font-size:10px;color:var(--muted)}
.chart-grid {display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}
.chart-card {padding:17px}
.chart-card h3 {font-size:12px;margin:0 0 15px;color:#d8e4f4}
.chart-card canvas {display:block;width:100%;height:170px}
.summary-grid {display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px;margin-top:14px}
.summary-card {padding:13px}
.summary-card span {font-size:10px;color:var(--muted)}
.summary-card strong {display:block;font-size:17px;margin-top:8px;font-variant-numeric:tabular-nums}
.summary-card small {display:block;font-size:9px;color:#71849e;margin-top:4px}
.two-col {display:grid;grid-template-columns:1.1fr .9fr;gap:14px;margin-top:17px}
.table-wrap {overflow-x:auto}
table {width:100%;border-collapse:collapse;text-align:left;white-space:nowrap}
th {font-size:9px;letter-spacing:.09em;text-transform:uppercase;color:#8ea4c2;font-weight:800;background:#16243a}
th,td {padding:11px 12px;border-bottom:1px solid #223149;font-size:11px}
td {color:#d0dbea}
tbody tr:hover {background:#17273d}
tbody tr:last-child td {border-bottom:0}
.badge {display:inline-flex;align-items:center;border-radius:20px;padding:4px 8px;font-size:9px;font-weight:800;text-transform:uppercase}
.badge.good {background:#12372f;color:#5fe0ad}.badge.warning {background:#3b301b;color:#ffd078}
.badge.critical {background:#40202b;color:#ff8295}.badge.neutral {background:#243249;color:#b7c5d9}
.lower-grid {display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin-top:17px}
.compact-section {margin-top:14px}
details summary {cursor:pointer;color:#b9cbe3;font-size:12px;font-weight:750}
details pre {max-height:480px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;background:#0a1220;border:1px solid #253650;border-radius:8px;padding:14px;color:#a9bdd8;font-size:10px;line-height:1.6}
.log-list {padding-left:17px;margin:0;color:#b8c8dc;font-size:11px;line-height:1.7}
.muted {color:var(--muted)}
#resource-health-details .grid {display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px}
.metric.card {background:#142239;border:1px solid #293d58;border-radius:10px;padding:14px}
.metric-name {font-size:10px;color:#a8bad2}.metric-value {font-size:20px;font-weight:800;margin:9px 0}
.progress-track {height:5px;background:#293b54;border-radius:8px;overflow:hidden;margin:9px 0}
.progress-fill {height:100%;border-radius:8px}.progress-fill.good{background:var(--green)}.progress-fill.warning{background:var(--yellow)}.progress-fill.critical{background:var(--red)}.progress-fill.neutral{background:var(--blue)}
.metric p {font-size:9px;color:var(--muted)}.metric .badge{margin-top:4px}
footer {padding:22px 0;text-align:center;color:#647894;font-size:10px}
@media(max-width:1200px){.main{padding:22px}.kpi-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.chart-grid{grid-template-columns:1fr 1fr}.summary-grid{grid-template-columns:repeat(3,1fr)}}
.network-chart-grid{grid-template-columns:minmax(0,1.6fr) minmax(260px,1fr);margin:12px 0 16px}.network-stats-card{min-width:0}.network-stat-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin-top:16px}.network-stat-grid div{display:flex;flex-direction:column;gap:7px;padding:12px;border:1px solid #263b56;border-radius:10px;background:#101d30}.network-stat-grid small{font-size:9px;color:#8fb9e9;letter-spacing:.04em}.network-stat-grid strong{font-size:16px;color:#f4f8ff}@media(max-width:850px){.network-chart-grid{grid-template-columns:1fr}}\n@media(max-width:850px){.sidebar{width:66px;padding:18px 9px}.brand{padding:0 4px 25px}.brand-name,.brand-sub,.nav-label,.nav a span:last-child,.sidebar-footer{display:none}.brand-mark{width:42px}.nav a{justify-content:center;padding:12px 5px}.nav-icon{font-size:17px}.main{margin-left:66px;padding:20px 16px}.two-col,.lower-grid{grid-template-columns:1fr}}
@media(max-width:600px){.main{padding:16px 11px}.topbar{align-items:flex-start}.top-actions{flex-wrap:wrap;justify-content:flex-end}.topbar h1{font-size:20px}.server-overview{align-items:flex-start}.health-state .state{font-size:15px}.kpi-grid{gap:9px}.kpi{padding:13px}.kpi-value{font-size:22px}.chart-grid{grid-template-columns:1fr}.summary-grid{grid-template-columns:repeat(2,1fr)}.server-meta{font-size:10px}}


.filter-row{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 12px}.filter-input{min-width:220px;flex:1}.extended-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-top:14px}.extended-card{padding:14px 16px}.extended-card small{display:block;color:#89a6ca;font-size:9px;letter-spacing:.6px}.extended-card strong{display:block;font-size:19px;margin:8px 0 4px}.extended-card span{color:#849bb8;font-size:10px}.settings-form{display:grid;gap:14px}.service-settings{display:flex;gap:18px;flex-wrap:wrap;border:1px solid #263750;border-radius:10px;padding:12px}.service-settings legend{color:#89a6ca;font-size:9px;letter-spacing:.6px}.service-settings label{display:flex;align-items:center;gap:7px;color:#dce8f8;font-size:11px}.settings-form label{display:grid;gap:7px}.settings-form input{width:100%}.exclude-label textarea{width:100%;resize:vertical}.settings-actions{display:flex;align-items:center;gap:12px}.primary-control{background:#1d4c7c;border-color:#397fc2;color:#fff;cursor:pointer}.control{cursor:pointer}.history-time{font-variant-numeric:tabular-nums}.command-cell{max-width:260px;overflow:hidden;text-overflow:ellipsis}.table-wrap{scrollbar-width:thin}@media(max-width:900px){.extended-grid{grid-template-columns:1fr 1fr}}@media(max-width:600px){.extended-grid{grid-template-columns:1fr}}

/* Reference-dashboard refinements */
.nav-label{font-size:10px;letter-spacing:1.5px;color:#7086a6;font-weight:800;padding:12px 10px 8px}
.nav-icon svg,.kpi-icon svg{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.nav-icon{display:grid;place-items:center;width:20px;height:20px;color:#66aaff;flex:none}
.nav a{gap:13px}.kpi-icon{display:grid;place-items:center}.kpi-foot{line-height:1.6}.kpi-foot span{color:#829bbd;font-size:10px}
.quick-stats{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin:14px 0}
.quick-stat{display:flex;align-items:center;gap:12px;padding:13px 14px;min-height:70px}
.quick-stat small,.settings-grid small{display:block;color:#89a6ca;font-size:9px;letter-spacing:.5px;margin-bottom:6px}
.quick-stat strong{font-size:15px;color:#f2f6ff}.quick-icon{width:32px;height:32px;border-radius:10px;display:grid;place-items:center;flex:none;background:#172a43;font-size:20px}
.quick-icon svg{width:19px;height:19px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}.brand-mark svg{width:22px;height:22px;fill:none;stroke:#fff;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}.quick-icon.green{color:#39df91}.quick-icon.cyan{color:#2fc7f4}.quick-icon.purple{color:#a78bfa}.quick-icon.red{color:#ff596b}.quick-icon.blue{color:#36b8ff}
.settings-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.settings-grid>div{background:#101d30;border:1px solid #243750;border-radius:10px;padding:14px}.settings-grid strong{font-size:13px}.settings-grid p{font-size:10px;color:#849bb8;margin:7px 0 0}
@media(max-width:1100px){.quick-stats{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:650px){.quick-stats,.settings-grid{grid-template-columns:1fr}}

/* ServerPulse multi-page navigation */
body[data-page] .main > .server-overview,
body[data-page] .main > .kpi-grid,
body[data-page] .main > .two-col,
body[data-page] .main > .quick-stats,
body[data-page] .main > .extended-grid,
body[data-page] .main > .lower-grid,
body[data-page] .main > #performance,
body[data-page] .main > #metrics-detail,
body[data-page] .main > #failed-units,
body[data-page] .main > #history,
body[data-page] .main > #logs,
body[data-page] .main > #resource-health-details,
body[data-page] .main > #settings,
body[data-page] .main > #json-report,
body[data-page] .main > #security {display:none!important}
body[data-page="dashboard"] .main > .server-overview{display:flex!important}
body[data-page="dashboard"] .main > .kpi-grid,
body[data-page="dashboard"] .main > .quick-stats,
body[data-page="dashboard"] .main > .extended-grid{display:grid!important}
body[data-page="dashboard"] .main > #performance{display:block!important}
body[data-page="dashboard"] .main > .two-col{display:grid!important}
body[data-page="metrics"] .main > #performance,
body[data-page="metrics"] .main > #metrics-detail,
body[data-page="metrics"] .main > #resource-health-details{display:block!important}
body[data-page="metrics"] .main > .extended-grid{display:grid!important}
body[data-page="alerts"] .main > .two-col,
body[data-page="processes"] .main > .two-col{display:grid!important;grid-template-columns:minmax(0,1fr)!important}
body[data-page="alerts"] #alerts, body[data-page="processes"] #processes{display:block!important}
body[data-page="alerts"] #processes, body[data-page="processes"] #alerts{display:none!important}
body[data-page="disks"] .main > .lower-grid,
body[data-page="network"] .main > .lower-grid{display:grid!important;grid-template-columns:minmax(0,1fr)!important}
body[data-page="disks"] #disks, body[data-page="network"] #network{display:block!important}
body[data-page="disks"] #network, body[data-page="disks"] #services, body[data-page="disks"] #sockets, body[data-page="disks"] #connections,
body[data-page="network"] #disks, body[data-page="network"] #services, body[data-page="network"] #sockets{display:none!important}
body[data-page="services"] .main > .lower-grid{display:grid!important;grid-template-columns:repeat(2,minmax(0,1fr))!important}
body[data-page="services"] #services, body[data-page="services"] #sockets, body[data-page="services"] #failed-units{display:block!important}
body[data-page="services"] #disks, body[data-page="services"] #network, body[data-page="services"] #connections{display:none!important}
body[data-page="logs"] .main > #logs,
body[data-page="security"] .main > #security,
body[data-page="history"] .main > #history,
body[data-page="settings"] .main > #settings,
body[data-page="settings"] .main > #json-report{display:block!important}
body[data-page="settings"] .main > #resource-health-details{display:block!important}
body[data-page] .main > footer{display:block}
.nav a.active{background:#1a2d46;border-left:3px solid #4a9cff;color:#fff}
#metrics-detail .metrics-detail-grid{grid-template-columns:repeat(4,minmax(0,1fr))}
@media(max-width:850px){#metrics-detail .metrics-detail-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:600px){#metrics-detail .metrics-detail-grid{grid-template-columns:1fr}body[data-page="services"] .main > .lower-grid{grid-template-columns:1fr!important}}
</style>
</head>
<body>
<div class="app">
<aside class="sidebar">
  <div class="brand"><div class="brand-mark"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12h4l3-6 4 12 3-6h4"/></svg></div><div><div class="brand-name">SERVERPULSE</div><div class="brand-sub">LINUX MONITOR</div></div></div>
  <nav class="nav" aria-label="Main navigation">
    <div class="nav-label">MONITORING</div>
    <a class="active" href="#dashboard" data-page="dashboard"><span class="nav-icon"><svg viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg></span>Dashboard</a>
    <a href="#metrics" data-page="metrics"><span class="nav-icon"><svg viewBox="0 0 24 24"><path d="M3 12h4l3-8 4 16 3-8h4"/></svg></span>Metrics</a>
    <a href="#alerts" data-page="alerts"><span class="nav-icon"><svg viewBox="0 0 24 24"><path d="M18 9a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"/></svg></span>Alerts</a>
    <a href="#disks" data-page="disks"><span class="nav-icon"><svg viewBox="0 0 24 24"><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/></svg></span>Disks</a>
    <a href="#network" data-page="network"><span class="nav-icon"><svg viewBox="0 0 24 24"><circle cx="12" cy="5" r="3"/><circle cx="5" cy="18" r="3"/><circle cx="19" cy="18" r="3"/><path d="m10.5 7.5-4 7.5m7-7.5 4 7.5M8 18h8"/></svg></span>Network</a>
    <a href="#services" data-page="services"><span class="nav-icon"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M12 2v3m0 14v3M2 12h3m14 0h3M5 5l2 2m10 10 2 2m0-14-2 2M7 17l-2 2"/></svg></span>Services</a>
    <a href="#processes" data-page="processes"><span class="nav-icon"><svg viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="16" rx="2"/><path d="M8 8h8v8H8zM9 1v3m6-3v3M9 20v3m6-3v3"/></svg></span>Processes</a>
    <a href="#logs" data-page="logs"><span class="nav-icon"><svg viewBox="0 0 24 24"><path d="M5 5h14M5 12h14M5 19h14"/></svg></span>Logs</a>
    <a href="#security" data-page="security"><span class="nav-icon"><svg viewBox="0 0 24 24"><path d="M12 3 20 6v5c0 5-3.4 8.2-8 10-4.6-1.8-8-5-8-10V6l8-3Z"/><path d="m9 12 2 2 4-4"/></svg></span>Security</a>
    <a href="#history" data-page="history"><span class="nav-icon"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg></span>History</a>
    <a href="#settings" data-page="settings"><span class="nav-icon"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v3m0 14v3M2 12h3m14 0h3"/></svg></span>Settings</a>
  </nav>
  <div class="sidebar-footer">LINUX SERVER HEALTH MONITOR<br>Version __VERSION__<br>Local monitoring instance</div>
</aside>
<main class="main" id="dashboard">
  <div class="topbar">
    <div><div class="eyebrow">INFRASTRUCTURE / OVERVIEW</div><h1 id="page-title">Server Health Dashboard</h1></div>
    <div class="top-actions"><span class="live-pill" id="live-monitor-status"><span class="live-dot"></span> LIVE · CONNECTING</span><select class="control" id="chart-range"><option value="5">5 minutes</option><option value="15">15 minutes</option><option value="60" selected>1 hour</option><option value="360">6 hours</option><option value="1440">24 hours</option><option value="10080">7 days</option></select></div>
  </div>

  <section class="panel server-overview">
    <div class="server-id"><div class="server-icon">▣</div><div><h2 id="server-hostname">__HOSTNAME__</h2><div class="server-meta" id="server-meta">__OS__ · Kernel __KERNEL__<br>Uptime: __UPTIME__ · Load: __LOAD__</div></div></div>
    <div class="health-state"><div class="state __HEALTH_CLASS__" id="overall-health">__HEALTH__</div><small>OVERALL SYSTEM HEALTH</small><div class="muted" style="font-size:10px;margin-top:7px"><!-- Systemd status: degraded -->Systemd status: <span id="systemd-status">__SYSTEMD__</span></div></div>
  </section>

  <section class="kpi-grid" aria-label="Key metrics">
    <article class="panel kpi"><div class="kpi-top"><span>CPU UTILIZATION</span><span class="kpi-icon"><svg viewBox="0 0 24 24"><path d="M3 12h4l3-8 4 16 3-8h4"/></svg></span></div><div class="kpi-value" id="kpi-cpu">__CPU__</div><div class="kpi-foot">Processor usage · <span id="cpu-thresholds">Warning ≥ 80% · Critical ≥ 95%</span></div><div class="kpi-bar"><span id="bar-cpu" style="width:__CPU_BAR__%"></span></div></article>
    <article class="panel kpi"><div class="kpi-top"><span>MEMORY USAGE</span><span class="kpi-icon"><svg viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="16" rx="2"/><path d="M8 8h8v8H8z"/></svg></span></div><div class="kpi-value" id="kpi-memory">__MEMORY__</div><div class="kpi-foot">RAM consumption · <span id="memory-thresholds">Warning ≥ 80% · Critical ≥ 95%</span></div><div class="kpi-bar"><span id="bar-memory" style="width:__MEMORY_BAR__%"></span></div></article>
    <article class="panel kpi"><div class="kpi-top"><span>DISK UTILIZATION</span><span class="kpi-icon"><svg viewBox="0 0 24 24"><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/></svg></span></div><div class="kpi-value" id="kpi-disk">__DISK__</div><div class="kpi-foot">Highest mounted filesystem · <span id="disk-thresholds">Warning ≥ 80% · Critical ≥ 95%</span></div><div class="kpi-bar"><span id="bar-disk" style="width:__DISK_BAR__%"></span></div></article>
    <article class="panel kpi"><div class="kpi-top"><span>NETWORK THROUGHPUT</span><span class="kpi-icon"><svg viewBox="0 0 24 24"><circle cx="12" cy="5" r="3"/><circle cx="5" cy="18" r="3"/><circle cx="19" cy="18" r="3"/><path d="m10.5 7.5-4 7.5m7-7.5 4 7.5M8 18h8"/></svg></span></div><div class="kpi-value" id="kpi-network">__NETWORK_COUNT__</div><div class="kpi-foot" id="network-kpi-foot">↓ Download · ↑ Upload</div><div class="kpi-bar"><span id="bar-network" style="width:__NETWORK_BAR__%"></span></div></article>
  </section>


  <section class="panel" id="performance">
    <div class="section-head"><h2>Performance Analytics</h2><span class="section-note">Historical telemetry · configurable auto refresh</span></div>
    <div class="chart-grid">
      <article class="panel chart-card"><h3>CPU Usage</h3><canvas id="cpu-chart" height="170"></canvas></article>
      <article class="panel chart-card"><h3>Memory Usage</h3><canvas id="memory-chart" height="170"></canvas></article>
      <article class="panel chart-card"><h3>Disk I/O Throughput</h3><canvas id="disk-io-chart" height="170"></canvas></article>
    </div>
    <div class="summary-grid">
      <div class="panel summary-card"><span>AVERAGE CPU</span><strong id="summary-avg-cpu">--</strong></div>
      <div class="panel summary-card"><span>PEAK CPU</span><strong id="summary-peak-cpu">--</strong><small id="summary-peak-cpu-time">No data</small></div>
      <div class="panel summary-card"><span>AVERAGE MEMORY</span><strong id="summary-avg-memory">--</strong></div>
      <div class="panel summary-card"><span>PEAK MEMORY</span><strong id="summary-peak-memory">--</strong><small id="summary-peak-memory-time">No data</small></div>
      <div class="panel summary-card"><span>MAX DISK READ</span><strong id="summary-max-read">--</strong><small id="summary-max-read-time">No data</small></div>
      <div class="panel summary-card"><span>MAX DISK WRITE</span><strong id="summary-max-write">--</strong><small id="summary-max-write-time">No data</small></div>
      <div class="panel summary-card"><span>ANOMALIES DETECTED</span><strong id="summary-anomalies">0</strong><small id="summary-anomaly-detail">No statistical outliers in this period</small></div>
      <div class="panel summary-card"><span>HISTORICAL SAMPLES</span><strong id="summary-samples">0</strong><small>Samples in selected period</small></div>
    </div>
  </section>

  <section class="panel compact-section" id="metrics-detail">
    <div class="section-head"><h2>Resource Metrics Detail</h2><span class="section-note">Live values from the latest collection cycle</span></div>
    <div class="extended-grid metrics-detail-grid">
      <article class="panel extended-card"><small>CPU IOWAIT</small><strong id="metrics-iowait">N/A</strong><span>CPU time waiting for storage I/O</span></article>
      <article class="panel extended-card"><small>RAM USED</small><strong id="metrics-ram-used">N/A</strong><span id="metrics-ram-total">Total memory: N/A</span></article>
      <article class="panel extended-card"><small>SWAP USED</small><strong id="metrics-swap-used">N/A</strong><span id="metrics-swap-total">Total swap: N/A</span></article>
      <article class="panel extended-card"><small>ACTIVE CONNECTIONS</small><strong id="metrics-connections">N/A</strong><span>Current network connections</span></article>
    </div>
    <div class="section-head" style="margin-top:18px"><h2>CPU Utilization by Logical Core</h2><span class="section-note">Per-core sampled utilization</span></div>
    <div class="table-wrap"><table><thead><tr><th>Logical Core</th><th>CPU Usage</th></tr></thead><tbody id="cpu-core-table-body"><tr><td colspan="2">Waiting for metrics…</td></tr></tbody></table></div>
    <div class="section-head" style="margin-top:18px"><h2>Disk I/O by Device</h2><span class="section-note">Read/write throughput and operation counters</span></div>
    <div class="table-wrap"><table><thead><tr><th>Device</th><th>Read</th><th>Write</th><th>Read Ops/s</th><th>Write Ops/s</th></tr></thead><tbody id="disk-io-device-body"><tr><td colspan="5">Waiting for metrics…</td></tr></tbody></table></div>
  </section>

  <div class="two-col">
    <section class="panel" id="alerts"><div class="section-head"><h2>Recent Alerts</h2><span class="section-note" id="alerts-summary">Loading events...</span></div><div class="filter-row"><input class="control filter-input" id="alert-search" type="search" placeholder="Search resource or message"><select class="control" id="alert-severity"><option value="ALL">All severities</option><option value="CRITICAL">Critical</option><option value="WARNING">Warning</option><option value="RECOVERED">Recovered</option></select><button class="control" id="export-alerts">Export CSV</button></div><div class="table-wrap"><table><thead><tr><th>Time</th><th>Severity</th><th>Resource</th><th>Value</th><th>Threshold</th><th>Event</th></tr></thead><tbody id="alerts-table-body"><tr><td colspan="6">No alerts yet.</td></tr></tbody></table></div></section>
    <section class="panel" id="processes"><div class="section-head"><h2>Top Processes</h2><span class="section-note">Sorted by CPU usage</span></div><input class="control filter-input" id="process-search" type="search" placeholder="Filter by PID, user or command" aria-label="Filter processes"><div class="table-wrap"><table><thead><tr><th>PID</th><th>User</th><th>Command</th><th>CPU</th><th>Memory</th></tr></thead><tbody id="process-table-body">__PROCESS_ROWS__</tbody></table></div></section>
  </div>

  <section class="quick-stats" aria-label="System status summary">
    <article class="panel quick-stat"><span class="quick-icon green"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3a9 9 0 1 0 9 9"/><path d="M12 7v5l4 2M16 3h5v5"/></svg></span><div><small>LOAD AVERAGE</small><strong id="quick-load">__LOAD__</strong></div></article>
    <article class="panel quick-stat"><span class="quick-icon cyan"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="5" r="3"/><circle cx="5" cy="18" r="3"/><circle cx="19" cy="18" r="3"/><path d="m10.5 7.5-4 7.5m7-7.5 4 7.5M8 18h8"/></svg></span><div><small>ACTIVE CONNECTIONS</small><strong id="quick-connections">__CONNECTIONS__</strong></div></article>
    <article class="panel quick-stat"><span class="quick-icon purple"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h16"/></svg></span><div><small>LISTENING SOCKETS</small><strong id="quick-sockets">__SOCKETS__</strong></div></article>
    <article class="panel quick-stat"><span class="quick-icon red"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="m9 9 6 6m0-6-6 6"/></svg></span><div><small>FAILED UNITS</small><strong id="quick-failed">__FAILED__</strong></div></article>
    <article class="panel quick-stat"><span class="quick-icon blue"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M7 3v4m10-4v4M3 10h18"/></svg></span><div><small>REPORT GENERATED</small><strong id="quick-generated">__GENERATED__</strong></div></article>
  </section>

  <section class="extended-grid" aria-label="Extended telemetry">
    <article class="panel extended-card"><small>CPU IOWAIT</small><strong id="extended-iowait">N/A</strong><span>Time waiting for storage I/O</span></article>
    <article class="panel extended-card"><small>SWAP USAGE</small><strong id="extended-swap">N/A</strong><span id="extended-swap-detail">Swap consumption</span></article>
    <article class="panel extended-card"><small>ROOT FREE SPACE</small><strong id="extended-root-free">N/A</strong><span>Available on / filesystem</span></article>
  </section>

  <div class="lower-grid">
    <section class="panel" id="disks"><div class="section-head"><h2>Disk Usage</h2></div><div class="table-wrap"><table><thead><tr><th>Mount point</th><th>Used</th><th>Filesystem</th><th>Monitoring</th></tr></thead><tbody id="disk-table-body">__DISK_ROWS__</tbody></table></div></section>
    <section class="panel" id="network">
      <div class="section-head"><h2>Network Monitoring</h2><span class="section-note">Live throughput · cumulative interface counters</span></div>
      <div class="chart-grid network-chart-grid">
        <article class="panel chart-card"><h3>Network Traffic · RX / TX (MB/s)</h3><canvas id="network-io-chart" height="170"></canvas></article>
        <article class="panel chart-card network-stats-card">
          <h3>Traffic Statistics</h3>
          <div class="network-stat-grid">
            <div><small>TOTAL RECEIVED</small><strong id="network-total-rx">--</strong></div>
            <div><small>TOTAL SENT</small><strong id="network-total-tx">--</strong></div>
            <div><small>PEAK DOWNLOAD</small><strong id="network-peak-rx">--</strong></div>
            <div><small>PEAK UPLOAD</small><strong id="network-peak-tx">--</strong></div>
          </div>
        </article>
      </div>
      <div class="table-wrap"><table><thead><tr><th>Interface</th><th>State</th><th>MAC</th><th>IPv4</th><th>RX Rate</th><th>TX Rate</th><th>Total RX</th><th>Total TX</th><th>RX Errors</th><th>TX Errors</th><th>Dropped</th></tr></thead><tbody id="network-table-body">__NETWORK_ROWS__</tbody></table></div>
    </section>
    <section class="panel" id="services"><div class="section-head"><h2>Services Status</h2></div><div class="table-wrap"><table><thead><tr><th>Service</th><th>Status</th></tr></thead><tbody id="service-table-body">__SERVICE_ROWS__</tbody></table></div></section>
    <section class="panel" id="sockets"><div class="section-head"><h2>Listening Sockets</h2><span class="section-note">TCP / UDP</span></div><div class="table-wrap"><table><thead><tr><th>Protocol</th><th>State</th><th>Local Address</th><th>Process</th></tr></thead><tbody id="socket-table-body">__SOCKET_ROWS__</tbody></table></div></section>
    <section class="panel" id="connections"><div class="section-head"><h2>Established TCP Connections</h2><span class="section-note">Active remote endpoints</span></div><div class="table-wrap"><table><thead><tr><th>State</th><th>Local Address</th><th>Peer Address</th><th>Recv-Q</th><th>Send-Q</th></tr></thead><tbody id="connection-table-body">__CONNECTION_ROWS__</tbody></table></div></section>
  </div>

  <section class="panel compact-section" id="failed-units"><div class="section-head"><h2>Failed systemd Units</h2><span class="section-note">System diagnostics</span></div><div class="table-wrap"><table><thead><tr><th>Unit</th><th>State</th><th>Description</th></tr></thead><tbody id="failed-units-table-body">__UNIT_ROWS__</tbody></table></div></section>
  <section class="panel compact-section" id="history"><div class="section-head"><h2>Recent Health History</h2><span class="section-note">Latest health reports</span></div><div class="table-wrap"><table><thead><tr><th>Timestamp (Local)</th><th>Status</th><th>CPU</th><th>RAM</th></tr></thead><tbody id="history-table-body">__HISTORY_ROWS__</tbody></table></div><div class="section-head" style="margin-top:12px"><span class="section-note" id="history-summary">Latest reports</span><button class="control" id="export-history">Export CSV</button></div></section>
  <section class="panel compact-section" id="logs"><div class="section-head"><h2>System Logs</h2></div><div class="lower-grid" style="margin-top:0"><div><h3 style="font-size:11px;color:#ff9aaa">Recent errors</h3><ul class="log-list">__ERRORS__</ul></div><div><h3 style="font-size:11px;color:#ffd078">Recent warnings</h3><ul class="log-list">__WARNINGS__</ul></div></div></section>
  <section class="panel compact-section" id="resource-health-details"><div class="section-head"><h2>Resource health: __HEALTH__</h2></div><details><summary>Resource health checks · thresholds and status</summary><div class="grid" style="margin-top:15px">__HEALTH_CARDS__</div></details></section>
  <section class="panel compact-section" id="security">
    <div class="section-head"><h2>Security Events</h2><span class="section-note" id="security-summary">Read-only host security telemetry</span></div>
    <div class="quick-stats">
      <article class="panel quick-stat"><div><small>FAIL2BAN</small><strong id="security-fail2ban">Checking…</strong></div></article>
      <article class="panel quick-stat"><div><small>SSH FAILED LOGINS</small><strong id="security-ssh-failed">0</strong></div></article>
      <article class="panel quick-stat"><div><small>SSH SUCCESSFUL LOGINS</small><strong id="security-ssh-success">0</strong></div></article>
      <article class="panel quick-stat"><div><small>SUDO COMMANDS</small><strong id="security-sudo">0</strong></div></article>
      <article class="panel quick-stat"><div><small>CRITICAL EVENTS</small><strong id="security-critical">0</strong></div></article>
    </div>
    <div class="filter-row"><select class="control" id="security-event-filter"><option value="">All event types</option><option value="ssh_login_failed">SSH failed</option><option value="ssh_login_success">SSH successful</option><option value="sudo_command">Sudo commands</option><option value="user_account_change">User changes</option><option value="firewall_change">Firewall changes</option><option value="fail2ban_ban">Fail2ban bans</option><option value="sensitive_file_modified">Sensitive file changes</option><option value="ssh_bruteforce_suspected">Brute-force detections</option></select><button class="control" id="security-refresh" type="button">Refresh events</button></div>
    <div class="table-wrap"><table><thead><tr><th>Time (UTC)</th><th>Severity</th><th>Event</th><th>User</th><th>Source IP</th><th>Details</th></tr></thead><tbody id="security-events-body"><tr><td colspan="6">Waiting for security events…</td></tr></tbody></table></div>
    <p class="section-note">Monitoring is read-only. Firewall rules, user accounts, SSH settings, and Fail2ban are never modified automatically.</p>
  </section>
  <section class="panel compact-section" id="settings"><div class="section-head"><h2>Dashboard Settings</h2><span class="section-note">Editable monitoring thresholds and exclusions</span></div><form id="settings-form" class="settings-form"><div class="settings-grid"><label><small>CPU WARNING (%)</small><input class="control" type="number" min="0" max="99" step="1" id="setting-cpu-warning" required></label><label><small>CPU CRITICAL (%)</small><input class="control" type="number" min="1" max="100" step="1" id="setting-cpu-critical" required></label><label><small>MEMORY WARNING (%)</small><input class="control" type="number" min="0" max="99" step="1" id="setting-memory-warning" required></label><label><small>MEMORY CRITICAL (%)</small><input class="control" type="number" min="1" max="100" step="1" id="setting-memory-critical" required></label><label><small>DISK WARNING (%)</small><input class="control" type="number" min="0" max="99" step="1" id="setting-disk-warning" required></label><label><small>DISK CRITICAL (%)</small><input class="control" type="number" min="1" max="100" step="1" id="setting-disk-critical" required></label></div><label><small>DASHBOARD REFRESH INTERVAL (SECONDS, 1–300)</small><input class="control" type="number" min="1" max="300" step="1" id="setting-refresh-interval" required></label><label class="exclude-label"><small>EXCLUDED MOUNT POINTS (ONE PER LINE)</small><textarea class="control" id="setting-excluded-mounts" rows="3" placeholder="/mnt/c&#10;/mnt/d"></textarea></label><fieldset class="service-settings"><legend>MONITORED SERVICES</legend><label><input type="checkbox" id="setting-service-ssh"> SSH</label><label><input type="checkbox" id="setting-service-sshd"> SSHD</label><label><input type="checkbox" id="setting-service-docker"> Docker</label><label><input type="checkbox" id="setting-service-nginx"> Nginx</label></fieldset><div class="settings-actions"><button class="control primary-control" type="submit">Save settings</button><span id="settings-status" class="section-note" role="status">Loading settings…</span></div></form></section>
  <section class="panel compact-section" id="json-report"><details><summary>View complete JSON report</summary><pre>__RAW_JSON__</pre></details></section>
  <footer>Linux Server Health Monitor · Version __VERSION__ · Generated __GENERATED__</footer>
</main>
</div>
</body>
</html>"""

    cpu_value = cpu.get("usage_percent")
    memory_value = memory.get("usage_percent")
    def percent_text(value):
        return f"{value:.1f}%" if isinstance(value, (int, float)) else "N/A"
    def bar_width(value):
        return f"{max(0, min(100, float(value))):.1f}" if isinstance(value, (int, float)) else "0"
    replacements = {
        "__VERSION__": esc(report.get("monitor_version", "unknown")),
        "__HOSTNAME__": esc(hostname),
        "__OS__": esc(os_name),
        "__KERNEL__": esc(kernel),
        "__UPTIME__": esc(uptime_text),
        "__LOAD__": esc(load_text),
        "__HEALTH__": esc(resource_status.upper()),
        "__HEALTH_CLASS__": status_class(resource_status),
        "__SYSTEMD__": esc(systemd_status),
        "__CPU__": percent_text(cpu_value),
        "__CPU_BAR__": bar_width(cpu_value),
        "__MEMORY__": percent_text(memory_value),
        "__MEMORY_BAR__": bar_width(memory_value),
        "__DISK__": percent_text(disk_max),
        "__DISK_BAR__": bar_width(disk_max),
        "__NETWORK_COUNT__": str(network_count),
        "__NETWORK_BAR__": str(min(100, network_count * 10)),
        "__CONNECTIONS__": esc(str(report.get("active_connections", "N/A"))),
        "__SOCKETS__": esc(str((report.get("listening_sockets") or {}).get("count", 0))),
        "__FAILED__": esc(str(len((report.get("system_diagnostics") or {}).get("failed_units") or []))),
        "__PROCESS_ROWS__": "".join(process_rows),
        "__DISK_ROWS__": "".join(disk_rows),
        "__NETWORK_ROWS__": "".join(network_rows),
        "__SERVICE_ROWS__": "".join(service_rows),
        "__SOCKET_ROWS__": "".join(socket_rows),
        "__CONNECTION_ROWS__": "".join(connection_rows),
        "__UNIT_ROWS__": "".join(unit_rows),
        "__HISTORY_ROWS__": "".join(history_rows),
        "__ERRORS__": log_list("errors"),
        "__WARNINGS__": log_list("warnings"),
        "__HEALTH_CARDS__": "".join(cards),
        "__RAW_JSON__": raw_json,
        "__GENERATED__": esc(generated),
    }
    for marker, value in replacements.items():
        template = template.replace(marker, value)
    return template



def watch_health(logger, interval):
    """Continuously collect and display health reports."""
    logger.info(
        "Continuous monitoring started (interval=%s seconds)",
        interval,
    )

    try:
        while True:
            try:
                report = collect_report(logger)
                save_report_history(report, logger)
                print("\n" + "=" * 60)
                print(f"Health check: {datetime.now(UTC).isoformat()}")
                print_report(report)
            except (ValueError, TypeError) as error:
                logger.error("Health check failed: %s", error)
                print(f"Health check failed: {error}", file=sys.stderr)
            except Exception:
                logger.exception("Unexpected error during health check")
                print(
                    "Health check failed. See the log for details.",
                    file=sys.stderr,
                )

            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nMonitoring stopped.")
        logger.info("Continuous monitoring stopped")


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
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Run continuous health monitoring",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=5,
        help="Seconds between checks in watch mode (default: 5)",
    )
    parser.add_argument(
        "--history",
        type=int,
        nargs="?",
        const=10,
        metavar="N",
        help="Show the latest N health checks (default: 10)",
    )

    args = parser.parse_args()
    if args.history is not None:
        if args.history < 1:
            parser.error("--history must be at least 1")
        show_history(args.history)
        return

    if args.interval < 1:
        parser.error("--interval must be at least 1 second")

    if args.watch and (args.output or args.html or args.json):
        parser.error(
            "--watch cannot be combined with --output, --html, or --json yet"
        )
    logger = setup_logging()
    logger.info("Linux Server Health Monitor v%s started", VERSION)

    if args.watch:
        watch_health(logger, args.interval)
        logger.info("Linux Server Health Monitor finished")
        return

    try:
        report = collect_report(logger)
        save_report_history(report, logger)
    except (ValueError, TypeError) as error:
        logger.error("Unable to generate health report: %s", error)
        parser.error(str(error))
    except Exception:
        logger.exception("Unexpected error while generating health report")
        raise

    if args.html:
        try:
            Path(args.html).write_text(
                generate_html_report(report, load_report_history(10)),
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
