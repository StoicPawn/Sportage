#!/usr/bin/env bash
set -uo pipefail
ROOT=/home/stoicpawn/projects/Sportage
cd "$ROOT"

# EnvironmentFile=.env is loaded by systemd immediately before each process start.
# Do not shell-source .env here: passwords may legally contain shell metacharacters.
if ! TOKEN="$(.venv/bin/python -m arbengine.betfair_auth 2>/tmp/sportage-betfair-auth.err)"; then
  printf '%s Betfair monitoring idle: credentials/certificate not ready; systemd will retry.\n' "$(date --iso-8601=seconds)"
  exit 0
fi
export BETFAIR_SESSION_TOKEN="$TOKEN"
unset TOKEN

export SPORTAGE_LIVE_EXECUTION=false
exec .venv/bin/sportage shadow \
  --provider adaptive \
  --db data/arbitrage.sqlite3 \
  --min-net-roi 0.015 \
  --costs config/costs.example.json
