#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_MODE=false
if [[ "${1:-}" == "--service" ]]; then
  SERVICE_MODE=true
elif [[ $# -gt 0 ]]; then
  echo "Usage: ./install.sh [--service]" >&2
  exit 2
fi

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "ServerPulse installation is supported on Linux/Ubuntu (WSL2 is suitable for development)." >&2
  exit 1
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python 3 was not found. Install it first (Ubuntu: sudo apt install python3 python3-venv)." >&2
  exit 1
fi

"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("ServerPulse requires Python 3.10 or newer.")
print(f"Using Python {sys.version.split()[0]}")
PY

cd "$ROOT_DIR"
if ! "$PYTHON_BIN" -m venv .venv 2>/dev/null; then
  echo "Could not create the virtual environment. On Ubuntu run:" >&2
  echo "  sudo apt update && sudo apt install -y python3-venv" >&2
  exit 1
fi

.venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt

mkdir -p logs
if [[ ! -f config.json ]]; then
  cp config.example.json config.json
fi
if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
fi

.venv/bin/python -m py_compile health_monitor.py live_dashboard.py security_monitor.py notification_service.py
echo
echo "ServerPulse installation completed."
echo "Start dashboard: ./start.sh"
echo "Open: http://127.0.0.1:8000"
echo "Edit .env to configure optional notifications."

if [[ "$SERVICE_MODE" == true ]]; then
  if ! command -v systemctl >/dev/null 2>&1 || ! systemctl --user show-environment >/dev/null 2>&1; then
    echo "systemd user manager is unavailable; start manually with ./start.sh." >&2
    exit 1
  fi
  SERVICE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  mkdir -p "$SERVICE_DIR"
  cat > "$SERVICE_DIR/serverpulse.service" <<EOF
[Unit]
Description=ServerPulse Linux Monitoring Dashboard
After=network.target

[Service]
Type=simple
WorkingDirectory=$ROOT_DIR
EnvironmentFile=-$ROOT_DIR/.env
Environment=SERVERPULSE_HOST=127.0.0.1
Environment=SERVERPULSE_PORT=8000
ExecStart=$ROOT_DIR/.venv/bin/python $ROOT_DIR/live_dashboard.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now serverpulse.service
  echo "ServerPulse user service enabled and started."
  echo "Status: systemctl --user status serverpulse.service"
fi
