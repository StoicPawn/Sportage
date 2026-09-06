from __future__ import annotations

from decimal import Decimal

import pytest

from arbengine.canary import CanaryGuard, CanaryPolicy, CanaryRiskError
from arbengine.connectors.base import BetOrder, ExecutionConnector, ExecutionResult, ExecutionStatus
from arbengine.connectors.execution import HealthTrackedExecutionConnector
from arbengine.connectors.execution_health import reset_all
from arbengine.execution_storage import ExecutionStore
from arbengine.storage import SQLiteStore


def order(stake: str, ref: str = "order-1") -> BetOrder:
    return BetOrder(
        operator_id="betfair",
        market_id="1.100",
        selection_id="10",
        stake=Decimal(stake),
        limit_odds=Decimal("2.00"),
        customer_order_ref=ref,
    )


def plan(*stakes: str) -> dict:
    return {
        "execution_id": "exe-test",
        "event_market_key": "evt|h2h:full_time:",
        "event_id": "evt",
        "market_signature": "h2h:full_time:",
        "fingerprint": "fp",
        "outcomes": [str(i) for i in range(len(stakes))],
        "live": True,
        "legs": [
            {
                "leg_id": f"L{i}",
                "role": "hedge",
                "outcome": str(i),
                "bookmaker": "Betfair Exchange",
                "operator_id": "betfair",
                "automatic": True,
                "order": order(value, f"order-{i}").model_dump(mode="json"),
            }
            for i, value in enumerate(stakes, start=1)
        ],
    }


def test_canary_rejects_oversized_plan(tmp_path):
    store = SQLiteStore(tmp_path / "canary.sqlite3")
    try:
        policy = CanaryPolicy(max_leg_stake=Decimal("5"), max_execution_capital=Decimal("12"))
        guard = CanaryGuard(store.conn, policy)
        with pytest.raises(CanaryRiskError, match="max leg stake"):
            guard.assert_plan(plan("6", "4"))
        with pytest.raises(CanaryRiskError, match="execution capital"):
            guard.assert_plan(plan("5", "5", "2.50"))
    finally:
        store.close()


def test_execution_store_enforces_active_live_canary(tmp_path, monkeypatch):
    monkeypatch.setenv("SPORTAGE_CANARY_MODE", "true")
    store = SQLiteStore(tmp_path / "canary.sqlite3")
    try:
        execution = ExecutionStore(store.conn)
        execution.create_run("exe-1", "evt-1", "prepared", True, plan("4", "4"))
        with pytest.raises(CanaryRiskError, match="active live execution"):
            execution.create_run("exe-2", "evt-2", "prepared", True, plan("4", "4"))
        execution.update_run("exe-1", "completed")
        execution.create_run("exe-2", "evt-2", "prepared", True, plan("4", "4"))
    finally:
        store.close()


def test_canary_order_attempt_budget_and_duplicate_ref(tmp_path):
    store = SQLiteStore(tmp_path / "canary.sqlite3")
    try:
        policy = CanaryPolicy(
            max_api_order_attempts_per_day=2,
            max_daily_api_liability=Decimal("10"),
        )
        guard = CanaryGuard(store.conn, policy)
        first = guard.authorize_order(order("4", "a"))
        guard.finish_order(first, "accepted")
        second = guard.authorize_order(order("4", "b"))
        guard.finish_order(second, "rejected")
        with pytest.raises(CanaryRiskError, match="order-attempt limit"):
            guard.authorize_order(order("1", "c"))

        other_store = SQLiteStore(tmp_path / "duplicate.sqlite3")
        try:
            duplicate_guard = CanaryGuard(other_store.conn, CanaryPolicy())
            duplicate_guard.authorize_order(order("1", "same"))
            with pytest.raises(CanaryRiskError, match="Duplicate canary order reference"):
                duplicate_guard.authorize_order(order("1", "same"))
        finally:
            other_store.close()
    finally:
        store.close()


class FakeAuto(ExecutionConnector):
    operator_id = "betfair"
    automatic_execution = True

    def place_order(self, value, *, live=False):
        return ExecutionResult(
            operator_id="betfair",
            status=ExecutionStatus.ACCEPTED,
            message="fake accepted",
            requested_stake=value.stake,
            requested_odds=value.limit_odds,
            matched_stake=value.stake,
        )


def test_connector_canary_blocks_large_live_order_before_inner(tmp_path, monkeypatch):
    reset_all()
    monkeypatch.setenv("SPORTAGE_CANARY_MODE", "true")
    monkeypatch.setenv("SPORTAGE_LIVE_EXECUTION", "true")
    monkeypatch.setenv("SPORTAGE_REQUIRE_LIVE_CERTIFICATION", "false")
    monkeypatch.setenv("SPORTAGE_REQUIRE_ACCOUNT_FUNDS", "false")
    monkeypatch.setenv("ARB_DB_PATH", str(tmp_path / "live.sqlite3"))
    connector = HealthTrackedExecutionConnector(FakeAuto())

    blocked = connector.place_order(order("8", "too-large"), live=True)
    assert blocked.status == ExecutionStatus.REJECTED
    assert "canary" in blocked.message.lower()

    accepted = connector.place_order(order("5", "small"), live=True)
    assert accepted.status == ExecutionStatus.ACCEPTED
    reset_all()
