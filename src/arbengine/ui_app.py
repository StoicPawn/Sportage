from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import streamlit as st

from arbengine.backtest import BacktestConfig, run_backtest
from arbengine.costs import load_cost_config
from arbengine.engine import find_arbitrage
from arbengine.liquidity import load_liquidity_config
from arbengine.models import SettlementResult
from arbengine.storage import SQLiteStore


st.set_page_config(page_title="Sportage", page_icon="📈", layout="wide")
st.markdown(
    """
<style>
.block-container {padding-top: 1.8rem; max-width: 1500px;}
[data-testid="stMetric"] {border: 1px solid rgba(128,128,128,.18); padding: 14px; border-radius: 14px;}
.small-muted {opacity: .7; font-size: .9rem;}
</style>
""",
    unsafe_allow_html=True,
)

st.title("Sportage")
st.caption("Net sports-arbitrage scanner · shadow history · execution-aware backtest")


def money(v: Decimal | float) -> str:
    return f"€{float(v):,.2f}"


def pct(v: Decimal | float) -> str:
    return f"{float(v):.2%}"


def parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


with st.sidebar:
    st.header("Engine settings")
    db_path = Path(st.text_input("SQLite database", os.getenv("ARB_DB_PATH", "data/arbitrage.sqlite3")))
    costs_text = st.text_input("Cost config", os.getenv("ARB_COST_CONFIG", "config/costs.example.json"))
    cost_path = Path(costs_text) if costs_text and Path(costs_text).exists() else None
    liquidity_text = st.text_input("Liquidity config", os.getenv("ARB_LIQUIDITY_CONFIG", ""))
    liquidity_path = Path(liquidity_text) if liquidity_text and Path(liquidity_text).exists() else None
    allocation = st.number_input("Capital per arbitrage (€)", min_value=1.0, value=500.0, step=50.0)
    min_net_roi = st.number_input("Minimum NET ROI", min_value=0.0, value=0.015, step=0.001, format="%.3f")
    max_age = st.number_input("Max quote age (seconds)", min_value=1.0, value=30.0, step=5.0)
    st.caption("Threshold is applied after commission, fees and configured slippage.")

if not db_path.exists():
    st.info("No history database yet. Start shadow mode from the CLI, then refresh this page.")
    st.code("sportage shadow --provider oddsapiio --min-net-roi 0.015")
    st.stop()

cost_book = load_cost_config(cost_path)
liquidity_book = load_liquidity_config(liquidity_path)
store = SQLiteStore(db_path)
summary = store.summary()

m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Completed scans", int(summary["scans"]))
m2.metric("Quotes stored", int(summary["quotes"]))
m3.metric("Signals stored", int(summary["opportunities"]))
m4.metric("Settlements", int(summary["settlements"]))
m5.metric("Best historical NET ROI", pct(float(summary["best_net_roi"])))

tab_live, tab_backtest, tab_costs, tab_liquidity, tab_settlements = st.tabs(
    ["Latest scanner", "Backtest", "Cost model", "Liquidity", "Settlements"]
)

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
            liquidity_book=liquidity_book,
        )
        st.caption(
            f"Latest scan: {scan_time.isoformat()} · {len(quotes)} quotes · "
            f"{len(opportunities)} qualifying surebets"
        )
        if not opportunities:
            st.success("No opportunity currently clears the configured NET threshold.")
        for opp in opportunities[:25]:
            with st.expander(
                f"{opp.event} · {opp.market_signature} · NET {pct(opp.net_roi)} · "
                f"{money(opp.guaranteed_profit)}",
                expanded=False,
            ):
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("NET ROI", pct(opp.net_roi))
                c2.metric("Gross ROI", pct(opp.gross_roi))
                c3.metric("Capital used", money(opp.capital_used))
                c4.metric("Guaranteed net", money(opp.guaranteed_profit))
                st.dataframe(
                    [
                        {
                            "Outcome": leg.outcome,
                            "Bookmaker": leg.bookmaker,
                            "Odds": float(leg.odds),
                            "Effective odds": float(leg.effective_odds),
                            "Stake €": float(leg.stake),
                            "Cash outlay €": float(leg.cash_outlay),
                            "Net return if win €": float(leg.net_return_if_win),
                            "Quote age s": round(leg.quote_age_seconds, 1),
                        }
                        for leg in opp.legs
                    ],
                    use_container_width=True,
                    hide_index=True,
                )

