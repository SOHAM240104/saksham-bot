#!/usr/bin/env bash
# Launch the Saksham UAT Console (Streamlit).
# Prefer: ./scripts/server.sh demo   (or ./scripts/server.sh all)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$ROOT/scripts/server.sh" demo
