#!/usr/bin/env bash
# Start the Market Scanner server.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements.txt
fi

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
echo "Market Scanner -> http://${HOST}:${PORT}"
exec .venv/bin/uvicorn backend.main:app --host "$HOST" --port "$PORT" "$@"
