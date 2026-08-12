#!/usr/bin/env bash
# Run HouseBudget on this machine.
#
#   ./run.sh                 → http://localhost:8000  (this computer only)
#   HOST=0.0.0.0 ./run.sh    → also reachable from phones on your home wi-fi
#   HB_DATA_DIR=/path ./run.sh  → keep the data somewhere specific
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "Setting up (one time only)…"
  python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements.txt
fi

DATA_DIR="$(.venv/bin/python -c 'from app import config; print(config.DATA_DIR)')"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

echo
echo "  HouseBudget"
echo "  ───────────"
echo "  Open:   http://localhost:${PORT}"
if [ "$HOST" = "0.0.0.0" ]; then
  IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  [ -n "${IP:-}" ] && echo "  Phone:  http://${IP}:${PORT}   (same wi-fi, plain HTTP)"
fi
echo "  Data:   ${DATA_DIR}"
echo "          ↑ your database and statements. Back this up; it is not in git."
echo

exec .venv/bin/uvicorn app.main:app --host "$HOST" --port "$PORT" --reload
