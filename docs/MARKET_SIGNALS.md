# Market signal lifecycle tracking

Sportage records one `market_signal_snapshots` row for every complete market seen during each successful shadow scan.

Each snapshot stores:

- canonical event and market signature;
- best cross-bookmaker price for every outcome;
- gross implied-probability sum;
- gross ROI;
- net ROI when the normal Sportage engine considers the market executable above the configured net threshold;
- distinct bookmaker count;
- status: `normal`, `near_arbitrage`, `gross_arbitrage`, or `net_arbitrage`.

`near_arbitrage` is intentionally recorded before a surebet exists. It lets the adaptive scheduler and later analytics measure whether a market tends to cross into executable territory.

## Lifecycles

`build_lifecycles` converts repeated snapshots into contiguous episodes. Two snapshots belong to the same episode while the gap between them is at or below `max_gap_seconds`. A larger gap starts a new lifecycle.

This prevents a surebet that survives through ten scanner ticks from being counted as ten independent opportunities.

For every lifecycle Sportage calculates:

- first and last observation;
- duration in seconds;
- number of snapshots;
- maximum gross ROI;
- maximum net ROI when available.

`summary_lifecycles` / `summarize_lifecycles` produces median, p90 and maximum duration plus ROI maxima. These metrics are the basis for deciding whether detected opportunities are realistically executable at a chosen human/API latency.
