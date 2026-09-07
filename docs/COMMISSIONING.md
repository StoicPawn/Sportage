# Sportage production commissioning

Commissioning is the final read-only gate before Sportage is allowed to create a new live execution plan. It does not place, cancel or modify bets and it requires `SPORTAGE_LIVE_EXECUTION=false` while it runs.

A successful run creates a short-lived commissioning certificate in the same SQLite database used by the execution coordinator. By default the certificate is valid for 30 minutes.

## Why it is a hard gate

Venue certification proves that one API/session works. Account-funds checks prove current balance. Canary limits bound order size. Commissioning combines those pieces and also proves that the two independent exchange feeds currently map at least one executable two-outcome market to the same canonical event/market/outcomes.

A new `live=True` plan is rejected when the latest commissioning run is missing, failed, expired or belongs to a different safety-configuration fingerprint.

Commissioning is checked only when new risk is opened. It is deliberately **not** checked before hedge, rescue, cancellation or reconciliation, so an expired certificate cannot block reduction of an exposure already opened.

## Required local state

Before remote API checks run, Sportage requires:

- `SPORTAGE_LIVE_EXECUTION=false`;
- `SPORTAGE_CANARY_MODE=true`;
- `SPORTAGE_REQUIRE_LIVE_CERTIFICATION=true`;
- `SPORTAGE_REQUIRE_ACCOUNT_FUNDS=true`;
- `BETFLAG_ENVIRONMENT=production`;
- no global execution halt;
- no active live execution;
- no event/market execution lock.

If any local prerequisite is red, remote checks are skipped.

## Remote read-only checks

A normal commissioning run verifies:

1. current successful production certification for Betfair and BetFlag (refreshed by default);
2. official account balances for both exchanges;
3. both accounts are flat (no material exposure or locked balance);
4. each account has at least the canary max-order liability plus the configured rescue balance buffer;
5. direct Betfair and BetFlag market data can be fetched;
6. at least one current two-outcome market has native market/selection ids, positive depth and the same canonical event/market/outcome set on both exchanges.

No order placement endpoint is called.

## Run

Keep the master switch off and set BetFlag to production:

```text
SPORTAGE_LIVE_EXECUTION=false
BETFLAG_ENVIRONMENT=production
SPORTAGE_REQUIRE_COMMISSIONING=true
```

Then:

```bash
sportage-commission run
```

To reuse still-valid venue certifications instead of refreshing them:

```bash
sportage-commission run --use-existing-certifications
```

Inspect the latest result:

```bash
sportage-commission status
```

Only a fully green result creates a valid certificate.

## Safety configuration fingerprint

The certificate binds to a SHA-256 fingerprint of safety-relevant non-secret configuration:

- BetFlag environment;
- canary mode and canary policy file contents;
- live-certification/account-funds gates;
- rescue balance buffer;
- execution policy file contents;
- bookmaker/exchange cost configuration file contents.

Credential/token values are never stored in the fingerprint. `SPORTAGE_LIVE_EXECUTION` is intentionally excluded: commissioning must happen with it `false`, and switching it to `true` afterward must not invalidate the certificate.

If a risk configuration file or relevant environment setting changes, the certificate becomes invalid immediately and commissioning must be rerun.

## Initial real-money sequence

The intended first-production sequence is:

```text
1. SPORTAGE_LIVE_EXECUTION=false
2. venue credentials configured
3. BETFLAG_ENVIRONMENT=production
4. sportage-commission run
5. sportage-commission status   -> all green
6. sportage-canary status       -> limits understood / no active run
7. SPORTAGE_LIVE_EXECUTION=true
8. prepare ONE canary-sized live plan
9. execute/reconcile/settle it completely
10. SPORTAGE_LIVE_EXECUTION=false
11. review telemetry before any second live attempt
```

A successful first trade is not a reason to increase canary limits. Limits should move only after repeated clean evidence from mapping, fills, reconciliation, rescue behavior and settlement.
