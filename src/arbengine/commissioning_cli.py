from __future__ import annotations

import os
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .commissioning import CommissioningStore, run_commissioning, safety_config_fingerprint
from .storage import SQLiteStore


app = typer.Typer(no_args_is_help=True, help="Sportage read-only production commissioning gate")
console = Console()


def _bind_db(db: Path) -> None:
    os.environ["ARB_DB_PATH"] = str(db)


def _print_report(report) -> None:
    state = "PASS" if report.success else "FAIL"
    console.print(
        f"[bold]Commissioning {state}[/bold] completed={report.completed_at.isoformat()} "
        f"expires={report.expires_at.isoformat()}"
    )
    table = Table("Check", "OK", "Message")
    for check in report.checks:
        table.add_row(check.name, "yes" if check.ok else "NO", check.message)
    console.print(table)
    console.print(f"Config fingerprint: {report.config_fingerprint}")


@app.command("run")
def run(
    db: Path = typer.Option(Path(os.getenv("ARB_DB_PATH", "data/arbitrage.sqlite3"))),
    ttl_minutes: float = typer.Option(30.0, min=1.0, max=240.0),
    refresh_certifications: bool = typer.Option(
        True,
        "--refresh-certifications/--use-existing-certifications",
        help="Refresh both production venue certifications using read-only API calls.",
    ),
) -> None:
    _bind_db(db)
    store = SQLiteStore(db)
    try:
        report = run_commissioning(
            store.conn,
            ttl_minutes=ttl_minutes,
            refresh_certifications=refresh_certifications,
        )
        _print_report(report)
        if not report.success:
            raise typer.Exit(code=2)
    finally:
        store.close()


@app.command("status")
def status(
    db: Path = typer.Option(Path(os.getenv("ARB_DB_PATH", "data/arbitrage.sqlite3"))),
) -> None:
    _bind_db(db)
    store = SQLiteStore(db)
    try:
        commissioning = CommissioningStore(store.conn)
        report = commissioning.latest()
        if report is None:
            console.print("No commissioning run recorded.")
            raise typer.Exit(code=2)
        _print_report(report)
        current = commissioning.valid_current(fingerprint=safety_config_fingerprint())
        console.print(f"Valid for current safety configuration: {'yes' if current else 'NO'}")
        if not current:
            raise typer.Exit(code=2)
    finally:
        store.close()


if __name__ == "__main__":
    app()
