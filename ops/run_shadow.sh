#!/usr/bin/env bash
set -uo pipefail
ROOT=/home/stoicpawn/projects/Sportage
cd "$ROOT"

# EnvironmentFile=.env is loaded by systemd immediately before each process start.
# Do not shell-source .env here: passwords may legally contain shell metacharacters.
# Betfair is an optional direct verification feed, not a prerequisite for discovery.
if [ -n "${BETFAIR_APP_KEY:-}" ]; then
  if TOKEN="$(.venv/bin/python -m arbengine.betfair_auth 2>/tmp/sportage-betfair-auth.err)"; then
    export BETFAIR_SESSION_TOKEN="$TOKEN"
    unset TOKEN
    printf '%s Betfair direct feed authenticated; aggregators remain enabled.\n' "$(date --iso-8601=seconds)"
  else
    printf '%s Betfair direct feed unavailable; continuing with configured aggregators/other sources.\n' "$(date --iso-8601=seconds)"
  fi
fi

if [ -z "${ODDS_API_IO_KEY:-}" ] && [ -z "${THE_ODDS_API_KEY:-}" ] && \
   [ -z "${BETFLAG_API_KEY:-}" ] && [ -z "${BETFAIR_SESSION_TOKEN:-}" ]; then
  printf '%s No market-data credential configured; systemd will retry.\n' "$(date --iso-8601=seconds)"
  exit 0
fi

export SPORTAGE_LIVE_EXECUTION=false
exec .venv/bin/sportage shadow \
  --provider adaptive \
  --db data/arbitrage.sqlite3 \
  --min-net-roi 0.015 \
  --costs config/costs.example.json
