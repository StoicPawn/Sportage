# Sportage execution protocol

Sportage cannot create a true distributed transaction across independent bookmakers. The execution layer therefore uses a fail-closed state machine that minimizes and audits orphan-leg risk instead of pretending it can eliminate it.

## Safety invariants

1. Never submit a new live order when the global execution halt is active.
2. One event+market can have only one active execution lock.
3. A timeout is `UNKNOWN`, never automatically `REJECTED`.
4. An `UNKNOWN` placement is reconciled by persistent order reference before any retry.
5. Exchange hedges and rescue orders use strict price limits and full-size FILL_OR_KILL where supported.
6. A rescue is allowed only inside configured maximum loss and slippage limits.
7. Unresolved exposure triggers `EMERGENCY` and a global halt.
8. Emergency locks survive process restarts and require explicit manual reconciliation.
9. Manual-only retail connectors never pretend to have placed a bet.

## Normal retail + exchange path

```text
PREPARE
  -> validate net ROI / quote age / rescue coverage
  -> acquire event-market lock
  -> prepare manual retail primary
  -> prepare automatic exchange hedge

WAITING_MANUAL
  -> user places primary on the retail bookmaker
  -> confirm exact accepted stake and odds

EXECUTING
  -> exchange preflight refresh
  -> verify market OPEN, price, depth and marketVersion
  -> place FILL_OR_KILL hedge with customerRef + customerOrderRef

FULLY_HEDGED
  -> both legs confirmed matched
  -> release event-market lock
```

## Unknown order path

```text
placeOrders raises timeout/network error
  -> DO NOT RESUBMIT
  -> listCurrentOrders(customerOrderRef)
  -> known matched/rejected state: continue from actual state
  -> still UNKNOWN: EMERGENCY + GLOBAL HALT
```

The persistent `customerOrderRef` is separate from Betfair's request-level `customerRef`. The first identifies the order for reconciliation; the second protects short-window duplicate submissions.

## Orphan / rescue path

When one leg is matched and the intended hedge is rejected, cancelled, partially matched, or otherwise not fully filled:

1. Cancel any known unmatched remainder.
2. Recompute actual exposure from matched stake and average matched price.
3. Read fresh quotes only.
4. Find an automatic rescue venue with provider-native market and selection IDs.
5. Reject candidates outside `max_rescue_slippage_bps` or available depth.
6. Calculate the rescue stake required to equalize returns against the already matched exposure.
7. Reject the rescue if projected loss exceeds `max_rescue_loss`.
8. Submit rescue as full-size FILL_OR_KILL.
9. If rescue is fully matched, mark `RESCUED`; otherwise enter `EMERGENCY` and halt all new executions.

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

These are safety limits, not profitability assumptions. They should be calibrated from real shadow/execution telemetry.

## CLI workflow

Prepare the highest-ranked stored opportunity in a scan:

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

## Current automatic venue

Betfair Exchange is the current official automatic execution connector. Retail Tier 1/2 connectors remain `MANUAL_REQUIRED` unless an official supported placement API is configured. The architecture can add future automatic connectors without changing the coordinator state machine.

## Remaining unavoidable risk

Even with this protocol, independent venues do not provide a cross-bookmaker atomic commit. Market suspension, account restrictions, rule differences, late rejection, internet failure and operator-side incidents can still create exposure. The coordinator's purpose is to bound, detect and react to that exposure, not to claim it is impossible.