with tab_backtest:
    st.subheader("Historical shadow backtest")
    st.caption(
        "Replays stored snapshots. Persistence and latency require the surebet to remain valid; "
        "results mode also moves bankroll between bookmaker wallets according to actual settlements."
    )

    r1, r2, r3, r4 = st.columns(4)
    lookback = r1.number_input("Lookback days", min_value=1, value=30, step=1)
    initial = r2.number_input("Initial bankroll (€)", min_value=1.0, value=5000.0, step=500.0)
    bt_stake = r3.number_input("Max capital / arb (€)", min_value=1.0, value=float(allocation), step=50.0)
    settlement_mode = r4.selectbox(
        "Settlement model",
        options=["guaranteed", "results"],
        format_func=lambda x: "Guaranteed floor" if x == "guaranteed" else "Actual result / wallets",
    )

    r5, r6, r7 = st.columns(3)
    settlement = r5.number_input("Settlement delay (h)", min_value=0.0, value=3.0, step=0.5)
    persistence = r6.number_input("Min signal persistence (s)", min_value=0.0, value=30.0, step=5.0)
    latency = r7.number_input("Execution latency (s)", min_value=0.0, value=15.0, step=5.0)

    if settlement_mode == "results" and liquidity_path is None:
        st.warning(
            "Results mode is strongest with a liquidity file: without finite bookmaker balances, "
            "Sportage can settle aggregate P&L but cannot constrain future trades by wallet cash."
        )

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=int(lookback))
    result = run_backtest(
        store,
        BacktestConfig(
            initial_bankroll=Decimal(str(initial)),
            stake_per_opportunity=Decimal(str(bt_stake)),
            min_net_roi=Decimal(str(min_net_roi)),
            max_quote_age_seconds=max_age,
            settlement_hours=float(settlement),
            settlement_mode=settlement_mode,
            min_signal_persistence_seconds=float(persistence),
            execution_latency_seconds=float(latency),
            enforce_bookmaker_liquidity=liquidity_path is not None,
            start=start,
            end=end,
        ),
        cost_book=cost_book,
        liquidity_book=liquidity_book,
    )

    b1, b2, b3, b4, b5 = st.columns(5)
    b1.metric("Projected NET", money(result.projected_profit), pct(result.projected_return_pct))
    b2.metric("Executed arbs", len(result.trades))
    b3.metric("Signals seen", result.signals_seen)
    b4.metric("Ending aggregate cash", money(result.ending_cash))
    b5.metric("Capital still locked", money(result.locked_capital))

    x1, x2, x3, x4 = st.columns(4)
    x1.metric("Rejected: too brief", result.signals_rejected_for_persistence)
    x2.metric("Rejected: latency", result.signals_rejected_for_latency)
    x3.metric("Rejected: liquidity", result.signals_rejected_for_liquidity)
    x4.metric("Rejected: missing result", result.signals_rejected_for_missing_result)

    if result.trades:
        st.dataframe(
            [
                {
                    "First seen": t.first_seen_at.isoformat(),
                    "Executed": t.detected_at.isoformat(),
                    "Event": t.event,
                    "Market": t.market,
                    "Persistence s": round(t.persistence_seconds, 1),
                    "Latency s": round(t.execution_latency_seconds, 1),
                    "NET ROI floor": float(t.net_roi),
                    "Capital €": float(t.capital_used),
                    "Guaranteed net €": float(t.guaranteed_profit),
                    "Winner": t.winning_outcome,
                    "Settled net €": float(t.realized_profit),
                    "Settlement": t.settle_at.isoformat(),
                }
                for t in result.trades
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No stored opportunity clears these parameters in the selected period.")

    if result.peak_locked_outlay_by_bookmaker:
        st.markdown("#### Bookmaker working-capital requirements")
        st.dataframe(
            [
                {
                    "Bookmaker": bookmaker,
                    "Peak concurrent outlay €": float(
                        result.peak_locked_outlay_by_bookmaker.get(bookmaker, 0)
                    ),
                    "Period turnover €": float(result.turnover_by_bookmaker.get(bookmaker, 0)),
                }
                for bookmaker in sorted(
                    set(result.peak_locked_outlay_by_bookmaker) | set(result.turnover_by_bookmaker)
                )
            ],
            use_container_width=True,
            hide_index=True,
        )

    if settlement_mode == "results" and result.ending_balance_by_bookmaker:
        st.markdown("#### Bookmaker wallet evolution")
        names = sorted(
            set(result.starting_balance_by_bookmaker) | set(result.ending_balance_by_bookmaker)
        )
        st.dataframe(
            [
                {
                    "Bookmaker": name,
                    "Starting €": float(result.starting_balance_by_bookmaker.get(name, 0)),
                    "Ending €": float(result.ending_balance_by_bookmaker.get(name, 0)),
                    "Change €": float(result.balance_change_by_bookmaker.get(name, 0)),
                }
                for name in names
            ],
            use_container_width=True,
            hide_index=True,
        )

with tab_costs:
    st.subheader("Execution cost assumptions")
    st.caption("These assumptions directly affect both scanner qualification and backtest P&L.")
    profiles = [cost_book.config.default, *cost_book.config.bookmakers]
    st.dataframe(
        [
            {
                "Bookmaker": p.bookmaker,
                "Win commission": float(p.commission_on_winnings_pct),
                "Stake fee": float(p.stake_fee_pct),
                "Fixed cost €": float(p.fixed_cost_per_bet),
                "Slippage bps": float(p.slippage_bps),
                "Min stake €": float(p.min_stake),
                "Max stake €": None if p.max_stake is None else float(p.max_stake),
            }
            for p in profiles
        ],
        use_container_width=True,
        hide_index=True,
    )

with tab_liquidity:
    st.subheader("Bookmaker liquidity")
    st.caption(
        "Optional prefunded cash caps. In results mode these become mutable wallets: losing legs reduce "
        "their account balance and the winning return is credited only to the winning account."
    )
    if liquidity_path is None:
        st.info("No liquidity file configured. Scanner uses only global bankroll and bookmaker stake limits.")
        st.code(
            """{
  "default_balance": 0,
  "bookmakers": {
    "Book A": 300,
    "Book B": 300
  }
}"""
        )
    else:
        st.dataframe(
            [
                {"Bookmaker": name, "Available €": float(balance)}
                for name, balance in liquidity_book.explicit_balances().items()
            ],
            use_container_width=True,
            hide_index=True,
        )
        default_balance = liquidity_book.config.default_balance
        st.caption(
            "Default balance for unlisted bookmakers: "
            f"{default_balance if default_balance is not None else 'unconstrained'}"
        )

with tab_settlements:
    st.subheader("Event / market settlements")
    st.caption(
        "Results are keyed by provider event id + exact market signature. This prevents a totals/spread "
        "line from accidentally settling a different line."
    )
    existing_results = store.list_settlement_results()
    if existing_results:
        st.dataframe(
            [
                {
                    "Event id": item.event_id,
                    "Market signature": item.market_signature,
                    "Winning outcome": item.winning_outcome,
                    "Settled at": item.settled_at.isoformat(),
                    "Source": item.source,
                }
                for item in reversed(existing_results[-100:])
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No settlement results stored yet.")

    st.markdown("#### Add or replace settlement")
    with st.form("settlement_form"):
        f1, f2 = st.columns(2)
        event_id = f1.text_input("Event id")
        market_signature = f2.text_input("Market signature", placeholder="h2h:full_time:")
        f3, f4, f5 = st.columns(3)
        winning_outcome = f3.text_input("Winning outcome")
        settled_at_text = f4.text_input(
            "Settled at (ISO-8601)", value=datetime.now(timezone.utc).isoformat()
        )
        result_source = f5.text_input("Source", value="manual")
        submitted = st.form_submit_button("Save settlement", type="primary")
        if submitted:
            if not event_id or not market_signature or not winning_outcome:
                st.error("Event id, market signature and winning outcome are required.")
            else:
                try:
                    settlement_record = SettlementResult(
                        event_id=event_id.strip(),
                        market_signature=market_signature.strip(),
                        winning_outcome=winning_outcome.strip(),
                        settled_at=parse_iso(settled_at_text.strip()),
                        source=result_source.strip() or "manual",
                    )
                    store.save_settlement_result(settlement_record)
                    st.success(f"Saved {settlement_record.event_market_key}")
                except Exception as exc:
                    st.error(f"Invalid settlement: {exc}")

store.close()
