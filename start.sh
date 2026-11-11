#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [[ ! -x "$ROOT_DIR/.venv/bin/python" ]]; then
    echo "ServerPulse is not installed. Run: ./install.sh" >&2
    exit 1
fi

if [[ -f "$ROOT_DIR/.env" ]]; then
    set -a
    source "$ROOT_DIR/.env"
    set +a
fi

# WSL2: bind to its network interface for reliable Windows access.
if grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null \
    && [[ -z "${SERVERPULSE_HOST:-}" || "$SERVERPULSE_HOST" == "127.0.0.1" ]]; then
    WSL_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
    if [[ -n "$WSL_IP" ]]; then
        SERVERPULSE_HOST="$WSL_IP"
    fi
fi

export SERVERPULSE_HOST="${SERVERPULSE_HOST:-127.0.0.1}"
export SERVERPULSE_PORT="${SERVERPULSE_PORT:-8000}"

DASHBOARD_URL="http://${SERVERPULSE_HOST}:${SERVERPULSE_PORT}"

echo "Starting ServerPulse..."
echo "Bind address: ${SERVERPULSE_HOST}:${SERVERPULSE_PORT}"

"$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/live_dashboard.py" &
SERVER_PID=$!

cleanup() {
    echo
    echo "Stopping ServerPulse..."
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
    echo "Dashboard stopped."
}

trap cleanup EXIT INT TERM

# Wait until the HTTP API responds.
READY=false
for attempt in $(seq 1 30); do
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "ServerPulse process exited unexpectedly." >&2
        exit 1
    fi

    if "$ROOT_DIR/.venv/bin/python" -c \
        "import urllib.request; urllib.request.urlopen('$DASHBOARD_URL/api/health', timeout=2)" \
        >/dev/null 2>&1; then
        READY=true
        break
    fi

    sleep 1
done

if [[ "$READY" != true ]]; then
    echo "Dashboard did not become ready within 30 seconds." >&2
    exit 1
fi

echo
echo "ServerPulse is ready."
echo "Dashboard: $DASHBOARD_URL"
echo "Opening Windows default browser..."

if command -v cmd.exe >/dev/null 2>&1; then
    cmd.exe /c start "" "$DASHBOARD_URL" || \
        echo "Automatic browser launch failed. Open: $DASHBOARD_URL"
elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$DASHBOARD_URL" >/dev/null 2>&1 &
else
    echo "Open this URL manually: $DASHBOARD_URL"
fi

echo
echo "Press CTRL+C to stop ServerPulse."

wait "$SERVER_PID"
