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
  # shellcheck disable=SC1091
  source "$ROOT_DIR/.env"
  set +a
fi

# WSL2 localhost forwarding may be unavailable. Bind only to the WSL interface
# when no explicit host was configured; regular Linux remains loopback-only.
if grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null \
  && [[ -z "${SERVERPULSE_HOST:-}" || "$SERVERPULSE_HOST" == "127.0.0.1" ]]; then
  WSL_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  if [[ -n "$WSL_IP" ]]; then
    export SERVERPULSE_HOST="$WSL_IP"
  fi
fi
export SERVERPULSE_HOST="${SERVERPULSE_HOST:-127.0.0.1}"
export SERVERPULSE_PORT="${SERVERPULSE_PORT:-8000}"

printf 'ServerPulse binding to %s:%s\n' "$SERVERPULSE_HOST" "$SERVERPULSE_PORT"
exec "$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/live_dashboard.py"
