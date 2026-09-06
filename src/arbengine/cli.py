from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .backtest import BacktestConfig, run_backtest
from .connectors.execution import build_execution_connector
from .connectors.base import BetOrder
from .costs import load_cost_config
from .engine import find_arbitrage
from .liquidity import load_liquidity_config
from .models import SettlementResult
from .operators import operators_by_tier
from .provider_health_storage import ProviderHealthStore
from .providers.mock import MockProvider
from .providers.odds_api_io import OddsApiIoProvider
from .providers.the_odds_api import TheOddsAPIProvider
from .providers.unified import build_unified_provider
from .shadow import run_shadow_loop
from .storage import SQLiteStore

app = typer.Typer(no_args_is_help=True)
console = Console()


def _provider(name: str, markets: str = "h2h,spreads,totals"):
    if name == "mock":
        return MockProvider()
    if name == "theoddsapi":
        return TheOddsAPIProvider(markets=markets)
    if name == "oddsapiio":
        return OddsApiIoProvider()
    if name == "unified":
        return build_unified_provider()
    raise typer.BadParameter("provider must be 'mock', 'theoddsapi', 'oddsapiio' or 'unified'")


def _parse_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@app.command("operators")
def operators_command() -> None:
    table = Table("Tier", "Operator", "ADM", "Market data", "Execution", "Domains")
    for spec in operators_by_tier(1, 2):
        table.add_row(
            str(spec.tier),
            f"{spec.display_name} ({spec.operator_id})",
            spec.adm_concession,
            spec.market_data_access.value,
            spec.execution_access.value,
            ", ".join(spec.domains),
        )
    console.print(table)


@app.command("data-health")
def data_health(
    db: Path = typer.Option(Path(os.getenv("ARB_DB_PATH", "data/arbitrage.sqlite3")), exists=True),
) -> None:
    store = SQLiteStore(db)
    try:
        health = ProviderHealthStore(store.conn)
        sources = health.latest_source_health()
        coverage = health.latest_operator_coverage()
    finally:
        store.close()

    if not sources and not coverage:
        console.print("No unified provider-health data stored yet. Run `sportage shadow --provider unified`.")
        return

    if sources:
        table = Table("Source", "Status", "Duration ms", "Raw", "Normalized", "Operators", "Error")
        for row in sources:
            table.add_row(
                row["source"],
                row["status"],
                f"{float(row['duration_ms']):.0f}",
                str(row["raw_quote_count"]),
                str(row["normalized_quote_count"]),
                str(row["operator_count"]),
                "" if row["error_type"] is None else f"{row['error_type']}: {row['error_message'] or ''}",
            )
        console.print(table)

    covered = {row["operator_id"] for row in coverage}
    specs = operators_by_tier(1, 2)
    spec_by_id = {spec.operator_id: spec for spec in specs}
    coverage_table = Table("Tier", "Operator", "Quotes", "Events", "Markets", "Sources", "Oldest age s")
    for row in coverage:
        spec = spec_by_id.get(row["operator_id"])
        coverage_table.add_row(
            str(spec.tier if spec else "?"),
            spec.display_name if spec else row["operator_id"],
            str(row["quote_count"]),
            str(row["event_count"]),
            str(row["market_count"]),
            str(row["source_count"]),
            f"{float(row['oldest_quote_age_seconds']):.1f}",
        )
    console.print(coverage_table)

    missing = [spec.display_name for spec in specs if spec.operator_id not in covered]
    console.print(
        f"Coverage: {len(covered)}/{len(specs)} Tier 1/2 operators"
        + (f" | Missing: {', '.join(missing)}" if missing else " | Full configured universe covered")
    )


@app.command("execution-preflight")
def execution_preflight(
    operator: str = typer.Option(...),
    market_id: str = typer.Option("example-market"),
    selection_id: str = typer.Option("1"),
    stake: float = typer.Option(10.0, min=0.01),
    odds: float = typer.Option(2.0, min=1.01),
) -> None:
    connector = build_execution_connector(operator)
    order = BetOrder(
        operator_id=connector.operator_id,
        market_id=market_id,
        selection_id=selection_id,
        stake=Decimal(str(stake)),
        limit_odds=Decimal(str(odds)),
    )
    result = connector.place_order(order, live=False)
    console.print(f"{connector.operator_id}: {result.status.value} - {result.message}")


