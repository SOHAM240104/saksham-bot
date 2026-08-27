#!/usr/bin/env bash
# Start Saksham Bot locally.
#
# Usage:
#   ./scripts/server.sh          # FastAPI only (default)
#   ./scripts/server.sh api      # FastAPI only
#   ./scripts/server.sh demo     # Streamlit UAT console only
#   ./scripts/server.sh all      # FastAPI + Streamlit together
#
# Env overrides:
#   HOST / PORT / RELOAD         — FastAPI (defaults: 127.0.0.1 / 8000 / 1)
#   DEMO_PORT                    — Streamlit (default: 8501)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

MODE="${1:-api}"

if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
else
  echo "Warning: $ROOT/.env not found — set DATABASE_URL, JWT_SECRET_KEY, etc. yourself." >&2
fi

if [[ -d "$ROOT/.venv" ]]; then
  # shellcheck disable=SC1091
  source "$ROOT/.venv/bin/activate"
fi

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
RELOAD="${RELOAD:-1}"
DEMO_PORT="${DEMO_PORT:-8501}"

start_api() {
  echo "Starting Saksham Bot API at http://${HOST}:${PORT}"
  local args=(app.main:app --host "$HOST" --port "$PORT")
  if [[ "$RELOAD" == "1" ]]; then
    args+=(--reload)
  fi
  uvicorn "${args[@]}"
}

start_demo() {
  echo "Starting Saksham UAT Console at http://127.0.0.1:${DEMO_PORT}"
  echo "(Expects FastAPI at ${HOST}:${PORT} — start with: ./scripts/server.sh api)"
  streamlit run demo/uat_console.py \
    --server.port "$DEMO_PORT" \
    --server.headless true \
    --browser.gatherUsageStats false
}

cleanup() {
  if [[ -n "${API_PID:-}" ]] && kill -0 "$API_PID" 2>/dev/null; then
    echo "Stopping API (pid $API_PID)…"
    kill "$API_PID" 2>/dev/null || true
    wait "$API_PID" 2>/dev/null || true
  fi
}

case "$MODE" in
  api|"")
    start_api
    ;;
  demo)
    start_demo
    ;;
  all)
    trap cleanup EXIT INT TERM
    start_api &
    API_PID=$!
    sleep 2
    if ! kill -0 "$API_PID" 2>/dev/null; then
      echo "API failed to start." >&2
      exit 1
    fi
    echo "API running (pid $API_PID). Starting Streamlit…"
    start_demo
    ;;
  *)
    echo "Usage: $0 [api|demo|all]" >&2
    exit 1
    ;;
esac
