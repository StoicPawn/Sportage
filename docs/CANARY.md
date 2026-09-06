# Sportage live canary envelope

Canary mode is an absolute safety envelope around live execution. It is independent from arbitrage ROI, bankroll sizing, venue certification, account balances and the global execution halt.

It does **not** enable live betting. `SPORTAGE_LIVE_EXECUTION=false` remains the master kill-switch.

## Default initial limits

`config/canary_policy.example.json` starts deliberately small:

- max stake per planned leg: **€5.00**
- max automatic order liability: **€7.50**
- max total execution liability/capital: **€12.00**
- max live executions prepared per UTC day: **3**
- max prepared live capital per UTC day: **€36.00**
- max simultaneously active live executions: **1**
- max automatic API order attempts per UTC day: **6**
- max cumulative authorized API liability per UTC day: **€30.00**

The default execution policy also caps rescue loss at **€1.00** during initial validation.

## Where the gate is enforced

### Live plan creation

`ExecutionStore.create_run(... live=True)` calls the canary guard before persisting a live execution. This sits below the CLI/coordinator path, so an oversized plan cannot be created simply by calling the coordinator from another module.

The guard reads every planned `BetOrder`, computes BACK stake or LAY liability, and applies per-leg, per-execution, daily and concurrent-run limits.

### Automatic live API placement

Immediately before an official connector sends a real live order, Sportage has already checked:

1. venue certification;
2. production environment eligibility;
3. current official account funds;
4. the canary order-attempt/liability budget.

The canary attempt is persisted before the underlying API call. Network failures and rejected/partial orders still count against the daily attempt budget; this prevents repeated failures from creating an uncontrolled retry loop.

A duplicate `customer_order_ref` is rejected by the canary ledger.

### Risk-reduction actions

Cancellation and reconciliation are not blocked by canary exhaustion. A safety system must not prevent an already-open exposure from being reduced.

## Monitoring

```bash
sportage-canary status
```

shows today's live preparations, active executions, prepared capital, API attempts and authorized liability versus their configured limits.

## Configuration

```text
SPORTAGE_CANARY_MODE=true
SPORTAGE_CANARY_POLICY=config/canary_policy.example.json
SPORTAGE_LIVE_EXECUTION=false
```

`SPORTAGE_CANARY_MODE=false` is an explicit development/advanced override. It should not be used for the first real-money validation cycle.

## Rollout rule

Do not increase canary limits merely because one trade succeeds. Limits should be raised only after execution telemetry shows repeated clean cycles: correct market mapping, accepted/matched state reconciliation, no orphan exposure, rescue routing behaving as designed and balances reconciling after settlement.
