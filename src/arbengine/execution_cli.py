from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .costs import load_cost_config
from .execution_coordinator import ExecutionCoordinator, ExecutionPlan, ExecutionPolicy, RunStatus
from .execution_storage import ExecutionStore
from .models import ArbitrageOpportunity
from .providers.scheduler import build_adaptive_provider
from .storage import SQLiteStore

app = typer.Typer(no_args_is_help=True, help="Sportage fail-closed execution coordinator")
console = Console()


def _policy(path: Path | None) -> ExecutionPolicy:
    if path is None:
        default = Path(os.getenv("SPORTAGE_EXECUTION_POLICY", "config/execution_policy.example.json"))
        path = default if default.exists() else None
    if path is None:
        return ExecutionPolicy()
    return ExecutionPolicy.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _cost_path(path: Path | None) -> Path | None:
    if path is not None:
        return path
    value = os.getenv("ARB_COST_CONFIG", "config/costs.example.json")
    candidate = Path(value)
    return candidate if candidate.exists() else None


@app.command("prepare")
def prepare(
    scan_id: int = typer.Option(..., min=1),
    opportunity_index: int = typer.Option(0, min=0),
    db: Path = typer.Option(Path(os.getenv("ARB_DB_PATH", "data/arbitrage.sqlite3")), exists=True),
    policy: Path | None = typer.Option(None, exists=True),
    costs: Path | None = typer.Option(None, exists=True),
    live: bool = typer.Option(False, help="Mark this plan as eligible for live execution on resume"),
) -> None:
    store = SQLiteStore(db)
    try:
        rows = store.conn.execute(
            "SELECT payload FROM opportunities WHERE scan_id=? ORDER BY CAST(net_roi AS REAL) DESC, id",
            (scan_id,),
        ).fetchall()
        if opportunity_index >= len(rows):
            raise typer.BadParameter(f"scan {scan_id} has only {len(rows)} stored opportunities")
        opportunity = ArbitrageOpportunity.model_validate(json.loads(rows[opportunity_index]["payload"]))
        quotes = store.load_quotes_for_scan(scan_id)
        coordinator = ExecutionCoordinator(
            store, policy=_policy(policy), cost_book=load_cost_config(_cost_path(costs))
        )
        plan = coordinator.prepare(opportunity, quotes, live=live)
        run = coordinator.exec_store.get_run(plan.execution_id)
        status = run["status"] if run else "unknown"
        console.print(f"[bold]{plan.execution_id}[/bold] status={status} live={live}")
        table = Table("Leg", "Role", "Operator", "Outcome", "Stake", "Min odds", "Mode / next action")
        for leg in plan.legs:
            action = "AUTO FOK" if leg.automatic else "MANUAL CONFIRM"
            table.add_row(
                leg.leg_id,
                leg.role.value,
                leg.operator_id,
                leg.outcome,
                str(leg.order.stake),
                str(leg.order.limit_odds),
                action,
            )
        console.print(table)
        manual = [leg for leg in plan.legs if not leg.automatic]
        if manual:
            console.print("Confirm each manual leg only after the bookmaker shows the bet as accepted.")
            for leg in manual:
                if leg.order.deep_link:
                    console.print(f"{leg.leg_id}: {leg.order.deep_link}")
    finally:
        store.close()


@app.command("confirm")
def confirm(
    execution_id: str = typer.Option(...),
    leg_id: str = typer.Option(...),
    accepted: bool = typer.Option(True, "--accepted/--rejected"),
    matched_stake: Decimal | None = typer.Option(None),
    average_odds: Decimal | None = typer.Option(None),
    bet_id: str | None = typer.Option(None),
    db: Path = typer.Option(Path(os.getenv("ARB_DB_PATH", "data/arbitrage.sqlite3")), exists=True),
    policy: Path | None = typer.Option(None, exists=True),
    costs: Path | None = typer.Option(None, exists=True),
) -> None:
    store = SQLiteStore(db)
    try:
        coordinator = ExecutionCoordinator(
            store, policy=_policy(policy), cost_book=load_cost_config(_cost_path(costs))
        )
        result = coordinator.confirm_manual_leg(
            execution_id,
            leg_id,
            accepted=accepted,
            matched_stake=matched_stake,
            average_odds=average_odds,
            bet_id=bet_id,
        )
        console.print(f"{result.execution_id}: {result.status.value} - {result.message}")
    finally:
        store.close()