@app.command()
def scan(
    provider: str = typer.Option("mock"),
    bankroll: float = typer.Option(1000.0, min=0.01),
    min_net_roi: float = typer.Option(0.0, min=0.0),
    costs: Path | None = typer.Option(None, exists=True),
    liquidity: Path | None = typer.Option(None, exists=True),
    markets: str = typer.Option("h2h,spreads,totals"),
) -> None:
    quotes = _provider(provider, markets=markets).fetch_quotes()
    opportunities = find_arbitrage(
        quotes,
        bankroll=Decimal(str(bankroll)),
        min_net_roi=Decimal(str(min_net_roi)),
        cost_book=load_cost_config(costs),
        liquidity_book=load_liquidity_config(liquidity),
    )
    if not opportunities:
        console.print("No net arbitrage found.")
        raise typer.Exit()

    for opp in opportunities:
        console.print(
            f"[bold]{opp.event}[/bold] {opp.market_signature}  "
            f"NET ROI={opp.net_roi:.3%}  GROSS={opp.gross_roi:.3%}  "
            f"net profit={opp.guaranteed_profit}"
        )
        table = Table("Outcome", "Bookmaker", "Odds", "Effective", "Stake", "Outlay", "Net return")
        for leg in opp.legs:
            table.add_row(
                leg.outcome,
                leg.bookmaker,
                str(leg.odds),
                f"{leg.effective_odds:.4f}",
                str(leg.stake),
                str(leg.cash_outlay),
                str(leg.net_return_if_win),
            )
        console.print(table)


@app.command()
def shadow(
    provider: str = typer.Option("unified"),
    bankroll: float = typer.Option(float(os.getenv("ARB_BANKROLL", "1000")), min=0.01),
    min_net_roi: float = typer.Option(float(os.getenv("ARB_MIN_NET_ROI", "0.015")), min=0.0),
    db: Path = typer.Option(Path(os.getenv("ARB_DB_PATH", "data/arbitrage.sqlite3"))),
    costs: Path | None = typer.Option(None, exists=True),
    liquidity: Path | None = typer.Option(None, exists=True),
    markets: str = typer.Option("h2h,spreads,totals"),
    interval: float = typer.Option(30.0, min=1.0),
    iterations: int | None = typer.Option(None, min=1),
) -> None:
    run_shadow_loop(
        provider=_provider(provider, markets=markets),
        db_path=db,
        bankroll=Decimal(str(bankroll)),
        min_net_roi=Decimal(str(min_net_roi)),
        cost_book=load_cost_config(costs),
        liquidity_book=load_liquidity_config(liquidity),
        interval_seconds=interval,
        iterations=iterations,
    )


@app.command("result-set")
def result_set(
    event_id: str = typer.Option(..., help="Canonical Sportage event id"),
    market_signature: str = typer.Option(..., help="Exact signature, e.g. h2h:full_time:"),
    winning_outcome: str = typer.Option(..., help="Exact normalized winning outcome label"),
    db: Path = typer.Option(Path(os.getenv("ARB_DB_PATH", "data/arbitrage.sqlite3"))),
    settled_at: str | None = typer.Option(None, help="ISO-8601 settlement time; defaults to now"),
    source: str = typer.Option("manual"),
) -> None:
    store = SQLiteStore(db)
    try:
        result = SettlementResult(
            event_id=event_id,
            market_signature=market_signature,
            winning_outcome=winning_outcome,
            settled_at=_parse_datetime(settled_at),
            source=source,
        )
        store.save_settlement_result(result)
    finally:
        store.close()
    console.print(
        f"Saved settlement {result.event_market_key}: winner={result.winning_outcome} "
        f"at {result.settled_at.isoformat()}"
    )


