# Sportage

Sportage is a **net sports-arbitrage** scanner and backtesting engine. It collects sportsbook/exchange
quotes, normalizes markets, requires complete outcome coverage, subtracts configurable execution costs,
respects bookmaker-specific liquidity, stores quote history and replays that history with conservative
execution assumptions.

The MVP is deliberately cheap: Python + SQLite + provider adapters + Streamlit. The domain core is isolated
from the UI and data providers so it can later move to Postgres/Timescale, queues, paid feeds or a dedicated
frontend without rewriting the arbitrage math.

## Product goals

1. **Usable interface** — live opportunities, exact stakes, costs, bankroll requirements and backtest controls.
2. **Net backtest** — answer “what would this actually have made?” after costs, latency, liquidity and settlement.
3. **Multi-event / multi-market scanner** — H2H/1X2, totals and spreads, filtered on configurable **NET ROI**.
4. **Free-first, scalable later** — inexpensive MVP with provider abstractions ready for broader paid feeds.

## V0.3

- Strict complete-outcome validation, preventing false 1X2 positives from incomplete feeds.
- Cost model per bookmaker/exchange:
  - commission on winnings;
  - stake fees and fixed costs;
  - conservative slippage in basis points;
  - min/max stake limits.
- Cost-aware optimiser evaluates bookmaker combinations instead of blindly picking the highest raw odds.
- Per-bookmaker liquidity caps resize surebets to cash actually available on each account.
- Markets: H2H, 1X2, totals, spreads.
- SQLite shadow history for every scan and quote snapshot.
- Configurable minimum signal persistence.
- **Execution-latency backtest**: a signal must still exist on a later scan after the configured delay and is
  repriced from that later snapshot.
- Two settlement models:
  - `guaranteed`: credits the mathematical guaranteed floor;
  - `results`: requires an exact event+market result, moves cash out of each bookmaker wallet and credits the
    winning return only to the winning bookmaker.
- Exact settlement key = provider event id + full market signature, so different totals/spread lines cannot
  accidentally settle one another.
- Streamlit dashboard with scanner, execution-aware backtest, cost model, liquidity and settlement tabs.
- CLI commands for scanning, shadow collection, backtest and result maintenance.
- GitHub Actions CI.

No automatic bet placement is implemented. The current objective is to prove that opportunities remain
profitable **after realistic execution assumptions** before adding an execution-assistance layer.

## Math: gross vs net

Gross surebet condition for decimal odds `q_i`:

```text
S = sum(1 / q_i)
S < 1
```

Sportage derives net-return factors after configured execution costs and allocates stakes so the net return is
equalised across all mutually exclusive outcomes. Qualification uses:

```text
net_roi = guaranteed_net_profit / actual_capital_used
```

## Install

```bash
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -e ".[dev,ui]"
pytest -q
```

## Local demo

```bash
sportage scan \
  --provider mock \
  --bankroll 1000 \
  --min-net-roi 0.015 \
  --costs config/costs.example.json \
  --liquidity config/liquidity.example.json
```

## Shadow collection

Free-first validation can use Odds-API.io. Event discovery is cached and odds are fetched in batches:

```bash
export ODDS_API_IO_KEY="..."
sportage shadow \
  --provider oddsapiio \
  --db data/arbitrage.sqlite3 \
  --costs config/costs.example.json \
  --liquidity config/liquidity.example.json \
  --min-net-roi 0.015 \
  --interval 120
```

The existing The Odds API adapter supports H2H, spreads and totals:

```bash
export THE_ODDS_API_KEY="..."
sportage shadow --provider theoddsapi --markets h2h,spreads,totals --interval 30
```

Both implement the same provider interface. A broader paid feed can therefore complement or replace them
without changing the arbitrage engine.

## Guaranteed backtest

```bash
sportage backtest \
  --db data/arbitrage.sqlite3 \
  --days 30 \
  --initial-bankroll 5000 \
  --stake-per-arb 500 \
  --min-net-roi 0.015 \
  --min-persistence-seconds 30 \
  --execution-latency-seconds 15 \
  --settlement-mode guaranteed \
  --costs config/costs.example.json \
  --liquidity config/liquidity.example.json
```

This replay:

- scans history in chronological order;
- requires the signal to remain above the NET threshold for the configured persistence period;
- waits for the configured execution latency and **reprices from the later scan**;
- never executes the same event+market twice;
- locks capital until settlement;
- enforces bookmaker-specific concurrent cash requirements;
- reports projected net, turnover, peak bookmaker outlay and rejection reasons.

## Result-settled wallet backtest

Store an exact result:

```bash
sportage result-set \
  --db data/arbitrage.sqlite3 \
  --event-id EVENT123 \
  --market-signature "h2h:full_time:" \
  --winning-outcome "Player A" \
  --settled-at "2026-09-05T20:30:00+02:00" \
  --source manual
```

Review stored settlements:

```bash
sportage results-list --db data/arbitrage.sqlite3
```

Then run:

```bash
sportage backtest \
  --db data/arbitrage.sqlite3 \
  --days 30 \
  --initial-bankroll 5000 \
  --stake-per-arb 500 \
  --min-net-roi 0.015 \
  --min-persistence-seconds 30 \
  --execution-latency-seconds 15 \
  --settlement-mode results \
  --costs config/costs.example.json \
  --liquidity config/liquidity.example.json
```

In `results` mode Sportage does **not** guess missing outcomes. A trade without an exact stored settlement is
excluded from that backtest and counted as a missing-result rejection. When a result exists, losing leg
outlays permanently reduce their bookmaker wallets and the winning leg's net return is credited to the
winning bookmaker. Later trades therefore depend on the actual wallet distribution, not just total bankroll.

## UI

```bash
sportage ui --db data/arbitrage.sqlite3
```

or:

```bash
python -m streamlit run src/arbengine/ui_app.py
```

The dashboard includes a Settlements tab so result records can be added without editing SQLite manually.

## Cost configuration

`config/costs.example.json` is intentionally conservative. Actual values should be calibrated from observed
execution and each operator's fee schedule.

```json
{
  "default": {"bookmaker": "*", "slippage_bps": 10},
  "bookmakers": [
    {"bookmaker": "Betfair Exchange", "commission_on_winnings_pct": 0.05, "slippage_bps": 15}
  ]
}
```

## Liquidity configuration

`config/liquidity.example.json` models where the bankroll actually sits:

```json
{
  "default_balance": 0,
  "bookmakers": {
    "Book A": 300,
    "Book B": 300,
    "Betfair Exchange": 300
  }
}
```

`default_balance: 0` means only listed/funded accounts may be used. `null` leaves unlisted bookmakers
unconstrained.

## Architecture

```text
Odds providers
      |
      v
Normalized quotes + exact market signatures
      |
      +------> SQLite shadow history --------> execution-aware backtest
      |                                             |
      |                                   settlement results / wallets
      v
Cost + liquidity aware optimiser
      |
      v
NET threshold + persistence + latency filters
      |
      +------> CLI
      +------> Streamlit dashboard
      +------> future alerts / execution assistant
```

### Scale path

```text
SQLite       -> Postgres/Timescale
polling      -> queues/streaming
free feeds   -> multiple premium/free adapters
Streamlit    -> API + dedicated web frontend
manual open  -> execution-assistance with preflight checks
```

## Important limitations before real-money use

A mathematical surebet is not operationally guaranteed. Remaining model-risk items include data latency,
rejected legs, bookmaker stake-limit changes, market-rule mismatches, void rules, partial feed coverage,
account restrictions and real execution slippage. Shadow history and settlement-aware replay exist to measure
these before Sportage is treated as production-ready.
