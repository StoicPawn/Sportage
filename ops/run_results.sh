#!/usr/bin/env bash
set -uo pipefail
ROOT=/home/stoicpawn/projects/Sportage
cd "$ROOT"

# systemd EnvironmentFile supplies credentials; never source the file as shell code.
if ! TOKEN="$(.venv/bin/python -m arbengine.betfair_auth 2>/tmp/sportage-betfair-results-auth.err)"; then
  printf '%s Result archive skipped: Betfair credentials/certificate not ready.\n' "$(date --iso-8601=seconds)"
  exit 0
fi
export BETFAIR_SESSION_TOKEN="$TOKEN"
unset TOKEN
export SPORTAGE_LIVE_EXECUTION=false
exec .venv/bin/python -m arbengine.result_cli --db data/arbitrage.sqlite3 --max-age-hours 72 --limit-events 100
