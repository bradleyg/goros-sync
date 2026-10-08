#!/usr/bin/env bash
# Start the Garmin → COROS sync web app.
#   HOST / PORT     bind address (default 127.0.0.1:8484)
#   APP_PASSWORD    optional: require HTTP basic auth for the UI
#   SYNC_DATA_DIR   optional: where the DB, credentials and tokens live (default ./data)
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt

exec .venv/bin/uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8484}"
