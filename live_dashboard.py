import json
import logging
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import health_monitor as monitor

HOST = "127.0.0.1"
PORT = 8000
INTERVAL = 5

logger = monitor.setup_logging()
latest_report = None
report_lock = threading.Lock()

alert_lock = threading.Lock()
ALERTS_FILE = Path(__file__).with_name("alerts_history.jsonl")
MAX_ALERTS = 100
CONFIG_FILE = Path(__file__).with_name("config.json")


def read_dashboard_config():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def validate_dashboard_config(payload):
    if not isinstance(payload, dict):
        raise TypeError("Settings payload must be an object")
    current = read_dashboard_config()
    thresholds = current.setdefault("thresholds", {})
    for resource in ("cpu", "memory", "disk"):
        values = payload.get(resource)
        if values is None:
            continue
        if not isinstance(values, dict):
            raise TypeError(f"{resource} thresholds must be an object")
        warning, critical = values.get("warning"), values.get("critical")
        if (isinstance(warning, bool) or isinstance(critical, bool)
                or not isinstance(warning, (int, float))
                or not isinstance(critical, (int, float))
                or not 0 <= warning < critical <= 100):
            raise ValueError(f"Invalid {resource} thresholds: warning must be below critical (0-100)")
        thresholds[resource] = {"warning": warning, "critical": critical}
    if "exclude_mountpoints" in payload:
        mounts = payload["exclude_mountpoints"]
        if not isinstance(mounts, list) or any(not isinstance(item, str) or not item.startswith("/") for item in mounts):
            raise ValueError("Excluded mount points must be an array of absolute paths")
        current.setdefault("health", {})["exclude_mountpoints"] = sorted(set(mounts))
    if "services" in payload:
        services = payload["services"]
        if not isinstance(services, dict):
            raise ValueError("Services settings must be an object")
        configured_services = current.setdefault("services", {})
        for name, enabled in services.items():
            if name not in configured_services or not isinstance(enabled, bool):
                raise ValueError(f"Invalid service setting: {name}")
            configured_services[name]["enabled"] = enabled
    temporary = CONFIG_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    temporary.replace(CONFIG_FILE)
    return current


def load_alert_history():
    """Load the most recent valid alert events from disk."""
    if not ALERTS_FILE.exists():
        return []

    events = []
    try:
        with ALERTS_FILE.open("r", encoding="utf-8") as file:
            for line in file:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                    if isinstance(event, dict) and event.get("name"):
                        events.append(event)
                except json.JSONDecodeError:
                    continue
    except OSError:
        logging.getLogger("live_dashboard").exception(
            "Failed to load alert history"
        )
        return []

    return events[-MAX_ALERTS:]


def save_alert_event(event):
    """Persist an alert and retain only the latest MAX_ALERTS events."""
    try:
        with ALERTS_FILE.open("a", encoding="utf-8") as file:
            file.write(
                json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                + "\n"
            )

        events = load_alert_history()
        with ALERTS_FILE.open("w", encoding="utf-8") as file:
            for item in events:
                file.write(
                    json.dumps(item, ensure_ascii=False, separators=(",", ":"))
                    + "\n"
                )
    except OSError:
        logging.getLogger("live_dashboard").exception(
            "Failed to save alert history"
        )


alert_history = load_alert_history()
alert_states = {}

# Restore the last known state of each resource.
for saved_alert in alert_history:
    name = saved_alert.get("name")
    status = saved_alert.get("current_status")
    if name and status:
        alert_states[name] = status

MAX_ALERTS = 100


def process_alerts(report):
    """Create alert events only when a resource state changes."""
    checks = (report.get("health") or {}).get("checks", [])
    now = datetime.now(UTC).isoformat()

    with alert_lock:
        for check in checks:
            name = str(check.get("name", "unknown"))
            current = str(check.get("status", "UNKNOWN")).upper()
            previous = alert_states.get(name)

            if current not in {"OK", "WARNING", "CRITICAL"}:
                continue

            event = None

            if current in {"WARNING", "CRITICAL"}:
                if previous != current:
                    event = current
            elif current == "OK" and previous in {"WARNING", "CRITICAL"}:
                event = "RECOVERED"

            alert_states[name] = current

            if event is None:
                continue

            value = check.get("value")
            warning = check.get("warning_threshold")
            critical = check.get("critical_threshold")

            if name.startswith("disk_io:"):
                unit = "MB/s"
            elif name == "failed_systemd_units":
                unit = "units"
            else:
                unit = "%"

            def display(value, unit=unit):
                if isinstance(value, (int, float)):
                    return f"{value:.2f} {unit}"
                return str(value) if value is not None else "N/A"

            threshold = (
                f"Warning: {display(warning)} | "
                f"Critical: {display(critical)}"
            )

            if event == "RECOVERED":
                message = f"{name} returned to normal"
                severity = "RECOVERED"
            else:
                message = f"{name} entered {event} state"
                severity = event

            event = {
                "timestamp_utc": now,
                "severity": severity,
                "name": name,
                "value": display(value),
                "threshold": threshold,
                "message": message,
                "previous_status": previous or "UNKNOWN",
                "current_status": current,
            }

            alert_history.append(event)
            del alert_history[:-MAX_ALERTS]
            save_alert_event(event)


