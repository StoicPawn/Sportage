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
from .costs import load_cost_config
from .engine import find_arbitrage
from .providers.mock import MockProvider
from .providers.odds_api_io import OddsApiIoProvider
from .providers.the_odds_api import TheOddsAPIProvider
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
    raise typer.BadParameter("provider must be 'mock', 'theoddsapi' or 'oddsapiio'")


@app.command()
def scan(
    provider: str = typer.Option("mock"),
    bankroll: float = typer.Option(1000.0, min=0.01),
    min_net_roi: float = typer.Option(0.0, min=0.0),
    costs: Path | None = typer.Option(None, exists=True),
    markets: str = typer.Option("h2h,spreads,totals"),
) -> None:
    quotes = _provider(provider, markets=markets).fetch_quotes()
    opportunities = find_arbitrage(
        quotes,
        bankroll=Decimal(str(bankroll)),
        min_net_roi=Decimal(str(min_net_roi)),
        cost_book=load_cost_config(costs),
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
    provider: str = typer.Option("mock"),
    bankroll: float = typer.Option(float(os.getenv("ARB_BANKROLL", "1000")), min=0.01),
    min_net_roi: float = typer.Option(float(os.getenv("ARB_MIN_NET_ROI", "0.015")), min=0.0),
    db: Path = typer.Option(Path(os.getenv("ARB_DB_PATH", "data/arbitrage.sqlite3"))),
    costs: Path | None = typer.Option(None, exists=True),
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
        interval_seconds=interval,
        iterations=iterations,
    )


@app.command("backtest")
def backtest_command(
    db: Path = typer.Option(Path(os.getenv("ARB_DB_PATH", "data/arbitrage.sqlite3")), exists=True),
    days: int = typer.Option(30, min=1),
    initial_bankroll: float = typer.Option(5000.0, min=0.01),
    stake_per_arb: float = typer.Option(500.0, min=0.01),
    min_net_roi: float = typer.Option(0.015, min=0.0),
    costs: Path | None = typer.Option(None, exists=True),
    settlement_hours: float = typer.Option(3.0, min=0.0),
    min_persistence_seconds: float = typer.Option(0.0, min=0.0),
) -> None:
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
                min_signal_persistence_seconds=min_persistence_seconds,
                start=start,
                end=end,
            ),
            cost_book=load_cost_config(costs),
        )
    finally:
        store.close()

    console.print(
        f"Scans={result.scans} Trades={len(result.trades)} Signals={result.signals_seen} | "
        f"Projected net={result.projected_profit:.2f} ({result.projected_return_pct:.2%}) | "
        f"Realized={result.realized_profit:.2f} | "
        f"Persistence rejects={result.signals_rejected_for_persistence}"
    )


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
