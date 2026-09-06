# Sportage data health and coverage

Sportage v0.5 treats market-data quality as a first-class input to arbitrage decisions.
A scan can be mathematically correct and still be operationally useless if one or more
bookmakers are missing, stale or temporarily unavailable.

## Concurrent source orchestration

`UnifiedOperatorProvider` fetches configured upstreams concurrently:

- Betfair Exchange API-NG when credentials are configured;
- The Odds API when `THE_ODDS_API_KEY` is configured;
- Odds-API.io when `ODDS_API_IO_KEY` is configured.

A failure in one source does not discard quotes returned by healthy sources. The scan
continues when at least one source completed successfully. If every configured source
fails, the scan is marked as an error rather than as a valid zero-opportunity scan.

`SPORTAGE_PROVIDER_WORKERS` controls the maximum parallel source requests.

## Source health ledger

Every unified shadow scan persists one `source_health` row per configured source with:

- source name and status (`ok` / `error`);
- start/end timestamp and request duration;
- raw quote count;
- normalized quote count accepted into the Sportage operator universe;
- number of covered operators;
- error type/message when the upstream fails.

This distinguishes a genuine no-arbitrage observation from an upstream outage.

## Operator coverage ledger

For every operator present in the canonical quote set, `operator_coverage` stores:

- quote count;
- distinct event count;
- distinct event+market count;
- number of independent upstream sources contributing quotes;
- freshest observation time;
- age of the oldest retained quote in the scan.

Coverage is measured against the Tier 1/2 universe in `arbengine.operators`.

## Duplicate source policy

When multiple sources provide the same operator/event/market/outcome, Sportage keeps one
canonical quote. Priority is:

1. direct Betfair API-NG;
2. The Odds API;
3. Odds-API.io;
4. unknown sources.

Within the same priority, the fresher quote wins.

## Why this matters

Backtests and live scanners should not interpret a period with poor bookmaker coverage as
evidence that arbitrage opportunities did not exist. Source and operator health are stored
beside each scan so future analytics can filter results by minimum coverage, freshness and
source redundancy before estimating profitability.