@app.command("results-list")
def results_list(
    db: Path = typer.Option(Path(os.getenv("ARB_DB_PATH", "data/arbitrage.sqlite3")), exists=True),
) -> None:
    store = SQLiteStore(db)
    try:
        results = store.list_settlement_results()
    finally:
        store.close()
    if not results:
        console.print("No settlement results stored.")
        return
    table = Table("Event", "Market", "Winner", "Settled", "Source")
    for result in results:
        table.add_row(
            result.event_id,
            result.market_signature,
            result.winning_outcome,
            result.settled_at.isoformat(),
            result.source,
        )
    console.print(table)


@app.command("backtest")
def backtest_command(
    db: Path = typer.Option(Path(os.getenv("ARB_DB_PATH", "data/arbitrage.sqlite3")), exists=True),
    days: int = typer.Option(30, min=1),
    initial_bankroll: float = typer.Option(5000.0, min=0.01),
    stake_per_arb: float = typer.Option(500.0, min=0.01),
    min_net_roi: float = typer.Option(0.015, min=0.0),
    costs: Path | None = typer.Option(None, exists=True),
    liquidity: Path | None = typer.Option(None, exists=True),
    settlement_hours: float = typer.Option(3.0, min=0.0),
    settlement_mode: str = typer.Option("guaranteed", help="guaranteed or results"),
    min_persistence_seconds: float = typer.Option(0.0, min=0.0),
    execution_latency_seconds: float = typer.Option(0.0, min=0.0),
) -> None:
    if settlement_mode not in {"guaranteed", "results"}:
        raise typer.BadParameter("settlement-mode must be 'guaranteed' or 'results'")
    if settlement_mode == "results" and liquidity is None:
        console.print(
            "[yellow]Results mode without --liquidity tracks aggregate cash but cannot constrain "
            "future trades by finite bookmaker wallets.[/yellow]"
        )

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    store = SQLiteStore(db)
    try:
        result = run_backtest(
            store,
            BacktestConfig(
                initial_bankroll=Decimal(str(initial_bankroll)),
                stake_per_opportunity=Decimal(str(stake_per_arb)),
                min_net_roi=Decimal(str(min_net_roi)),
                settlement_hours=settlement_hours,
                settlement_mode=settlement_mode,
                min_signal_persistence_seconds=min_persistence_seconds,
                execution_latency_seconds=execution_latency_seconds,
                enforce_bookmaker_liquidity=liquidity is not None,
                start=start,
                end=end,
            ),
            cost_book=load_cost_config(costs),
            liquidity_book=load_liquidity_config(liquidity),
        )
    finally:
        store.close()

    console.print(
        f"Scans={result.scans} Trades={len(result.trades)} Signals={result.signals_seen} | "
        f"Projected net={result.projected_profit:.2f} ({result.projected_return_pct:.2%}) | "
        f"Realized={result.realized_profit:.2f} | "
        f"Persistence rejects={result.signals_rejected_for_persistence} | "
        f"Latency rejects={result.signals_rejected_for_latency} | "
        f"Liquidity rejects={result.signals_rejected_for_liquidity} | "
        f"Missing-result rejects={result.signals_rejected_for_missing_result}"
    )
    if result.ending_balance_by_bookmaker:
        balances = Table("Bookmaker", "Start", "End", "Delta")
        names = sorted(
            set(result.starting_balance_by_bookmaker) | set(result.ending_balance_by_bookmaker)
        )
        for name in names:
            balances.add_row(
                name,
                str(result.starting_balance_by_bookmaker.get(name, Decimal("0"))),
                str(result.ending_balance_by_bookmaker.get(name, Decimal("0"))),
                str(result.balance_change_by_bookmaker.get(name, Decimal("0"))),
            )
        console.print(balances)


@app.command()
def ui(
    db: Path = typer.Option(Path(os.getenv("ARB_DB_PATH", "data/arbitrage.sqlite3"))),
) -> None:
    env = os.environ.copy()
    env["ARB_DB_PATH"] = str(db)
    app_path = Path(__file__).resolve().parent / "ui_app.py"
    subprocess.run([sys.executable, "-m", "streamlit", "run", str(app_path)], check=True, env=env)


if __name__ == "__main__":
    app()
