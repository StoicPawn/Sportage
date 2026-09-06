# Sportage Analytics Control Center

Sportage V0.8 adds an episode-based analytics page to the Streamlit application.

Run:

```bash
sportage ui --db data/arbitrage.sqlite3
```

Streamlit discovers `src/arbengine/pages/1_Control_Center.py` as a second application page.

## What it measures

The Control Center reads the `market_signal_snapshots` history produced by V0.7+ shadow scans and converts repeated observations into contiguous lifecycles. A surebet observed on twelve consecutive five-second scans is therefore one episode, not twelve opportunities.

### Funnel

The funnel is cumulative by canonical event + market:

- `near or better`: the market was at some point within the configured near-arbitrage band, gross-arbitrage, or net-arbitrage;
- `gross or better`: the market reached mathematical gross arbitrage or net arbitrage;
- `net arbitrage`: the market produced an executable net-positive combination after configured costs;
- `executable >= N seconds`: a distinct net-arbitrage lifecycle remained present for at least the selected execution requirement.

### Survival curve

The default execution horizons are 2, 5, 10, 15, 30 and 60 seconds. For every threshold Sportage reports both the surviving episode count and the observed survival percentage.

This is an operational persistence metric. It does not imply that both bookmaker legs would necessarily be accepted.

### Breakdowns

Net-arbitrage lifecycles are summarized by:

- sport;
- exact market signature;
- bookmaker involvement.

A bookmaker is counted at most once per lifecycle even when it appears in multiple snapshots. Bookmaker counts are not mutually exclusive because one lifecycle normally contains more than one operator.

## Sample-size warning

The UI warns when fewer than 30 net-arbitrage lifecycles exist in the selected window. Rankings and survival rates from a small sample should be treated as preliminary rather than evidence of profitability.

## Freshness

Lifecycle collection only uses quotes inside the same operational freshness window used by the arbitrage engine. Scheduler cache retention therefore cannot create artificial long-lived surebets from stale prices.