@app.command("resume")
def resume(
    execution_id: str = typer.Option(...),
    db: Path = typer.Option(Path(os.getenv("ARB_DB_PATH", "data/arbitrage.sqlite3")), exists=True),
    policy: Path | None = typer.Option(None, exists=True),
    costs: Path | None = typer.Option(None, exists=True),
    scheduler_config: Path = typer.Option(
        Path(os.getenv("SPORTAGE_SCHEDULER_CONFIG", "config/provider_scheduler.example.json")), exists=True
    ),
    live: bool = typer.Option(False, help="Actually submit supported official-API orders"),
) -> None:
    provider = build_adaptive_provider(scheduler_config)
    report = provider.fetch_report()
    if not report.quotes:
        raise typer.BadParameter("No fresh market quotes are available for hedge/rescue")
    store = SQLiteStore(db)
    try:
        coordinator = ExecutionCoordinator(
            store, policy=_policy(policy), cost_book=load_cost_config(_cost_path(costs))
        )
        result = coordinator.resume(execution_id, report.quotes, live=live)
        console.print(f"{result.execution_id}: {result.status.value} - {result.message}")
        if result.rescue_loss is not None:
            console.print(f"Rescue loss floor: €{result.rescue_loss:.2f}")
    finally:
        store.close()


@app.command("status")
def status(
    execution_id: str = typer.Option(...),
    db: Path = typer.Option(Path(os.getenv("ARB_DB_PATH", "data/arbitrage.sqlite3")), exists=True),
) -> None:
    store = SQLiteStore(db)
    try:
        execution = ExecutionStore(store.conn)
        halted, reason = execution.halt_state()
        row = execution.get_run(execution_id)
        if row is None:
            raise typer.BadParameter("Unknown execution id")
        console.print(
            f"[bold]{execution_id}[/bold] status={row['status']} live={bool(row['live'])} "
            f"global_halt={halted}"
        )
        if reason:
            console.print(f"HALT reason: {reason}")
        if row["reason"]:
            console.print(f"Run reason: {row['reason']}")
        table = Table("Leg", "Role", "Outcome", "Operator", "Status", "Matched", "Avg odds", "Bet id")
        for leg in execution.get_legs(execution_id):
            result = json.loads(leg["result_json"]) if leg["result_json"] else {}
            table.add_row(
                leg["leg_id"], leg["role"], leg["outcome"], leg["operator_id"], leg["status"],
                str(result.get("matched_stake") or ""), str(result.get("average_price_matched") or ""),
                str(result.get("bet_id") or ""),
            )
        console.print(table)
        events = execution.events(execution_id)
        if events:
            console.print("Events: " + " -> ".join(event["event_type"] for event in events))
    finally:
        store.close()


@app.command("halt")
def halt(
    reason: str = typer.Option(...),
    db: Path = typer.Option(Path(os.getenv("ARB_DB_PATH", "data/arbitrage.sqlite3"))),
) -> None:
    store = SQLiteStore(db)
    try:
        ExecutionStore(store.conn).set_halt(f"manual: {reason}")
        console.print("Global execution halt enabled.")
    finally:
        store.close()


@app.command("resolve-emergency")
def resolve_emergency(
    execution_id: str = typer.Option(...),
    confirm_flat: bool = typer.Option(False, "--confirm-flat", help="Confirm exposure has been manually neutralized"),
    clear_global_halt: bool = typer.Option(False, "--clear-global-halt"),
    db: Path = typer.Option(Path(os.getenv("ARB_DB_PATH", "data/arbitrage.sqlite3")), exists=True),
) -> None:
    if not confirm_flat:
        raise typer.BadParameter("Use --confirm-flat only after independently verifying that exposure is flat")
    store = SQLiteStore(db)
    try:
        execution = ExecutionStore(store.conn)
        row = execution.get_run(execution_id)
        if row is None:
            raise typer.BadParameter("Unknown execution id")
        plan = ExecutionPlan.model_validate(json.loads(row["plan_json"]))
        execution.release_lock(plan.event_market_key, execution_id)
        execution.update_run(
            execution_id,
            RunStatus.ABORTED.value,
            "Emergency manually reconciled; operator confirmed exposure flat",
        )
        execution.event(execution_id, "EMERGENCY_MANUALLY_RESOLVED", {"confirm_flat": True})
        if clear_global_halt:
            execution.clear_halt()
        console.print(
            "Emergency marked reconciled and event lock released. "
            + ("Global halt cleared." if clear_global_halt else "Global halt remains enabled.")
        )
    finally:
        store.close()


if __name__ == "__main__":
    app()
