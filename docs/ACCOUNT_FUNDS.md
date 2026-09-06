# Live account funds and rescue reserve

Sportage V0.13 treats venue funding as a hard execution prerequisite, not as a reporting metric.

## Official account sources

### Betfair Exchange

Sportage calls the official Accounts API JSON-RPC endpoint with `AccountAPING/v1.0/getAccountFunds` and records:

- `availableToBetBalance` -> available balance
- `exposure`
- `exposureLimit`
- `retainedCommission`

Italian Exchange accounts use the same Accounts API endpoint after authentication through the Italian login flow.

### BetFlag Exchange

Sportage calls the official authenticated `GET /account/balance` endpoint and converts cent-based values to EUR:

- `FreeBalance` -> available balance
- `Balance` -> total balance
- `LockedBalance` -> locked balance / existing exposure

Snapshots are written to the same SQLite database used by execution.

## Three funding barriers

### 1. `account-status`

```bash
sportage-exec account-status --operator all
```

Refreshes and persists current official balances without placing any bet.

### 2. `prepare --live`

Before a live execution plan is created, Sportage:

1. requires valid production venue certifications;
2. requires a different certified production rescue venue;
3. refreshes official balances;
4. subtracts capital required by already planned automatic legs;
5. estimates the rescue stake needed to rebuild the opportunity's guaranteed payout;
6. stresses rescue odds by `max_rescue_slippage_bps`;
7. verifies current displayed exchange depth;
8. requires an additional free-balance buffer (`SPORTAGE_RESCUE_BALANCE_BUFFER_PCT`, default 10%).

If no independent venue has enough free balance and depth, `prepare --live` fails before any manual primary is placed.

### 3. Immediately before every live API order

The automatic connector refreshes account funds again just before `place_order(live=True)`.

For a BACK/PUNTA order the required funds are the stake.
For a LAY/BANCA order the required funds are the liability:

```text
liability = stake * (odds - 1)
```

If current free balance is insufficient, the order is rejected locally, that venue's circuit breaker opens, and the coordinator can route to the independent rescue venue when exposure already exists.

Cancellation and reconciliation calls are never blocked by the funding gate: risk-reduction actions must remain available even when account balance or certification state is unhealthy.

## Production-only live routing

BetFlag `staging` is deliberately test-only. A successful staging certification can validate integration but cannot unlock `prepare --live`, cannot be selected as an independent real-money rescue venue, and cannot pass the connector-level live certification gate.

Set and certify `BETFLAG_ENVIRONMENT=production` before real-money use.

## Two-outcome live restriction

Automatic live execution currently accepts only two-outcome markets. A 1X2/three-outcome market can require two rescue orders after one sequential hedge failure; Sportage does not claim those two independent orders are atomic, so it rejects such a live plan before exposure is opened. Scanning, analytics and backtesting of 1X2 remain available.

## Environment controls

```text
SPORTAGE_REQUIRE_LIVE_CERTIFICATION=true
SPORTAGE_REQUIRE_ACCOUNT_FUNDS=true
SPORTAGE_RESCUE_BALANCE_BUFFER_PCT=0.10
SPORTAGE_EXECUTION_VENUE_COOLDOWN_SECONDS=60
SPORTAGE_LIVE_EXECUTION=false
```

The first two gates are enabled by default. Disabling either is a development/testing override, not a production configuration.
