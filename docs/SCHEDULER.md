# Adaptive provider scheduler

Sportage v0.6 decouples the **scanner tick** from the **upstream API call cadence**.
The shadow loop can run every few seconds while each source is fetched only when its own schedule and budget allow it.

## Modes

The scheduler derives one global urgency mode from the most recent usable market cache:

- `base`: no event is close and no market is close to arbitrage;
- `near_event`: at least one cached event starts inside `near_event_window_seconds`;
- `hot`: the best complete market has an implied-probability sum within `hot_implied_gap` of 1.0 (including actual arbitrage below 1.0).

Every source has separate intervals for the three modes. A direct source can therefore run much faster than a credit-limited aggregator.

## Budgets

Each source can define:

- `daily_call_limit`;
- `monthly_unit_limit`;
- `units_per_call`;
- maximum cache age;
- error retry interval.

Usage is persisted in the same SQLite database in `scheduler_source_state`, so restarting Sportage does not reset the configured daily/monthly budget counters.

The example configuration is `config/provider_scheduler.example.json`. Its numbers are deliberately conservative placeholders for a free-first MVP and are fully configurable; provider pricing/limits should be updated when plans change.

## Cache semantics

A cached quote keeps the original provider `observed_at`. Returning it on a later scheduler tick does **not** make it fresh. The arbitrage engine still applies `max_quote_age_seconds` independently, while the scheduler may retain older values briefly to decide whether an event is near or a market is becoming hot.

## Commands

Run the adaptive collector:

```bash
sportage shadow --provider adaptive
```

The default scheduler file can be changed with `SPORTAGE_SCHEDULER_CONFIG` or `--scheduler-config`.

Inspect persistent provider usage:

```bash
sportage scheduler-status
```

Inspect latest source/coverage health:

```bash
sportage data-health
```

## Safety property

Budget exhaustion skips upstream calls; it never fabricates a refreshed timestamp. If a cached quote ages beyond the engine freshness threshold, it cannot produce an executable arbitrage even if it remains in the scheduler cache.