def collect_loop():
    global latest_report

    while True:
        try:
            report = monitor.collect_report(logger)
            monitor.save_report_history(report, logger)
            process_alerts(report)

            with report_lock:
                latest_report = report

        except Exception:
            logger.exception("Live monitoring collection failed")

        time.sleep(INTERVAL)


LIVE_SCRIPT = r"""
<style>
#live-monitor-status {
    position: static;
    z-index: 1;
    padding: 8px 12px;
    border-radius: 30px;
    background: #102c28;
    color: #5ee0ae;
    font: 800 11px Arial, sans-serif;
    border: 1px solid #245d4d;
    white-space: nowrap;
}
#live-monitor-status.offline {
    background: #391b24;
    color: #ff7185;
    border-color: #873448;
}
</style>

<script>
(() => {
    const INTERVAL = 5000;
    const statusBox = document.getElementById("live-monitor-status");

    // Sidebar navigation switches between focused monitoring pages without reloads.
    const pageTitles = {
        dashboard: ["Server Health Dashboard", "INFRASTRUCTURE / OVERVIEW"],
        metrics: ["System Metrics", "PERFORMANCE / RESOURCE TELEMETRY"],
        alerts: ["Alert Center", "EVENTS / HEALTH ALERTS"],
        disks: ["Disk Monitoring", "STORAGE / FILESYSTEMS"],
        network: ["Network Monitoring", "NETWORK / INTERFACES / TRAFFIC"],
        services: ["Services & Sockets", "SYSTEMD / LISTENING SERVICES"],
        processes: ["Process Monitor", "SYSTEM / TOP PROCESSES"],
        logs: ["System Logs", "DIAGNOSTICS / LOG EVENTS"],
        history: ["Health History", "TELEMETRY / HISTORICAL REPORTS"],
        settings: ["Dashboard Settings", "CONFIGURATION / THRESHOLDS"]
    };
    function setActivePage(page, updateHash = false) {
        if (!Object.prototype.hasOwnProperty.call(pageTitles, page)) page = "dashboard";
        document.body.dataset.page = page;
        document.querySelectorAll(".nav a[data-page]").forEach(link => {
            const active = link.dataset.page === page;
            link.classList.toggle("active", active);
            if (active) link.setAttribute("aria-current", "page");
            else link.removeAttribute("aria-current");
        });
        const title = document.getElementById("page-title");
        const eyebrow = document.querySelector(".eyebrow");
        if (title) title.textContent = pageTitles[page][0];
        if (eyebrow) eyebrow.textContent = pageTitles[page][1];
        if (updateHash && window.location.hash !== `#${page}`) {
            window.history.pushState({page}, "", `#${page}`);
        }
        window.dispatchEvent(new Event("resize"));
    }
    document.querySelectorAll(".nav a[data-page]").forEach(link => {
        link.addEventListener("click", event => {
            event.preventDefault();
            setActivePage(link.dataset.page, true);
        });
    });
    window.addEventListener("popstate", () => setActivePage(window.location.hash.slice(1)));
    setActivePage(window.location.hash.slice(1) || "dashboard");

    function displayName(name) {
        if (name.startsWith("disk_io:")) {
            const parts = name.split(":");
            if (parts.length === 3) {
                const operation = parts[2] === "read" ? "Read" : "Write";
                return `Disk ${operation} Throughput (${parts[1]})`;
            }
        }
        return name;
    }

    function statusClass(status) {
        const value = String(status).toLowerCase();
        if (["ok", "healthy", "active"].includes(value)) return "good";
        if (["warning", "degraded"].includes(value)) return "warning";
        if (["critical", "failed", "error"].includes(value)) return "critical";
        return "neutral";
    }

    function updateCards(report) {
        const checks = report.health?.checks || [];
        const checkMap = new Map(
            checks.map(check => [displayName(check.name), check])
        );

        document.querySelectorAll(".metric.card").forEach(card => {
            const nameElement = card.querySelector(".metric-name");
            if (!nameElement) return;

            const check = checkMap.get(nameElement.textContent.trim());
            if (!check) return;

            const name = check.name;
            const valueElement = card.querySelector(".metric-value");
            const badge = card.querySelector(".badge");
            const fill = card.querySelector(".progress-fill");
            const track = card.querySelector(".progress-track");
            const muted = card.querySelector(".muted");

            const isDiskIO = name.startsWith("disk_io:");
            const unit = isDiskIO ? " MB/s" :
                (name === "cpu" || name === "memory" || name.startsWith("disk:"))
                ? "%" : "";

            if (valueElement) {
                valueElement.textContent = check.value + unit;
            }

            const status = statusClass(check.status);

            if (badge) {
                badge.textContent = check.status;
                badge.classList.remove("good", "warning", "critical", "neutral");
                badge.classList.add(status);
            }

            let progress = Number(check.value);

            if (isDiskIO) {
                const critical = Number(check.critical_threshold);
                progress = critical > 0 ? progress / critical * 100 : 0;
            }

            progress = Math.max(0, Math.min(100, progress || 0));

            if (fill) {
                fill.style.width = progress.toFixed(1) + "%";
                fill.classList.remove("good", "warning", "critical", "neutral");
                fill.classList.add(status);
            }

            if (track) {
                track.setAttribute("aria-valuenow", progress.toFixed(1));
            }

            if (muted) {
                muted.textContent =
                    `Warning: ${check.warning_threshold}${unit} · ` +
                    `Critical: ${check.critical_threshold}${unit}`;
            }
        });

        const resourceBadge = [...document.querySelectorAll(".badge")]
            .find(el => el.textContent.trim().startsWith("RESOURCE HEALTH:"));

        if (resourceBadge) {
            resourceBadge.textContent =
                "RESOURCE HEALTH: " + report.health.status;

            resourceBadge.classList.remove(
                "good", "warning", "critical", "neutral"
            );
            resourceBadge.classList.add(statusClass(report.health.status));
        }

        const generated = report.timestamp_utc || new Date().toISOString();

        const timestampElements = [...document.querySelectorAll("*")]
            .filter(el =>
                el.children.length === 0 &&
                el.textContent.trim().startsWith("Generated:")
            );

        timestampElements.forEach(el => {
            el.textContent = "Generated: " + generated;
        });

        statusBox.classList.remove("offline");
        statusBox.textContent =
            "● LIVE · " + new Date(generated).toLocaleTimeString();
    }
function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, char => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
    })[char]);
}

function formatBytes(value) {
    const bytes = Number(value);
    if (!Number.isFinite(bytes) || bytes < 0) return "N/A";
    if (bytes < 1024) return bytes.toFixed(0) + " B";
    const units = ["KB", "MB", "GB", "TB", "PB"];
    let size = bytes;
    let unit = -1;
    do {
        size /= 1024;
        unit += 1;
    } while (size >= 1024 && unit < units.length - 1);
    return size.toFixed(2) + " " + units[unit];
}

function updateTable(id, rows, emptyMessage, columns) {
    const tbody = document.getElementById(id);
    if (!tbody) return;

    if (!rows.length) {
        tbody.innerHTML =
            `<tr><td colspan="${columns}">${escapeHtml(emptyMessage)}</td></tr>`;
        return;
    }

    tbody.innerHTML = rows.join("");
}

function statusBadge(status) {
    const value = String(status ?? "UNKNOWN");
    return `<span class="badge ${statusClass(value)}">${escapeHtml(value)}</span>`;
}

function updateTables(report) {

    // Disk usage
    const disks = report.disks || [];

    updateTable(
        "disk-table-body",
        disks.map(d => `
            <tr>
                <td>${escapeHtml(d.mountpoint)}</td>
                <td>${escapeHtml(d.usage_percent)}%</td>
                <td>${escapeHtml(d.type)}</td>
                <td>${d.monitored === false ? '<span class="badge neutral">Excluded (WSL)</span>' : '<span class="badge good">Monitored</span>'}</td>
            </tr>
        `),
        "No disk information available.",
        4
    );

    // Network interfaces
    const network = report.network || [];

    updateTable(
        "network-table-body",
        network.map(n => `
            <tr>
                <td>${escapeHtml(n.name)}</td>
                <td>${statusBadge(n.state)}</td>
                <td>${escapeHtml(n.mac_address)}</td>
                <td>${escapeHtml((n.ipv4 || []).join(", ") || "N/A")}</td>
                <td>${report.network_io?.interfaces?.[n.name] ? `${escapeHtml(report.network_io.interfaces[n.name].rx_mbps)} MB/s` : 'Sampling…'}</td>
                <td>${report.network_io?.interfaces?.[n.name] ? `${escapeHtml(report.network_io.interfaces[n.name].tx_mbps)} MB/s` : 'Sampling…'}</td>
                <td>${formatBytes(report.network_io?.interfaces?.[n.name]?.total_rx_bytes)}</td>
                <td>${formatBytes(report.network_io?.interfaces?.[n.name]?.total_tx_bytes)}</td>
            </tr>
        `),
        "No network interfaces available.",
        8
    );

    // Listening sockets
    const sockets = report.listening_sockets?.items || [];

    updateTable(
        "socket-table-body",
        sockets.map(s => `
            <tr>
                <td>${escapeHtml(s.protocol)}</td>
                <td>${escapeHtml(s.state)}</td>
                <td>${escapeHtml(s.local_address)}</td>
                <td>${escapeHtml(s.process || "Unknown")}</td>
            </tr>
        `),
        "No listening sockets available.",
        4
    );

    // Services
    const services = report.services || {};

    updateTable(
        "service-table-body",
        Object.entries(services).map(([name, service]) => {
            const value = typeof service === "object"
                ? service.status
                : service;

            return `
                <tr>
                    <td>${escapeHtml(name)}</td>
                    <td>${statusBadge(value)}</td>
                </tr>
            `;
        }),
        "No service information available.",
        2
    );

    // Top processes
    const processes = report.top_processes || [];

    updateTable(
        "process-table-body",
        processes.map(p => `
            <tr>
                <td>${escapeHtml(p.pid)}</td>
                <td>${escapeHtml(p.user || "N/A")}</td>
                <td title="${escapeHtml(p.command || p.name)}">${escapeHtml(p.command || p.name)}</td>
                <td>${escapeHtml(p.cpu_percent)}%</td>
                <td>${escapeHtml(p.memory_percent)}%</td>
            </tr>
        `),
        "No process information available.",
        5
    );

    // Failed systemd units
    const units = report.system_diagnostics?.failed_units || [];

    updateTable(
        "failed-units-table-body",
        units.map(u => `
            <tr>
                <td>${escapeHtml(u.unit)}</td>
                <td>${statusBadge(u.active)}</td>
                <td>${escapeHtml(u.description)}</td>
            </tr>
        `),
        "No failed units reported.",
        3
    );
}
    const chartHistory = [];

    function drawChart(canvasId, values, color, maxValue, label, secondary = null, timestamps = []) {
        const canvas = document.getElementById(canvasId);
        if (!canvas) return;

        const ctx = canvas.getContext("2d");
        const width = canvas.clientWidth || 300;
        const height = 170;
        const ratio = window.devicePixelRatio || 1;

        canvas.width = width * ratio;
        canvas.height = height * ratio;
        canvas.style.height = height + "px";

        ctx.scale(ratio, ratio);
        ctx.clearRect(0, 0, width, height);

        const pad = {top: 12, right: 12, bottom: 42, left: 38};
        const plotW = width - pad.left - pad.right;
        const plotH = height - pad.top - pad.bottom;

        ctx.font = "10px Arial";
        ctx.fillStyle = "#94a3b8";
        ctx.strokeStyle = "rgba(148,163,184,0.18)";
        ctx.lineWidth = 1;

        for (let i = 0; i <= 4; i++) {
            const y = pad.top + plotH * i / 4;
            ctx.beginPath();
            ctx.moveTo(pad.left, y);
            ctx.lineTo(width - pad.right, y);
            ctx.stroke();

            ctx.fillText(
                (maxValue * (1 - i / 4)).toFixed(0),
                4, y + 3
            );
        }

        if (!values.length) {
            ctx.fillText("Waiting for data...", pad.left + 8, height / 2);
            return;
        }

        function drawLine(data, stroke, dashed = false) {
            if (!data.length) return;

            ctx.beginPath();
            ctx.strokeStyle = stroke;
            ctx.lineWidth = 2;
            ctx.setLineDash(dashed ? [5, 4] : []);

            data.forEach((value, index) => {
                const x = pad.left + (
                    data.length === 1 ? plotW / 2 :
                    plotW * index / (data.length - 1)
                );
                const y = pad.top + plotH * (
                    1 - Math.min(maxValue, Math.max(0, value)) / maxValue
                );

                if (index === 0) ctx.moveTo(x, y);
                else ctx.lineTo(x, y);
            });

            ctx.stroke();
            ctx.setLineDash([]);
        }

        drawLine(values, color);

        if (secondary) {
            drawLine(secondary.values, secondary.color, true);
        }

        // Time axis: first, middle, and latest sample
        if (timestamps.length) {
            const indexes = [...new Set([
                0,
                Math.floor((timestamps.length - 1) / 2),
                timestamps.length - 1
            ])];

            ctx.font = "9px Arial";
            ctx.fillStyle = "#94a3b8";
            ctx.textAlign = "center";

            indexes.forEach(index => {
                const date = new Date(timestamps[index]);
                if (Number.isNaN(date.getTime())) return;

                const x = pad.left + (
                    timestamps.length === 1
                        ? plotW / 2
                        : plotW * index / (timestamps.length - 1)
                );

                ctx.fillText(
                    date.toLocaleTimeString([], {
                        hour: "2-digit",
                        minute: "2-digit"
                    }),
                    x,
                    height - 23
                );
            });

            ctx.textAlign = "left";
        }

        // Chart legend
        const legendY = height - 5;
        ctx.font = "10px Arial";
        ctx.fillStyle = color;
        ctx.fillRect(pad.left, legendY - 7, 12, 3);
        ctx.fillStyle = "#94a3b8";
        ctx.fillText(label, pad.left + 17, legendY);

        if (secondary) {
            ctx.fillStyle = secondary.color;
            ctx.setLineDash([3, 2]);
            ctx.beginPath();
            ctx.moveTo(pad.left + 105, legendY - 5);
            ctx.lineTo(pad.left + 119, legendY - 5);
            ctx.stroke();
            ctx.setLineDash([]);
            ctx.fillStyle = "#94a3b8";
            ctx.fillText(secondary.label, pad.left + 124, legendY);
        }
    }

    function updatePerformanceSummary(cpu, memory, read, write, timestamps) {
        function numericValues(values) {
            return values.filter(
                value => typeof value === "number" && Number.isFinite(value)
            );
        }

        function average(values) {
            const valid = numericValues(values);
            if (!valid.length) return "--";
            return (
                valid.reduce((sum, value) => sum + value, 0) /
                valid.length
            ).toFixed(1) + "%";
        }

        function peakDetails(values, unit) {
            const validIndexes = values
                .map((value, index) => ({value, index}))
                .filter(item =>
                    typeof item.value === "number" &&
                    Number.isFinite(item.value)
                );

            if (!validIndexes.length) {
                return {value: "--", time: "No data"};
            }

            const maximum = validIndexes.reduce(
                (best, item) => item.value > best.value ? item : best
            );

            const timestamp = timestamps[maximum.index];
            let time = "Time unavailable";

            if (timestamp) {
                const date = new Date(timestamp);
                if (!Number.isNaN(date.getTime())) {
                    time = new Intl.DateTimeFormat("en-GB", {
                        timeZone: "Africa/Cairo",
                        hour: "2-digit",
                        minute: "2-digit",
                        second: "2-digit",
                        hour12: true
                    }).format(date);
                }
            }

            return {
                value: maximum.value.toFixed(2) + unit,
                time: "At " + time
            };
        }

        function setMetric(id, value) {
            const element = document.getElementById(id);
            if (element) element.textContent = value;
        }

        const peakCpu = peakDetails(cpu, "%");
        const peakMemory = peakDetails(memory, "%");
        const maxRead = peakDetails(read, " MB/s");
        const maxWrite = peakDetails(write, " MB/s");

        setMetric("summary-avg-cpu", average(cpu));
        setMetric("summary-peak-cpu", peakCpu.value);
        setMetric("summary-peak-cpu-time", peakCpu.time);

        setMetric("summary-avg-memory", average(memory));
        setMetric("summary-peak-memory", peakMemory.value);
        setMetric("summary-peak-memory-time", peakMemory.time);

        setMetric("summary-max-read", maxRead.value);
        setMetric("summary-max-read-time", maxRead.time);

        setMetric("summary-max-write", maxWrite.value);
        setMetric("summary-max-write-time", maxWrite.time);
    }

    function updateCharts(history) {
        const rangeSelect = document.getElementById("chart-range");
        const minutes = Number(rangeSelect?.value || 60);
        const cutoff = Date.now() - minutes * 60 * 1000;

        const valid = (history || [])
            .filter(item =>
                item &&
                typeof item === "object" &&
                item.timestamp_utc &&
                !Number.isNaN(Date.parse(item.timestamp_utc))
            )
            .filter(item => Date.parse(item.timestamp_utc) >= cutoff)
            .sort((a, b) =>
                Date.parse(a.timestamp_utc) - Date.parse(b.timestamp_utc)
            );

        const timestamps = valid.map(item => item.timestamp_utc);
        const cpu = valid.map(item => item.cpu?.usage_percent ?? 0);
        const memory = valid.map(item => item.memory?.usage_percent ?? 0);

        const read = valid.map(item => {
            const devices = item.disk_io || {};
            return Object.values(devices).reduce(
                (sum, device) =>
                    sum + (Number(device.read_bytes_per_sec) || 0),
                0
            ) / (1024 * 1024);
        });

        const write = valid.map(item => {
            const devices = item.disk_io || {};
            return Object.values(devices).reduce(
                (sum, device) =>
                    sum + (Number(device.write_bytes_per_sec) || 0),
                0
            ) / (1024 * 1024);
        });

        updatePerformanceSummary(cpu, memory, read, write, timestamps);

        drawChart(
            "cpu-chart", cpu, "#38bdf8", 100, "CPU %",
            null, timestamps
        );

        drawChart(
            "memory-chart", memory, "#a78bfa", 100, "Memory %",
            null, timestamps
        );

        const diskMax = Math.max(10, ...read, ...write) * 1.15;

        drawChart(
            "disk-io-chart",
            read,
            "#38bdf8",
            diskMax,
            "Read MB/s",
            {
                values: write,
                color: "#f59e0b",
                label: "Write MB/s"
            },
            timestamps
        );

        const networkRx = valid.map(item =>
            Number(item.network_io?.rx_mbps) || 0
        );
        const networkTx = valid.map(item =>
            Number(item.network_io?.tx_mbps) || 0
        );
        const networkMax = Math.max(1, ...networkRx, ...networkTx) * 1.15;
        drawChart(
            "network-io-chart",
            networkRx,
            "#38bdf8",
            networkMax,
            "Download MB/s",
            {
                values: networkTx,
                color: "#f59e0b",
                label: "Upload MB/s"
            },
            timestamps
        );

        const peakRx = Math.max(0, ...networkRx);
        const peakTx = Math.max(0, ...networkTx);
        const setNetworkStat = (id, value) => {
            const element = document.getElementById(id);
            if (element) element.textContent = value;
        };
        setNetworkStat("network-peak-rx", peakRx.toFixed(3) + " MB/s");
        setNetworkStat("network-peak-tx", peakTx.toFixed(3) + " MB/s");
    }

    let allAlerts = [];
    function renderAlerts(alerts) {
        allAlerts = alerts || [];
        const search = (document.getElementById("alert-search")?.value || "").toLowerCase();
        const severity = document.getElementById("alert-severity")?.value || "ALL";
        alerts = allAlerts.filter(item =>
            (severity === "ALL" || String(item.severity).toUpperCase() === severity) &&
            (!search || `${item.name} ${item.message} ${item.value}`.toLowerCase().includes(search))
        );
        const body = document.getElementById("alerts-table-body");
        const summary = document.getElementById("alerts-summary");

        if (!body || !summary) return;

        const critical = alerts.filter(
            item => item.severity === "CRITICAL"
        ).length;

        const warnings = alerts.filter(
            item => item.severity === "WARNING"
        ).length;

        const recovered = alerts.filter(
            item => item.severity === "RECOVERED"
        ).length;

        summary.textContent =
            `${alerts.length} shown / ${allAlerts.length} total · ` +
            `Critical: ${critical} · Warnings: ${warnings} · Recovered: ${recovered}`;
        if (!alerts.length) {
            body.innerHTML = `<tr><td colspan="6">${allAlerts.length ? "No alerts match these filters." : "No alerts yet. All monitored resources are normal."}</td></tr>`;
            return;
        }

        body.replaceChildren();

        alerts.slice(0, 100).forEach(item => {
            const row = document.createElement("tr");

            const date = new Date(item.timestamp_utc);
            const time = Number.isNaN(date.getTime())
                ? "Unknown"
                : date.toLocaleString("en-GB", {
                    timeZone: "Africa/Cairo",
                    hour12: true
                });

            const fields = [
                time,
                item.severity,
                item.name,
                item.value,
                item.threshold,
                item.message
            ];

            fields.forEach((value, index) => {
                const cell = document.createElement("td");
                cell.textContent = value ?? "N/A";

                if (index === 1) {
                    const severity = String(value).toLowerCase();
                    cell.style.fontWeight = "bold";

                    if (severity === "critical") {
                        cell.style.color = "#ff7185";
                    } else if (severity === "warning") {
                        cell.style.color = "#f4b74f";
                    } else if (severity === "recovered") {
                        cell.style.color = "#4ade80";
                    }
                }

                row.appendChild(cell);
            });

            body.appendChild(row);
        });
    }

    async function refreshAlerts() {
        try {
            const response = await fetch("/api/alerts", {
                cache: "no-store"
            });

            if (!response.ok) {
                throw new Error("Alerts API failed");
            }

            renderAlerts(await response.json());
        } catch (error) {
            console.error("Alerts update failed:", error);
        }
    }

    async function refreshHistory() {
        try {
            const response = await fetch("/api/history", {
                cache: "no-store"
            });
            if (!response.ok) throw new Error("History API failed");
            const history = await response.json();
            updateCharts(history);
            const body = document.getElementById("history-table-body");
            if (body) {
                const rows = history.slice(-10).reverse().map(item => {
                    const date = new Date(item.timestamp_utc);
                    const stamp = Number.isNaN(date.getTime()) ? "Unknown" : date.toLocaleString();
                    return `<tr><td class="history-time">${escapeHtml(stamp)}</td><td>${statusBadge(item.health?.status || "UNKNOWN")}</td><td>${escapeHtml(item.cpu?.usage_percent ?? "N/A")}%</td><td>${escapeHtml(item.memory?.usage_percent ?? "N/A")}%</td></tr>`;
                });
                updateTable("history-table-body", rows, "No health history available.", 4);
                const summary = document.getElementById("history-summary");
                if (summary) summary.textContent = `${history.length} retained samples`;
            }
        } catch (error) {
            console.error("Chart history update failed:", error);
        }
    }

    document.getElementById("chart-range")?.addEventListener(
        "change",
        refreshHistory
    );

    async function loadSettings() {
        const response = await fetch("/api/config", {cache: "no-store"});
        if (!response.ok) throw new Error("Could not load settings");
        const config = await response.json();
        for (const resource of ["cpu", "memory", "disk"]) {
            const values = config.thresholds?.[resource] || {};
            const warning = document.getElementById(`setting-${resource}-warning`);
            const critical = document.getElementById(`setting-${resource}-critical`);
            if (warning) warning.value = values.warning ?? "";
            if (critical) critical.value = values.critical ?? "";
        }
        const mounts = document.getElementById("setting-excluded-mounts");
        if (mounts) mounts.value = (config.health?.exclude_mountpoints || []).join("\n");
        for (const [name, settings] of Object.entries(config.services || {})) {
            const checkbox = document.getElementById(`setting-service-${name}`);
            if (checkbox) checkbox.checked = settings.enabled !== false;
        }
        const status = document.getElementById("settings-status");
        if (status) status.textContent = "Settings loaded";
    }

    document.getElementById("settings-form")?.addEventListener("submit", async event => {
        event.preventDefault();
        const payload = {};
        for (const resource of ["cpu", "memory", "disk"]) {
            payload[resource] = {
                warning: Number(document.getElementById(`setting-${resource}-warning`).value),
                critical: Number(document.getElementById(`setting-${resource}-critical`).value)
            };
        }
        payload.exclude_mountpoints = (document.getElementById("setting-excluded-mounts")?.value || "")
            .split(/\r?\n/).map(value => value.trim()).filter(Boolean);
        payload.services = Object.fromEntries(["ssh", "sshd", "docker", "nginx"].map(name => [
            name, Boolean(document.getElementById(`setting-service-${name}`)?.checked)
        ]));
        const status = document.getElementById("settings-status");
        try {
            const response = await fetch("/api/config", {
                method: "POST", headers: {"Content-Type": "application/json"},
                body: JSON.stringify(payload)
            });
            const result = await response.json();
            if (!response.ok) throw new Error(result.error || "Save failed");
            if (status) status.textContent = "Saved. New thresholds apply on the next collection cycle.";
        } catch (error) {
            if (status) status.textContent = error.message;
        }
    });

    function downloadCsv(filename, rows) {
        const csv = rows.map(row => row.map(value => `"${String(value ?? "").replaceAll('"', '""')}"`).join(",")).join("\r\n");
        const url = URL.createObjectURL(new Blob([csv], {type: "text/csv;charset=utf-8"}));
        const link = document.createElement("a");
        link.href = url; link.download = filename; link.click(); URL.revokeObjectURL(url);
    }
    document.getElementById("export-alerts")?.addEventListener("click", () => {
        downloadCsv("server-alerts.csv", [["Timestamp", "Severity", "Resource", "Value", "Threshold", "Message"], ...allAlerts.map(a => [a.timestamp_utc, a.severity, a.name, a.value, a.threshold, a.message])]);
    });
    document.getElementById("export-history")?.addEventListener("click", async () => {
        const response = await fetch("/api/history", {cache: "no-store"});
        if (!response.ok) return;
        const history = await response.json();
        downloadCsv("server-health-history.csv", [["Timestamp UTC", "Health", "CPU %", "Memory %", "Disk Read B/s", "Disk Write B/s"], ...history.map(item => {
            const devices = Object.values(item.disk_io || {});
            return [item.timestamp_utc, item.health?.status, item.cpu?.usage_percent, item.memory?.usage_percent, devices.reduce((sum, d) => sum + (d.read_bytes_per_sec || 0), 0), devices.reduce((sum, d) => sum + (d.write_bytes_per_sec || 0), 0)];
        })]);
    });
    document.getElementById("alert-search")?.addEventListener("input", () => renderAlerts(allAlerts));
    document.getElementById("alert-severity")?.addEventListener("change", () => renderAlerts(allAlerts));
    loadSettings().catch(error => {
        const status = document.getElementById("settings-status");
        if (status) status.textContent = error.message;
    });

    function updateMetricsDetails(report) {
        const memory = report.memory || {};
        const setText = (id, value) => {
            const element = document.getElementById(id);
            if (element) element.textContent = value;
        };
        const gb = value => Number.isFinite(Number(value))
            ? `${(Number(value) / 1073741824).toFixed(2)} GB` : "N/A";
        setText("metrics-iowait", report.cpu?.iowait_percent == null
            ? "N/A" : `${Number(report.cpu.iowait_percent).toFixed(2)}%`);
        setText("metrics-ram-used", gb(memory.used_bytes));
        setText("metrics-ram-total", `Total memory: ${gb(memory.total_bytes)}`);
        setText("metrics-swap-used", gb(memory.swap_used_bytes));
        setText("metrics-swap-total", `Total swap: ${gb(memory.swap_total_bytes)}`);
        setText("metrics-connections", report.active_connections == null
            ? "N/A" : String(report.active_connections));
        const body = document.getElementById("disk-io-device-body");
        if (!body) return;
        const devices = Object.entries(report.disk_io || {});
        if (!devices.length) {
            body.innerHTML = '<tr><td colspan="5">No disk I/O metrics available.</td></tr>';
            return;
        }
        body.innerHTML = devices.map(([name, item]) => {
            const mb = value => `${(Number(value || 0) / 1048576).toFixed(3)} MB/s`;
            return `<tr><td>${escapeHtml(name)}</td>
                <td>${mb(item.read_bytes_per_sec)}</td>
                <td>${mb(item.write_bytes_per_sec)}</td>
                <td>${Number(item.reads_per_sec || 0).toFixed(1)}</td>
                <td>${Number(item.writes_per_sec || 0).toFixed(1)}</td></tr>`;
        }).join("");
    }

    function updateOverview(report) {
        const cpu = report.cpu?.usage_percent;
        const memory = report.memory?.usage_percent;
        const disks = report.disks || [];
        const diskValues = disks
            .filter(item => item.monitored !== false && !(item.type === "9p" && String(item.mountpoint || "").startsWith("/mnt/")))
            .map(item => Number(item.usage_percent))
            .filter(Number.isFinite);
        const disk = diskValues.length ? Math.max(...diskValues) : null;
        const network = report.network || [];
        const health = report.health?.status || "UNKNOWN";
        const system = report.system || {};
        const os = system.os || {};
        const uptime = Number(system.uptime_seconds);
        const uptimeText = Number.isFinite(uptime)
            ? `${Math.floor(uptime / 86400)}d ${Math.floor((uptime % 86400) / 3600)}h`
            : "N/A";
        const loadText = (system.load_average || [])
            .slice(0, 3).map(value => Number(value).toFixed(2)).join(" · ") || "N/A";

        const setText = (id, value) => {
            const element = document.getElementById(id);
            if (element) element.textContent = value;
        };
        const setBar = (id, value) => {
            const element = document.getElementById(id);
            if (element) element.style.width =
                Number.isFinite(Number(value)) ? `${Math.max(0, Math.min(100, Number(value)))}%` : "0%";
        };

        setText("kpi-cpu", cpu == null ? "N/A" : `${Number(cpu).toFixed(1)}%`);
        setText("kpi-memory", memory == null ? "N/A" : `${Number(memory).toFixed(1)}%`);
        setText("kpi-disk", disk == null ? "N/A" : `${disk.toFixed(1)}%`);
        const netIO = report.network_io || {};
        const rx = Number(netIO.rx_mbps || 0);
        const tx = Number(netIO.tx_mbps || 0);
        const networkReady = netIO.sample_ready !== false;
        setText("kpi-network", networkReady ? `${rx.toFixed(2)} / ${tx.toFixed(2)} MB/s` : "Sampling…");
        setText("network-kpi-foot", networkReady ? `↓ Download  ·  ↑ Upload` : "Collecting first interface counter sample");
        setText("network-total-rx", formatBytes(netIO.total_rx_bytes));
        setText("network-total-tx", formatBytes(netIO.total_tx_bytes));
        setBar("bar-cpu", cpu);
        setBar("bar-memory", memory);
        setBar("bar-disk", disk);
        setBar("bar-network", Math.min(100, (rx + tx) * 10));
        const thresholds = report.thresholds || {};
        const cpuT = thresholds.cpu || {};
        const memT = thresholds.memory || {};
        const diskT = thresholds.disk || {};
        setText("cpu-thresholds", `Warning ≥ ${cpuT.warning ?? 80}%  ·  Critical ≥ ${cpuT.critical ?? 95}%`);
        setText("memory-thresholds", `Warning ≥ ${memT.warning ?? 80}%  ·  Critical ≥ ${memT.critical ?? 95}%`);
        setText("disk-thresholds", `Warning ≥ ${diskT.warning ?? 80}%  ·  Critical ≥ ${diskT.critical ?? 95}%`);

        setText("server-hostname", system.hostname || "Unknown server");
        const primaryIP = (network || []).flatMap(item => item.ipv4 || [])
            .find(address => !address.startsWith("127.")) || "IP unavailable";
        setText("server-meta",
            `${os.distribution || "Unknown OS"} · Kernel ${os.kernel || "Unknown"} · IP: ${primaryIP}\nUptime: ${uptimeText} · Load: ${loadText}`);
        const state = document.getElementById("overall-health");
        if (state) {
            state.textContent = health.toUpperCase();
            state.className = "state " + statusClass(health);
        }
        setText("systemd-status", report.system_diagnostics?.systemd_status || "unknown");
        setText("quick-load", loadText);
        const sockets = report.listening_sockets?.items || [];
        setText("quick-sockets", String(sockets.length));
        const failed = report.system_diagnostics?.failed_units || [];
        setText("quick-failed", String(Array.isArray(failed) ? failed.length : 0));
        const connections = report.active_connections;
        setText("quick-connections", connections == null ? "N/A" : String(connections));
        setText("quick-generated", new Date(report.timestamp_utc || Date.now()).toLocaleTimeString());
        setText("extended-iowait", report.cpu?.iowait_percent == null ? "N/A" : `${Number(report.cpu.iowait_percent).toFixed(1)}%`);
        const swap = report.memory?.swap_usage_percent;
        setText("extended-swap", swap == null ? "N/A" : `${Number(swap).toFixed(1)}%`);
        const swapUsed = Number(report.memory?.swap_used_bytes || 0);
        const swapTotal = Number(report.memory?.swap_total_bytes || 0);
        setText("extended-swap-detail", `${(swapUsed / 1073741824).toFixed(2)} / ${(swapTotal / 1073741824).toFixed(2)} GB used`);
        const rootDisk = disks.find(item => item.mountpoint === "/" && item.monitored !== false);
        const freeBytes = Number(rootDisk?.available_bytes);
        setText("extended-root-free", Number.isFinite(freeBytes) ? `${(freeBytes / 1073741824).toFixed(1)} GB` : "N/A");
    }

    async function refreshDashboard() {
        try {
            const response = await fetch("/api/health", {
                cache: "no-store"
            });

            if (!response.ok) {
                throw new Error("API returned " + response.status);
            }

            const report = await response.json();
            updateOverview(report);
            updateMetricsDetails(report);
            updateCards(report);
            updateTables(report);
            await refreshHistory();
            await refreshAlerts();

        } catch (error) {
            statusBox.classList.add("offline");
            statusBox.textContent = "● CONNECTION LOST · Retrying...";
        }
    }

    refreshDashboard();
    setInterval(refreshDashboard, INTERVAL);
})();
</script>
"""


class DashboardHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path == "/api/config":
            content = json.dumps(read_dashboard_config(), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        if self.path == "/api/alerts":
            with alert_lock:
                alerts = list(reversed(alert_history))

            self.send_response(200)
            self.send_header(
                "Content-Type",
                "application/json; charset=utf-8"
            )
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(
                json.dumps(alerts, ensure_ascii=False).encode("utf-8")
            )
            return

        if self.path == "/api/history":
            history = monitor.load_report_history(720)
            content = json.dumps(
                history, ensure_ascii=False
            ).encode("utf-8")

            self.send_response(200)
            self.send_header(
                "Content-Type", "application/json; charset=utf-8"
            )
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        if self.path == "/api/health":
            with report_lock:
                report = latest_report

            if report is None:
                self.send_error(503, "Initial report is not ready")
                return

            content = json.dumps(
                report, ensure_ascii=False
            ).encode("utf-8")

            self.send_response(200)
            self.send_header(
                "Content-Type", "application/json; charset=utf-8"
            )
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        if self.path != "/":
            self.send_error(404)
            return

        with report_lock:
            report = latest_report

        if report is None:
            self.send_error(503, "Initial report is not ready")
            return

        html = monitor.generate_html_report(
            report,
            monitor.load_report_history(10),
        )

        html = html.replace("</body>", LIVE_SCRIPT + "</body>")

        content = html.encode("utf-8")

        self.send_response(200)
        self.send_header(
            "Content-Type", "text/html; charset=utf-8"
        )
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self):
        if self.path != "/api/config":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 16384:
                raise ValueError("Invalid settings payload size")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            config = validate_dashboard_config(payload)
            content = json.dumps({"ok": True, "config": config}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            content = json.dumps({"ok": False, "error": str(error)}).encode("utf-8")
            self.send_response(400)
        except OSError as error:
            content = json.dumps({"ok": False, "error": str(error)}).encode("utf-8")
            self.send_response(500)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    collector = threading.Thread(
        target=collect_loop,
        daemon=True,
    )
    collector.start()

    server = ThreadingHTTPServer((HOST, PORT), DashboardHandler)

    print(f"Live Dashboard: http://localhost:{PORT}")
    print(f"API Endpoint: http://localhost:{PORT}/api/health")
    print(f"Update interval: {INTERVAL} seconds")
    print("Dashboard updates without page reload.")
    print("Press CTRL+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()
