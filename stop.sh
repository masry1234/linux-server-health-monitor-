cat > start.sh <<'EOF'
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

export SERVERPULSE_HOST="${SERVERPULSE_HOST:-127.0.0.1}"
export SERVERPULSE_PORT="${SERVERPULSE_PORT:-8000}"

echo "ServerPulse starting..."
echo "Bind address: $SERVERPULSE_HOST:$SERVERPULSE_PORT"

exec "$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/live_dashboard.py"
EOF

chmod +x start.sh