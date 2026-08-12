#!/usr/bin/env bash
# Local development server: ./run.sh  → http://localhost:8000
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
fi
exec .venv/bin/uvicorn app.main:app --reload --port "${PORT:-8000}"
