# Sportage

Sportage is a **net sports-arbitrage** research and scanning engine. It collects sportsbook/exchange quotes, normalizes markets, identifies surebets only when every mutually-exclusive outcome is covered, subtracts configurable execution costs, stores quote history, and replays that history in a bankroll-aware backtest.

The MVP is deliberately cheap: Python + SQLite + a provider adapter + Streamlit. The core domain is kept independent of the UI and data provider so the system can later move to Postgres, Redis/queues, paid feeds, or a different frontend without rewriting the arbitrage math.

## Product goals

1. **Good interface** — dashboard for latest qualifying opportunities, cost assumptions and backtest.
2. **Net backtest** — “what would this have made?” using stored shadow snapshots, configurable costs, thresholds, bankroll, stake size and settlement delay.
3. **Multi-event / multi-market scanner** — H2H/1X2 plus totals and spreads from the first real-data adapter. A signal is shown only when its **NET ROI** clears the configured threshold.
4. **Free-first, scalable later** — local SQLite/Streamlit for MVP; adapters isolate future data upgrades.

## V0.2

- Strict complete-outcome validation (prevents false 1X2 opportunities from incomplete feeds).
- Net-cost model per bookmaker/exchange: commission on winnings, stake fee, fixed cost per leg, conservative quote slippage, min/max stake limits.
- Cost-aware optimiser enumerates bookmaker combinations instead of blindly choosing highest raw odds.
- Markets: H2H, 1X2, totals, spreads.
- Scan-run IDs and quote history in SQLite.
- Bankroll-aware historical backtest with capital locking until settlement and one trade per event/market.
- Streamlit dashboard with latest scanner, backtest and cost-model tabs.
- CLI and CI tests.

No automatic bet placement is implemented. The current objective is to prove that opportunities remain profitable **after** realistic execution assumptions before adding any execution-assistance layer.

## Math: gross vs net

Gross surebet condition for decimal odds `q_i`:

```text
S = sum(1 / q_i)
S < 1
```

Sportage does not stop there. For each bookmaker it applies configured slippage and commission to derive a net-return factor `a_i`. Stakes are chosen so every possible winning outcome returns the same net amount, while placement fees are included in the cash budget. The ranking and threshold use:

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
sportage scan --provider mock --bankroll 1000 --min-net-roi 0.015 --costs config/costs.example.json
```

## Shadow collection

```bash
export THE_ODDS_API_KEY="..."
sportage shadow --provider theoddsapi --markets h2h,spreads,totals --db data/arbitrage.sqlite3 --costs config/costs.example.json --min-net-roi 0.015 --interval 30
```

## Backtest

```bash
sportage backtest --db data/arbitrage.sqlite3 --days 30 --initial-bankroll 5000 --stake-per-arb 500 --min-net-roi 0.015 --costs config/costs.example.json
```

## UI

```bash
sportage ui --db data/arbitrage.sqlite3
```

## Architecture

```text
Odds providers (free first; paid later)
        |
        v
Normalized Quote + complete-market signature
        |
        +------> SQLite shadow history ------> Backtest
        |
        v
Cost-aware arbitrage optimiser
        |
        v
NET threshold / risk filters
        |
        +------> CLI
        +------> Streamlit UI
        +------> future alerts / execution assistant
```

### Scale path

```text
SQLite       -> Postgres/Timescale
polling      -> queues/streaming
single API   -> multiple paid/free adapters
Streamlit    -> API + dedicated web frontend
manual open  -> execution-assistance with preflight checks
```

## Important limitations before real-money use

A mathematical surebet is not operationally guaranteed. The remaining model-risk items include quote latency, rejected legs, maximum stake changes, market-rule mismatches, void rules, settlement differences, account restrictions and data-source gaps. Shadow history exists specifically to measure these before the system is treated as production-ready.
