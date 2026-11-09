# Changelog

## 0.9.0 - Multi-page monitoring workspace

### Navigation and dashboard
- Converted the sidebar from scroll-to-section links into focused in-page views with browser hash navigation and back/forward support.
- Added dedicated Dashboard, Metrics, Alerts, Disks, Network, Services, Processes, Logs, History, and Settings views.
- Kept live collection and existing API endpoints running across page changes without a full reload.

### Metrics
- Added a live resource detail view for CPU I/O wait, RAM, swap, active connections, and per-device disk I/O rates.
- Added explicit empty states for unavailable disk I/O data.

### Quality
- Added regression coverage for all navigation targets and live metrics refresh hooks.
- Preserved the existing network observability and settings functionality.

## 0.8.0 - Network observability

### Network monitoring
- Added historical RX/TX throughput chart using retained health-report history.
- Added cumulative received/sent byte counters for each interface and aggregate totals.
- Added peak download/upload statistics for the selected history range.
- Expanded interface inventory with per-interface traffic rates and cumulative counters.
- Kept the existing `/api/health`, `/api/history`, `/api/alerts`, and `/api/config` contracts.

### Quality
- Added regression tests for initial network counter samples, byte totals, and throughput rate calculations.


## 0.7.0 - ServerPulse dashboard and monitoring improvements

### Dashboard and usability
- Standardized sidebar, KPI, brand, and quick-stat icons with inline SVG.
- Added active sidebar state while navigating between dashboard sections.
- Added CPU I/O wait, swap utilization, and root filesystem free-space telemetry.
- Made the health history table refresh live and display readable local timestamps.

### Monitoring and configuration
- WSL Windows drive mounts remain visible in disk inventory but are excluded from Linux disk health and the disk KPI.
- Added editable CPU, memory, and disk warning/critical thresholds.
- Added service monitoring toggles for configured services.
- Added persistent settings through the local `/api/config` endpoint.
- Network throughput reports whether the first counter sample is still warming up.
- Added WSL-specific filtering for known host-integration journal noise.

### Alerting and exports
- Added alert search and severity filters.
- Added CSV exports for alert events and health history.

### Quality
- Added dashboard feature regression tests.
- Expanded CI to run the full unittest suite.
