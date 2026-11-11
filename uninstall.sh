#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PURGE_DATA=false
if [[ "${1:-}" == "--purge-data" ]]; then
  PURGE_DATA=true
elif [[ $# -gt 0 ]]; then
  echo "Usage: ./uninstall.sh [--purge-data]" >&2
  exit 2
fi

if command -v systemctl >/dev/null 2>&1; then
  systemctl --user disable --now serverpulse.service 2>/dev/null || true
  SERVICE_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/serverpulse.service"
  rm -f "$SERVICE_FILE"
  systemctl --user daemon-reload 2>/dev/null || true
fi

rm -rf "$ROOT_DIR/.venv"
if [[ "$PURGE_DATA" == true ]]; then
  rm -f "$ROOT_DIR/health_history.sqlite3" "$ROOT_DIR/health_history.sqlite3-shm" \
        "$ROOT_DIR/health_history.sqlite3-wal" "$ROOT_DIR/security_monitor_state.json" \
        "$ROOT_DIR/security_file_baseline.json" "$ROOT_DIR/alerts_history.jsonl"
  rm -rf "$ROOT_DIR/logs"
  echo "Runtime data and logs were removed."
else
  echo "Runtime data, logs, configuration, and secrets were preserved."
  echo "To remove runtime data explicitly, run: ./uninstall.sh --purge-data"
fi
echo "ServerPulse application environment removed."
