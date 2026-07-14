#!/usr/bin/env bash
# Convenience launcher for the Hermes ↔ OpenCode gateway.
# Usage:  ./run.sh            # foreground
#         ./run.sh --reload    # dev mode with auto-reload
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -d .venv ]]; then
  echo ">> creating virtualenv (.venv)"
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo ">> installing dependencies"
pip install -q --upgrade pip
pip install -q -r requirements.txt

export GATEWAY_HOST="${GATEWAY_HOST:-127.0.0.1}"
export GATEWAY_PORT="${GATEWAY_PORT:-8787}"

echo ">> starting gateway on http://${GATEWAY_HOST}:${GATEWAY_PORT}"
if [[ "${1:-}" == "--reload" ]]; then
  exec uvicorn main:app --host "$GATEWAY_HOST" --port "$GATEWAY_PORT" --reload
else
  exec python main.py
fi
