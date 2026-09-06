from __future__ import annotations

import os
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .canary import CanaryGuard
from .storage import SQLiteStore


app = typer.Typer(no_args_is_help=False, help="Sportage live canary risk envelope")
console = Console()


@app.command("status")
def status(
    db: Path = typer.Option(Path(os.getenv("ARB_DB_PATH", "data/arbitrage.sqlite3"))),
) -> None:
    os.environ["ARB_DB_PATH"] = str(db)
    store = SQLiteStore(db)
    try:
        guard = CanaryGuard(store.conn)
        summary = guard.today_summary()
        policy = summary["policy"]
        console.print(f"Canary enabled: {summary['enabled']}")
        table = Table("Metric", "Today", "Limit")
        table.add_row(
            "Live executions",
            str(summary["live_executions"]),
            str(policy["max_live_executions_per_day"]),
        )
        table.add_row(
            "Active live executions",
            str(summary["active_live_executions"]),
            str(policy["max_active_live_executions"]),
        )
        table.add_row(
            "Prepared capital",
            f"€{summary['prepared_capital']:.2f}",
            f"€{float(policy['max_daily_prepared_capital']):.2f}",
        )
        table.add_row(
            "API order attempts",
            str(summary["api_order_attempts"]),
            str(policy["max_api_order_attempts_per_day"]),
        )
        table.add_row(
            "API authorized liability",
            f"€{summary['api_liability']:.2f}",
            f"€{float(policy['max_daily_api_liability']):.2f}",
        )
        console.print(table)
        console.print(
            f"Per leg ≤ €{float(policy['max_leg_stake']):.2f}; "
            f"per automatic order liability ≤ €{float(policy['max_order_liability']):.2f}; "
            f"per execution ≤ €{float(policy['max_execution_capital']):.2f}."
        )
    finally:
        store.close()


if __name__ == "__main__":
    app()
