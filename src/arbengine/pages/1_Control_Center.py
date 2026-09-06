from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

from arbengine.control_center import build_control_center_report
from arbengine.storage import SQLiteStore


st.set_page_config(page_title="Sportage Control Center", page_icon="📊", layout="wide")
st.title("Sportage · Analytics Control Center")
st.caption(
    "Surebet lifecycle, execution survival, funnel conversion and operator/market breakdowns. "
    "Metrics are episode-based, not raw scan counts."
)


def pct(value) -> str:
    return "—" if value is None else f"{float(value):.2%}"


def ratio(num: int, den: int) -> str:
    return "—" if den <= 0 else f"{num / den:.1%}"


def rows_to_frame(rows):
    return pd.DataFrame(
        [
            {
                "Group": row.label,
                "Net lifecycles": row.lifecycles,
                "Median lifetime s": round(row.median_duration_seconds, 1),
                "Max lifetime s": round(row.max_duration_seconds, 1),
                "Max NET ROI": None if row.max_net_roi is None else float(row.max_net_roi),
            }
            for row in rows
        ]
    )


with st.sidebar:
    st.header("Analysis window")
    db_path = Path(st.text_input("SQLite database", os.getenv("ARB_DB_PATH", "data/arbitrage.sqlite3")))
    lookback_days = st.number_input("Lookback days", min_value=1, value=30, step=1)
    execution_seconds = st.number_input(
        "Execution requirement (s)",
        min_value=0.0,
        value=15.0,
        step=1.0,
        help="A net surebet counts as executable if the same lifecycle survives at least this long.",
    )
    max_gap_seconds = st.number_input(
        "Lifecycle max gap (s)",
        min_value=1.0,
        value=90.0,
        step=5.0,
        help="Larger gaps split repeated observations into separate surebet episodes.",
    )

if not db_path.exists():
    st.info("No Sportage history database found yet. Start shadow collection and refresh this page.")
    st.code("sportage shadow --provider adaptive")
    st.stop()

end = datetime.now(timezone.utc)
start = end - timedelta(days=int(lookback_days))
store = SQLiteStore(db_path)
try:
    report = build_control_center_report(
        store,
        start=start,
        end=end,
        execution_seconds=float(execution_seconds),
        max_gap_seconds=float(max_gap_seconds),
    )
finally:
    store.close()

if report.signal_snapshots == 0:
    st.warning(
        "No market-signal lifecycle data exists in the selected window yet. "
        "V0.7+ shadow scans populate it automatically."
    )
    st.stop()

k1, k2, k3, k4, k5, k6 = st.columns(6)
k1.metric("Signal snapshots", report.signal_snapshots)
k2.metric("Distinct markets", report.distinct_markets)
k3.metric("Net surebet episodes", report.net_lifecycles)
k4.metric("Median lifetime", f"{report.median_net_lifetime_seconds:.1f}s")
k5.metric("P90 lifetime", f"{report.p90_net_lifetime_seconds:.1f}s")
k6.metric("Max NET ROI", pct(report.max_net_roi))

if report.net_lifecycles < 30:
    st.warning(
        f"Only {report.net_lifecycles} net surebet episodes are available in this window. "
        "Treat survival rates and bookmaker rankings as preliminary until the sample is larger."
    )

st.markdown("### Opportunity funnel")
f = report.funnel
f1, f2, f3, f4 = st.columns(4)
f1.metric("Near or better", f.near_or_better)
f2.metric("Gross arbitrage", f.gross_or_better, ratio(f.gross_or_better, f.near_or_better))
f3.metric("Net arbitrage", f.net_arbitrage, ratio(f.net_arbitrage, f.gross_or_better))
f4.metric(
    f"Executable ≥ {execution_seconds:g}s",
    f.executable,
    ratio(f.executable, f.net_arbitrage),
)
st.caption(
    "Funnel stages are cumulative by event+market: gross includes markets that later reached net; "
    "executable counts distinct net-arbitrage lifecycles that survived the selected execution time."
)

st.markdown("### Execution survival curve")
survival_df = pd.DataFrame(
    [
        {
            "Seconds": int(point.seconds) if point.seconds.is_integer() else point.seconds,
            "Surviving episodes": point.survivors,
            "Survival %": point.survival_rate * 100.0,
        }
        for point in report.survival
    ]
)
c1, c2 = st.columns([1.4, 1.0])
with c1:
    chart_df = survival_df.set_index("Seconds")[["Survival %"]]
    st.bar_chart(chart_df, y_label="Survival %", x_label="Required execution time (s)")
with c2:
    st.dataframe(survival_df, use_container_width=True, hide_index=True)

survival_map = {point.seconds: point.survival_rate for point in report.survival}
if report.net_lifecycles:
    s15 = survival_map.get(15.0)
    s30 = survival_map.get(30.0)
    if s15 is not None:
        st.caption(
            f"Observed survival: {s15:.1%} of net episodes lasted at least 15s"
            + (f"; {s30:.1%} lasted at least 30s." if s30 is not None else ".")
        )

st.markdown("### Where executable opportunities come from")
tab_sport, tab_market, tab_book = st.tabs(["Sport", "Market", "Bookmaker involvement"])

with tab_sport:
    sport_df = rows_to_frame(report.by_sport)
    if sport_df.empty:
        st.info("No sport breakdown available yet.")
    else:
        st.dataframe(
            sport_df,
            use_container_width=True,
            hide_index=True,
            column_config={"Max NET ROI": st.column_config.NumberColumn(format="%.2%%")},
        )

with tab_market:
    market_df = rows_to_frame(report.by_market)
    if market_df.empty:
        st.info("No market breakdown available yet.")
    else:
        st.dataframe(
            market_df,
            use_container_width=True,
            hide_index=True,
            column_config={"Max NET ROI": st.column_config.NumberColumn(format="%.2%%")},
        )

with tab_book:
    book_df = rows_to_frame(report.by_bookmaker)
    if book_df.empty:
        st.info("No bookmaker breakdown available yet.")
    else:
        st.dataframe(
            book_df,
            use_container_width=True,
            hide_index=True,
            column_config={"Max NET ROI": st.column_config.NumberColumn(format="%.2%%")},
        )
        st.caption(
            "A lifecycle can involve multiple bookmakers, so bookmaker counts are not mutually exclusive. "
            "Each bookmaker is counted at most once per lifecycle."
        )

st.markdown("### Reading the result")
if report.net_lifecycles == 0:
    st.info("No net arbitrage episode was observed in the selected window.")
elif f.executable == 0:
    st.warning(
        "Net arbitrages were observed, but none survived the selected execution requirement. "
        "Under these observations, execution speed is currently the limiting factor."
    )
else:
    execution_rate = f.executable / report.net_lifecycles
    st.write(
        f"With a {execution_seconds:g}s execution requirement, "
        f"{f.executable}/{report.net_lifecycles} observed net episodes ({execution_rate:.1%}) "
        "would have remained visible long enough to attempt execution. This is an operational "
        "survival measure, not a guarantee that both bookmaker legs would be accepted."
    )
