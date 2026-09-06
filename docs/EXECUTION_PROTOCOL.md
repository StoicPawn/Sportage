# Sportage execution protocol

Sportage cannot create a true distributed transaction across independent bookmakers. The execution layer therefore uses a fail-closed state machine that minimizes and audits orphan-leg risk instead of pretending it can eliminate it.

## Safety invariants

1. Never submit a new live order when the global execution halt is active.
2. One event+market can have only one active execution lock.
3. A timeout is `UNKNOWN`, never automatically `REJECTED`.
4. An `UNKNOWN` placement is reconciled before any retry; if it cannot be resolved, execution halts.
5. Exchange hedges and rescue orders use strict price limits and full-size FILL_OR_KILL where supported.
6. BetFlag has no documented native FILL_OR_KILL; Sportage cancels the unmatched remainder immediately and treats any partial fill as exposure requiring rescue.
7. A failed automatic venue is circuit-broken before rescue routing, so it cannot immediately rescue its own failed hedge.
8. A live plan is armed only if every automatic venue has a current successful certification for the exact environment.
9. Before a live plan is created, each automatic venue must have a different certified automatic venue able to cover every outcome in the same market with fresh native execution references.
10. The automatic connector factory re-checks certification immediately before every live placement, so CLI bypass cannot silently disable the gate.
11. Cancellation and reconciliation remain available even if certification expires; emergency risk reduction is never blocked by the readiness gate.
12. A rescue is allowed only inside configured maximum loss and slippage limits.
13. Unresolved exposure triggers `EMERGENCY` and a global halt.
14. Emergency locks survive process restarts and require explicit manual reconciliation.
15. Manual-only retail connectors never pretend to have placed a bet.

## Venue certification gate

Certification is read-only: it never calls the placement endpoint.

For **Betfair** it verifies:

- `BETFAIR_APP_KEY` and `BETFAIR_SESSION_TOKEN` are configured;
- direct API-NG market data returns executable native `marketId` / `selectionId` references and depth;
- the authenticated `listCurrentOrders` endpoint is readable;
- a fresh market/selection preflight validates current price, depth and market version.

For **BetFlag** it verifies:

- API key configuration plus session token or username/password;
- the exact configured environment (`staging` or `production`);
- official direct market data returns native market/selection references and depth;
- authenticated `GET /offers/all/{market}` is readable, proving the session can access the reconciliation endpoint;
- a fresh execution preflight validates current price, depth and market version.

Successful certification is persisted in SQLite with an expiry timestamp. Staging and production are different certifications: a successful BetFlag staging certification does **not** unlock production.

```bash
sportage-exec venue-certify --operator betfair
sportage-exec venue-certify --operator betflag --environment staging
sportage-exec venue-certify --operator betflag --environment production
sportage-exec venue-status
```

The default certification TTL is 24 hours. Re-run certification whenever credentials, API keys, environment or account configuration changes.

## Normal retail + exchange path

```text
CERTIFY VENUES
  -> authenticated read-only probes
  -> direct market-data/native-id validation
  -> execution preflight
  -> durable certification PASS

PREPARE --live
  -> validate net ROI / quote age
  -> require certification for automatic legs
  -> require independent certified rescue map
  -> acquire event-market lock
  -> prepare manual retail primary
  -> prepare automatic exchange hedge

WAITING_MANUAL
  -> user places primary on the retail bookmaker
  -> confirm exact accepted stake and odds

EXECUTING
  -> exchange preflight refresh
  -> certification checked again immediately before live placement
  -> verify market, price, depth and native market version
  -> submit strict automatic hedge

FULLY_HEDGED
  -> both legs confirmed matched
  -> release event-market lock
```

## Unknown order path

```text
placement raises timeout/network error
  -> DO NOT RESUBMIT BLINDLY
  -> reconcile with the official operator order API where possible
  -> known matched/rejected state: continue from actual state
  -> still UNKNOWN: EMERGENCY + GLOBAL HALT
```

Betfair has `customerOrderRef`, which gives strong direct reconciliation. BetFlag's public API does not document an equivalent client idempotency key; if Sportage cannot uniquely reconcile a BetFlag placement after a transport failure, it deliberately remains `UNKNOWN` and halts rather than risk a duplicate bet.

## Orphan / independent rescue path

When one leg is matched and the intended hedge is rejected, cancelled, partially matched, or otherwise not fully filled:

