from __future__ import annotations

import os
from pathlib import Path

import typer

from .execution_abort import AbortSafetyError, abort_if_flat
from .execution_cli import _bind_db, app, console
from .storage import SQLiteStore


@app.command("abort")
def abort(
    execution_id: str = typer.Option(...),
    reason: str = typer.Option("Operator cancelled before exposure"),
    db: Path = typer.Option(Path(os.getenv("ARB_DB_PATH", "data/arbitrage.sqlite3")), exists=True),
) -> None:
    """Abort a prepared execution only when the durable ledger proves exposure is zero."""

    _bind_db(db)
    store = SQLiteStore(db)
    try:
        try:
            result = abort_if_flat(store.conn, execution_id, reason=reason)
        except AbortSafetyError as exc:
            raise typer.BadParameter(str(exc)) from exc
        console.print(f"{result.execution_id}: {result.message}")
    finally:
        store.close()


if __name__ == "__main__":
    app()
