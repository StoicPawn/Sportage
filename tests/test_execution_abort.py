from __future__ import annotations

import json
from decimal import Decimal

import pytest

from arbengine.canary import CanaryGuard
from arbengine.connectors.base import BetOrder, ExecutionResult, ExecutionStatus
from arbengine.execution_abort import AbortSafetyError, abort_if_flat
from arbengine.execution_storage import ExecutionStore
from arbengine.storage import SQLiteStore


def _plan(execution_id: str, event_key: str) -> dict:
    manual = BetOrder(
        operator_id="bet365",
        market_id=f"manual:{execution_id}:0",
        selection_id="A",
        stake=Decimal("4"),
        limit_odds=Decimal("2.10"),
        customer_order_ref=f"{execution_id}-manual",
    )
    hedge = BetOrder(
        operator_id="betfair",
        market_id="1.100",
        selection_id="22",
        stake=Decimal("4"),
        limit_odds=Decimal("2.05"),
        customer_order_ref=f"{execution_id}-hedge",
    )
    return {
        "execution_id": execution_id,
        "event_market_key": event_key,
        "event_id": "evt-abort",
        "market_signature": "h2h:full_time:",
        "fingerprint": "fp-abort",
        "outcomes": ["A", "B"],
        "live": True,
        "legs": [
            {
                "leg_id": "L1",
                "role": "primary",
                "outcome": "A",
                "bookmaker": "Bet365",
                "operator_id": "bet365",
                "automatic": False,
                "order": manual.model_dump(mode="json"),
            },
            {
                "leg_id": "L2",
                "role": "hedge",
                "outcome": "B",
                "bookmaker": "Betfair Exchange",
                "operator_id": "betfair",
                "automatic": True,
                "order": hedge.model_dump(mode="json"),
            },
        ],
    }


def _create_pre_exposure_run(store: SQLiteStore, execution_id: str, event_key: str) -> ExecutionStore:
    execution = ExecutionStore(store.conn)
    plan = _plan(execution_id, event_key)
    execution.acquire_lock(event_key, execution_id)
    execution.create_run(execution_id, event_key, "waiting_manual", True, plan)
    for leg, status in zip(plan["legs"], ("manual_required", "prepared"), strict=True):
        execution.save_leg(
            execution_id,
            leg["leg_id"],
            leg["role"],
            leg["outcome"],
            leg["operator_id"],
            status,
            leg["order"],
        )
    return execution


def test_abort_zero_exposure_releases_lock_and_active_canary_slot(tmp_path, monkeypatch):
    monkeypatch.setenv("SPORTAGE_CANARY_MODE", "true")
    store = SQLiteStore(tmp_path / "abort.sqlite3")
    try:
        execution = _create_pre_exposure_run(store, "exe-1", "evt-1|h2h")
        before = CanaryGuard(store.conn).today_summary()
        assert before["active_live_executions"] == 1
        assert before["live_executions"] == 1

        result = abort_if_flat(store.conn, "exe-1", reason="user cancelled test")
        assert result.aborted is True
        assert execution.get_run("exe-1")["status"] == "aborted"
        assert store.conn.execute(
            "SELECT 1 FROM execution_locks WHERE event_market_key='evt-1|h2h'"
        ).fetchone() is None
        events = execution.events("exe-1")
        assert events[-1]["event_type"] == "ABORTED_ZERO_EXPOSURE"

        after = CanaryGuard(store.conn).today_summary()
        assert after["active_live_executions"] == 0
        assert after["live_executions"] == 1

        # The active slot is free again; the aborted preparation still counts toward
        # the daily preparation limit, by design.
        _create_pre_exposure_run(store, "exe-2", "evt-2|h2h")
        assert CanaryGuard(store.conn).today_summary()["active_live_executions"] == 1
    finally:
        store.close()


def test_abort_refuses_matched_exposure_and_keeps_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("SPORTAGE_CANARY_MODE", "true")
    store = SQLiteStore(tmp_path / "matched.sqlite3")
    try:
        execution = _create_pre_exposure_run(store, "exe-match", "evt-match|h2h")
        plan = _plan("exe-match", "evt-match|h2h")
        accepted = ExecutionResult(
            operator_id="bet365",
            status=ExecutionStatus.ACCEPTED,
            message="manual accepted",
            bet_id="ticket-1",
            requested_stake=Decimal("4"),
            requested_odds=Decimal("2.10"),
            matched_stake=Decimal("4"),
            average_price_matched=Decimal("2.10"),
            remaining_stake=Decimal("0"),
        )
        leg = plan["legs"][0]
        execution.save_leg(
            "exe-match",
            "L1",
            "primary",
            "A",
            "bet365",
            "accepted",
            leg["order"],
            accepted.model_dump(mode="json"),
        )
        execution.update_run("exe-match", "prepared")

        with pytest.raises(AbortSafetyError, match="status=accepted"):
            abort_if_flat(store.conn, "exe-match")
        assert execution.get_run("exe-match")["status"] == "prepared"
        assert store.conn.execute(
            "SELECT execution_id FROM execution_locks WHERE event_market_key='evt-match|h2h'"
        ).fetchone()["execution_id"] == "exe-match"
    finally:
        store.close()


def test_abort_refuses_unknown_or_inflight_state_even_with_zero_matched(tmp_path, monkeypatch):
    monkeypatch.setenv("SPORTAGE_CANARY_MODE", "true")
    store = SQLiteStore(tmp_path / "unknown.sqlite3")
    try:
        execution = _create_pre_exposure_run(store, "exe-unknown", "evt-unknown|h2h")
        plan = _plan("exe-unknown", "evt-unknown|h2h")
        unknown = ExecutionResult(
            operator_id="betfair",
            status=ExecutionStatus.UNKNOWN,
            message="transport timeout",
            customer_order_ref="exe-unknown-hedge",
            requested_stake=Decimal("4"),
            requested_odds=Decimal("2.05"),
            matched_stake=Decimal("0"),
        )
        leg = plan["legs"][1]
        execution.save_leg(
            "exe-unknown",
            "L2",
            "hedge",
            "B",
            "betfair",
            "unknown",
            leg["order"],
            unknown.model_dump(mode="json"),
        )

        with pytest.raises(AbortSafetyError, match="status=unknown"):
            abort_if_flat(store.conn, "exe-unknown")
        assert execution.get_run("exe-unknown")["status"] == "waiting_manual"
    finally:
        store.close()