1. Mark the failed automatic venue unhealthy for `SPORTAGE_EXECUTION_VENUE_COOLDOWN_SECONDS` (default 60s).
2. Cancel any known unmatched remainder.
3. Recompute actual exposure from matched stake and average matched price.
4. Read fresh quotes only.
5. Exclude the venue that just failed from automatic rescue candidates while its circuit is open.
6. Find another automatic venue with provider-native market and selection IDs.
7. Reject candidates outside `max_rescue_slippage_bps` or available depth.
8. Calculate the rescue stake required to equalize returns against the already matched exposure.
9. Reject the rescue if projected loss exceeds `max_rescue_loss`.
10. Submit the rescue with the strictest immediate-fill behavior supported by that API.
11. If rescue is fully matched, mark `RESCUED`; otherwise enter `EMERGENCY` and halt all new executions.

With the current Italy profile this gives a genuine independent path:

```text
retail primary
     |
     v
Betfair hedge ---- failure ----> Betfair circuit OPEN
                                  |
                                  v
                           BetFlag rescue

and symmetrically:

BetFlag hedge ---- failure ----> BetFlag circuit OPEN
                                  |
                                  v
                           Betfair rescue
```

The circuit breaker is intentionally in-memory and short-lived. Durable unresolved risk is handled separately by the execution database and global halt, which survive process restarts.

## Configuration

`config/execution_policy.example.json`:

```json
{
  "min_net_roi": "0.015",
  "max_quote_age_seconds": 10,
  "max_rescue_loss": "5.00",
  "max_rescue_slippage_bps": "100",
  "require_rescue_venue": true,
  "require_full_fill_exchange": true,
  "max_reconcile_attempts": 2
}
```

Environment:

```text
SPORTAGE_EXECUTION_VENUE_COOLDOWN_SECONDS=60
SPORTAGE_REQUIRE_LIVE_CERTIFICATION=true
SPORTAGE_LIVE_EXECUTION=false
```

`SPORTAGE_REQUIRE_LIVE_CERTIFICATION=true` is the default. Disabling it is an explicit development override and should not be used for production execution.

These are safety limits, not profitability assumptions. They should be calibrated from real shadow/execution telemetry.

## CLI workflow

First certify the automatic venues:

```bash
sportage-exec venue-certify --operator betfair
sportage-exec venue-certify --operator betflag --environment production
sportage-exec venue-status
```

Prepare the highest-ranked stored opportunity in a scan. For `--live`, Sportage prints the certified failover map and refuses to create the plan if independent rescue is unavailable:

```bash
sportage-exec prepare --scan-id 123 --live
```

For a manual retail primary, place the bet yourself and confirm only after the bookmaker shows it as accepted:

```bash
sportage-exec confirm \
  --execution-id exe_... \
  --leg-id L1 \
  --accepted \
  --matched-stake 100 \
  --average-odds 2.10 \
  --bet-id YOUR_BOOKMAKER_REFERENCE
```

Refresh market data and execute the supported automatic hedge/rescue:

```bash
sportage-exec resume --execution-id exe_... --live
```

Inspect the complete state and audit trail:

```bash
sportage-exec status --execution-id exe_...
```

Manually halt all new execution:

```bash
sportage-exec halt --reason "operator account issue"
```

An emergency must not be cleared merely because the process was restarted. After independently verifying/neutralizing the exposure:

```bash
sportage-exec resolve-emergency \
  --execution-id exe_... \
  --confirm-flat \
  --clear-global-halt
```

## Current automatic venues

The Italy profile has two verified official automatic execution connectors:

- **Betfair Exchange API-NG** — native FILL_OR_KILL, market version, client order references and reconciliation.
- **BetFlag Exchange API 2.0.7** — official market data, login/session, placement, cancellation and user-order reconciliation; immediate cancellation is used for any unmatched remainder because native FILL_OR_KILL is not publicly documented.

All other current Tier 1/2 retail connectors remain `MANUAL_REQUIRED` unless a verified official transactional API is configured.

## Remaining unavoidable risk

Even with this protocol, independent venues do not provide a cross-bookmaker atomic commit. Market suspension, account restrictions, rule differences, late rejection, internet failure and operator-side incidents can still create exposure. The coordinator's purpose is to bound, detect and react to that exposure, not to claim it is impossible.
