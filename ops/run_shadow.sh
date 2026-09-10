#!/usr/bin/env bash
set -uo pipefail
ROOT=/home/stoicpawn/projects/Sportage
cd "$ROOT"

while true; do
  if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
  fi
  if TOKEN="$(.venv/bin/python -m arbengine.betfair_auth 2>/tmp/sportage-betfair-auth.err)"; then
    export BETFAIR_SESSION_TOKEN="$TOKEN"
    unset TOKEN
    break
  fi
  printf '%s Betfair monitoring idle: credentials/certificate not ready; retrying in 5m.\n' "$(date --iso-8601=seconds)"
  sleep 300
done

export SPORTAGE_LIVE_EXECUTION=false
exec .venv/bin/sportage shadow \
  --provider adaptive \
  --db data/arbitrage.sqlite3 \
  --min-net-roi 0.015 \
  --costs config/costs.example.json
