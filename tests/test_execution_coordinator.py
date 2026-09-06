from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import arbengine.execution_coordinator as coordinator_module
from arbengine.connectors.base import (
    ExecutionConnector,
    ExecutionPreflight,
    ExecutionResult,
    ExecutionStatus,
)
from arbengine.execution_coordinator import ExecutionCoordinator, ExecutionPolicy, RunStatus
from arbengine.models import ArbitrageOpportunity, Leg, MarketType, Quote
from arbengine.storage import SQLiteStore


NOW = datetime.now(timezone.utc)


def make_opportunity() -> ArbitrageOpportunity:
    return ArbitrageOpportunity(
        event_id="evt-1",
        sport="football",
        event="A vs B",
        commence_time=NOW + timedelta(hours=2),
        market=MarketType.H2H,
        gross_implied_sum=Decimal("0.97"),
        gross_roi=Decimal("0.03"),
        net_roi=Decimal("0.02"),
        capital_available=Decimal("1000"),
        capital_used=Decimal("202.44"),
        unallocated_cash=Decimal("797.56"),
        guaranteed_payout=Decimal("210"),
        gross_guaranteed_profit=Decimal("7.56"),
        guaranteed_profit=Decimal("7.56"),
        estimated_costs=Decimal("0"),
        legs=[
            Leg(
                outcome="A",
                bookmaker="Bet365",
                operator_id="bet365",
                odds=Decimal("2.10"),
                effective_odds=Decimal("2.10"),
                stake=Decimal("100"),
                cash_outlay=Decimal("100"),
                net_return_if_win=Decimal("210"),
                quote_age_seconds=0,
            ),
            Leg(
                outcome="B",
                bookmaker="Betfair Exchange",
                operator_id="betfair",
                odds=Decimal("2.05"),
                effective_odds=Decimal("2.05"),
                stake=Decimal("102.44"),
                cash_outlay=Decimal("102.44"),
                net_return_if_win=Decimal("210"),
                quote_age_seconds=0,
                source_market_id="1.123",
                source_selection_id="22",
                source_market_version="7",
                available_size=Decimal("1000"),
            ),
        ],
    )


def rescue_quotes() -> list[Quote]:
    return [
        Quote(
            event_id="evt-1",
            source_event_id="bf-event",
            operator_id="betfair",
            sport="football",
            commence_time=NOW + timedelta(hours=2),
            home="A",
            away="B",
            market=MarketType.H2H,
            outcome=outcome,
            bookmaker="Betfair Exchange",
            odds=Decimal(odds),
            expected_outcomes=2,
            observed_at=datetime.now(timezone.utc),
            source="betfair_api_ng",
            source_market_id="1.123",
            source_selection_id=selection,
            source_market_version="8",
            available_size=Decimal("1000"),
        )
        for outcome, odds, selection in [("A", "2.08", "11"), ("B", "2.04", "22")]
    ]


class FakeManual(ExecutionConnector):
    operator_id = "bet365"
    automatic_execution = False

    def place_order(self, order, *, live=False):  # pragma: no cover - coordinator never calls manual connector
        raise AssertionError("manual connector must not be called")


class FakeAuto(ExecutionConnector):
    operator_id = "betfair"
    automatic_execution = True
    mode = "accept"

    def preflight(self, order):
        return ExecutionPreflight(
            operator_id=self.operator_id,
            ok=True,
            message="ok",
            market_open=True,
            current_odds=order.limit_odds,
            available_size=Decimal("1000"),
            market_version="9",
        )

    def place_order(self, order, *, live=False):
        if self.mode == "raise" and not (order.customer_order_ref or "").startswith("rs-"):
            raise TimeoutError("simulated timeout")
        if self.mode == "reject_then_rescue" and not (order.customer_order_ref or "").startswith("rs-"):
            return ExecutionResult(
                operator_id=self.operator_id,
                status=ExecutionStatus.REJECTED,
                message="rejected",
                requested_stake=order.stake,
                requested_odds=order.limit_odds,
                matched_stake=Decimal("0"),
            )
        return ExecutionResult(
            operator_id=self.operator_id,
            status=ExecutionStatus.ACCEPTED,
            message="matched",
            bet_id="bf-1",
            customer_order_ref=order.customer_order_ref,
            requested_stake=order.stake,
            requested_odds=order.limit_odds,
            matched_stake=order.stake,
            average_price_matched=order.limit_odds,
            remaining_stake=Decimal("0"),
        )

    def reconcile_order(self, *, bet_id=None, customer_order_ref=None):
        return ExecutionResult(
            operator_id=self.operator_id,
            status=ExecutionStatus.UNKNOWN,
            message="still unknown",
            bet_id=bet_id,
            customer_order_ref=customer_order_ref,
        )


def install_fakes(monkeypatch, mode="accept"):
    FakeAuto.mode = mode

    def build(operator):
        if operator == "betfair":
            return FakeAuto()
        if operator == "bet365":
            return FakeManual()
        raise KeyError(operator)

    monkeypatch.setattr(coordinator_module, "build_execution_connector", build)


def coordinator(tmp_path, monkeypatch, mode="accept"):
    install_fakes(monkeypatch, mode)
    store = SQLiteStore(tmp_path / "exec.sqlite3")
    policy = ExecutionPolicy(max_rescue_slippage_bps=Decimal("500"), max_rescue_loss=Decimal("10"))
    return store, ExecutionCoordinator(store, policy=policy)


def test_manual_primary_then_fok_hedge_completes(tmp_path, monkeypatch):
    store, coord = coordinator(tmp_path, monkeypatch, "accept")
    try:
        plan = coord.prepare(make_opportunity(), rescue_quotes(), live=True)
        assert coord.exec_store.get_run(plan.execution_id)["status"] == RunStatus.WAITING_MANUAL.value
        result = coord.confirm_manual_leg(plan.execution_id, "L1", accepted=True)
        assert result.status == RunStatus.PREPARED
        result = coord.resume(plan.execution_id, rescue_quotes(), live=True)
        assert result.status == RunStatus.COMPLETED
        assert coord.exec_store.halt_state()[0] is False
    finally:
        store.close()


def test_rejected_hedge_routes_to_rescue(tmp_path, monkeypatch):
    store, coord = coordinator(tmp_path, monkeypatch, "reject_then_rescue")
    try:
        plan = coord.prepare(make_opportunity(), rescue_quotes(), live=True)
        coord.confirm_manual_leg(plan.execution_id, "L1", accepted=True)
        result = coord.resume(plan.execution_id, rescue_quotes(), live=True)
        assert result.status == RunStatus.RESCUED
        assert result.rescue_loss == Decimal("0")
        rescue_rows = [row for row in coord.exec_store.get_legs(plan.execution_id) if row["role"] == "rescue"]
        assert len(rescue_rows) == 1
        assert rescue_rows[0]["status"] == ExecutionStatus.ACCEPTED.value
    finally:
        store.close()


def test_unknown_order_halts_all_new_execution(tmp_path, monkeypatch):
    store, coord = coordinator(tmp_path, monkeypatch, "raise")
    try:
        plan = coord.prepare(make_opportunity(), rescue_quotes(), live=True)
        coord.confirm_manual_leg(plan.execution_id, "L1", accepted=True)
        result = coord.resume(plan.execution_id, rescue_quotes(), live=True)
        assert result.status == RunStatus.EMERGENCY
        halted, reason = coord.exec_store.halt_state()
        assert halted is True
        assert "UNKNOWN" in (reason or "")
        with pytest.raises(RuntimeError, match="globally halted"):
            coord.prepare(make_opportunity(), rescue_quotes(), live=True)
    finally:
        store.close()
