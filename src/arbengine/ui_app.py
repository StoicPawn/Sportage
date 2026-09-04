from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import streamlit as st

from arbengine.backtest import BacktestConfig, run_backtest
from arbengine.costs import load_cost_config
from arbengine.engine import find_arbitrage
from arbengine.storage import SQLiteStore

st.set_page_config(page_title="Sportage", layout="wide")
st.markdown("""
<style>
.block-container {padding-top: 1.8rem; max-width: 1500px;}
[data-testid="stMetric"] {border: 1px solid rgba(128,128,128,.18); padding: 14px; border-radius: 14px;}
</style>
""", unsafe_allow_html=True)

st.title("Sportage")
st.caption("Net sports-arbitrage scanner · shadow history · configurable backtest")


def money(v) -> str:
    return f"€{float(v):,.2f}"


def pct(v) -> str:
    return f"{float(v):.2%}"


with st.sidebar:
    st.header("Engine settings")
    db_path = Path(st.text_input("SQLite database", os.getenv("ARB_DB_PATH", "data/arbitrage.sqlite3")))
    costs_text = st.text_input("Cost config", os.getenv("ARB_COST_CONFIG", "config/costs.example.json"))
    cost_path = Path(costs_text) if costs_text and Path(costs_text).exists() else None
    allocation = st.number_input("Capital per arbitrage (€)", min_value=1.0, value=500.0, step=50.0)
    min_net_roi = st.number_input("Minimum NET ROI", min_value=0.0, value=0.015, step=0.001, format="%.3f")
    max_age = st.number_input("Max quote age (seconds)", min_value=1.0, value=30.0, step=5.0)
    st.caption("Threshold is applied after commission, fees and configured slippage.")

if not db_path.exists():
    st.info("No history database yet. Start shadow mode, then refresh this page.")
    st.code("sportage shadow --provider oddsapiio --min-net-roi 0.015")
    st.stop()

cost_book = load_cost_config(cost_path)
store = SQLiteStore(db_path)
summary = store.summary()

m1, m2, m3, m4 = st.columns(4)
m1.metric("Completed scans", int(summary["scans"]))
m2.metric("Quotes stored", int(summary["quotes"]))
m3.metric("Signals stored", int(summary["opportunities"]))
m4.metric("Best historical NET ROI", pct(float(summary["best_net_roi"])))

tab_live, tab_backtest, tab_costs = st.tabs(["Latest scanner", "Backtest", "Cost model"])

with tab_live:
    latest = store.latest_scan()
    if latest is None:
        st.warning("No completed scan available yet.")
    else:
        scan_time = datetime.fromisoformat(latest["started_at"])
        quotes = store.load_quotes_for_scan(int(latest["id"]))
        opportunities = find_arbitrage(
            quotes,
            bankroll=Decimal(str(allocation)),
            min_net_roi=Decimal(str(min_net_roi)),
            max_quote_age_seconds=max_age,
            now=scan_time,
            cost_book=cost_book,
        )
        st.caption(f"Latest scan: {scan_time.isoformat()} · {len(quotes)} quotes · {len(opportunities)} qualifying surebets")
        if not opportunities:
            st.success("No opportunity currently clears the configured NET threshold.")
        for opp in opportunities[:25]:
            with st.expander(f"{opp.event} · {opp.market_signature} · NET {pct(opp.net_roi)} · {money(opp.guaranteed_profit)}"):
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("NET ROI", pct(opp.net_roi))
                c2.metric("Gross ROI", pct(opp.gross_roi))
                c3.metric("Capital used", money(opp.capital_used))
                c4.metric("Guaranteed net", money(opp.guaranteed_profit))
                st.dataframe([{
                    "Outcome": leg.outcome,
                    "Bookmaker": leg.bookmaker,
                    "Odds": float(leg.odds),
                    "Effective odds": float(leg.effective_odds),
                    "Stake €": float(leg.stake),
                    "Cash outlay €": float(leg.cash_outlay),
                    "Net return if win €": float(leg.net_return_if_win),
                    "Quote age s": round(leg.quote_age_seconds, 1),
                } for leg in opp.legs], use_container_width=True, hide_index=True)

with tab_backtest:
    st.subheader("Historical shadow backtest")
    st.caption("Replays stored quote snapshots with the selected threshold/cost assumptions; a configurable persistence filter rejects one-snapshot arbs.")
    c1, c2, c3, c4, c5 = st.columns(5)
    lookback = c1.number_input("Lookback days", min_value=1, value=30, step=1)
    initial = c2.number_input("Initial bankroll (€)", min_value=1.0, value=5000.0, step=500.0)
    bt_stake = c3.number_input("Max capital / arb (€)", min_value=1.0, value=float(allocation), step=50.0)
    settlement = c4.number_input("Settlement delay (h)", min_value=0.0, value=3.0, step=0.5)
    persistence = c5.number_input("Min signal persistence (s)", min_value=0.0, value=30.0, step=5.0)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=int(lookback))
    result = run_backtest(store, BacktestConfig(
        initial_bankroll=Decimal(str(initial)),
        stake_per_opportunity=Decimal(str(bt_stake)),
        min_net_roi=Decimal(str(min_net_roi)),
        max_quote_age_seconds=max_age,
        settlement_hours=float(settlement),
        min_signal_persistence_seconds=float(persistence),
        start=start,
        end=end,
    ), cost_book=cost_book)

    b1, b2, b3, b4, b5, b6 = st.columns(6)
    b1.metric("Projected NET", money(result.projected_profit), pct(result.projected_return_pct))
    b2.metric("Executed arbs", len(result.trades))
    b3.metric("Signals seen", result.signals_seen)
    b4.metric("Ending cash", money(result.ending_cash))
    b5.metric("Capital locked", money(result.locked_capital))
    b6.metric("Rejected: too brief", result.signals_rejected_for_persistence)
    if result.trades:
        st.dataframe([{
            "First seen": t.first_seen_at.isoformat(),
            "Executed": t.detected_at.isoformat(),
            "Event": t.event,
            "Market": t.market,
            "Persistence s": round(t.persistence_seconds, 1),
            "NET ROI": float(t.net_roi),
            "Capital €": float(t.capital_used),
            "Guaranteed net €": float(t.guaranteed_profit),
            "Settlement": t.settle_at.isoformat(),
        } for t in result.trades], use_container_width=True, hide_index=True)
    else:
        st.info("No stored opportunity clears these parameters in the selected period.")

with tab_costs:
    st.subheader("Execution cost assumptions")
    profiles = [cost_book.config.default, *cost_book.config.bookmakers]
    st.dataframe([{
        "Bookmaker": p.bookmaker,
        "Win commission": float(p.commission_on_winnings_pct),
        "Stake fee": float(p.stake_fee_pct),
        "Fixed cost €": float(p.fixed_cost_per_bet),
        "Slippage bps": float(p.slippage_bps),
        "Min stake €": float(p.min_stake),
        "Max stake €": None if p.max_stake is None else float(p.max_stake),
    } for p in profiles], use_container_width=True, hide_index=True)

store.close()
