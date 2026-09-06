from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import arbengine.connectors.execution as execution_module
from arbengine.connectors.base import (
    BetOrder,
    ExecutionConnector,
    ExecutionPreflight,
    ExecutionResult,
    ExecutionStatus,
)
from arbengine.connectors.execution import HealthTrackedExecutionConnector, build_execution_connector
from arbengine.connectors.execution_health import execution_available, reset_all
from arbengine.execution_coordinator import ExecutionCoordinator, ExecutionPolicy, RunStatus
from arbengine.models import ArbitrageOpportunity, Leg, MarketType, Quote
from arbengine.storage import SQLiteStore


NOW = datetime.now(timezone.utc)


class FakeManual(ExecutionConnector):
    operator_id = "bet365"
    automatic_execution = False

    def place_order(self, order, *, live=False):  # pragma: no cover
        raise AssertionError("manual leg must not be auto-submitted")


class FailingBetfair(ExecutionConnector):
    operator_id = "betfair"
    automatic_execution = True

    def preflight(self, order):
        return ExecutionPreflight(
            operator_id=self.operator_id,
            ok=True,
            message="ok",
            market_open=True,
            current_odds=order.limit_odds,
            available_size=Decimal("1000"),
            market_version="10",
        )

    def place_order(self, order, *, live=False):
        return ExecutionResult(
            operator_id=self.operator_id,
            status=ExecutionStatus.REJECTED,
            message="simulated Betfair rejection",
            requested_stake=order.stake,
            requested_odds=order.limit_odds,
            matched_stake=Decimal("0"),
        )


class HealthyBetflag(ExecutionConnector):
    operator_id = "betflag"
    automatic_execution = True
    submitted = 0

    def preflight(self, order):
        return ExecutionPreflight(
            operator_id=self.operator_id,
            ok=True,
            message="ok",
            market_open=True,
            current_odds=order.limit_odds,
            available_size=Decimal("1000"),
            market_version="11",
        )

    def place_order(self, order, *, live=False):
        type(self).submitted += 1
        return ExecutionResult(
            operator_id=self.operator_id,
            status=ExecutionStatus.ACCEPTED,
            message="BetFlag matched rescue",
            bet_id="bf-flag-1",
            requested_stake=order.stake,
            requested_odds=order.limit_odds,
            matched_stake=order.stake,
            average_price_matched=order.limit_odds,
            remaining_stake=Decimal("0"),
        )


def opportunity() -> ArbitrageOpportunity:
    return ArbitrageOpportunity(
        event_id="evt-independent",
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
                outcome="A", bookmaker="Bet365", operator_id="bet365",
                odds=Decimal("2.10"), effective_odds=Decimal("2.10"),
                stake=Decimal("100"), cash_outlay=Decimal("100"),
                net_return_if_win=Decimal("210"), quote_age_seconds=0,
            ),
            Leg(
                outcome="B", bookmaker="Betfair Exchange", operator_id="betfair",
                odds=Decimal("2.05"), effective_odds=Decimal("2.05"),
                stake=Decimal("102.44"), cash_outlay=Decimal("102.44"),
                net_return_if_win=Decimal("210"), quote_age_seconds=0,
                source_market_id="1.123", source_selection_id="22",
                source_market_version="9", available_size=Decimal("1000"),
            ),
        ],
    )


def quotes() -> list[Quote]:
    result = []
    for operator, bookmaker, source, market_id, version in (
        ("betfair", "Betfair Exchange", "betfair_api_ng", "1.123", "10"),
        ("betflag", "BetFlag", "betflag_exchange_api", "9001", "11"),
    ):
        for outcome, odds, selection in (("A", "2.08", "11"), ("B", "2.04", "22")):
            result.append(
                Quote(
                    event_id="evt-independent",
                    source_event_id=f"{operator}-event",
                    operator_id=operator,
                    sport="football",
                    commence_time=NOW + timedelta(hours=2),
                    home="A",
                    away="B",
                    market=MarketType.H2H,
                    outcome=outcome,
                    bookmaker=bookmaker,
                    odds=Decimal(odds),
                    expected_outcomes=2,
                    observed_at=datetime.now(timezone.utc),
                    source=source,
                    source_market_id=market_id,
                    source_selection_id=selection,
                    source_market_version=version,
                    available_size=Decimal("1000"),
                )
            )
    return result


def install_fake_registry(monkeypatch):
    reset_all()
    HealthyBetflag.submitted = 0
    # These tests isolate circuit-breaker/rescue routing. Certification gating has
    # dedicated tests in test_venue_certification.py and remains enabled by default.
    monkeypatch.setenv("SPORTAGE_REQUIRE_LIVE_CERTIFICATION", "false")
    monkeypatch.setitem(execution_module._EXECUTION_CONNECTORS, "bet365", FakeManual)
    monkeypatch.setitem(execution_module._EXECUTION_CONNECTORS, "betfair", FailingBetfair)
    monkeypatch.setitem(execution_module._EXECUTION_CONNECTORS, "betflag", HealthyBetflag)


def test_failed_automatic_venue_trips_only_its_own_circuit(monkeypatch):
    install_fake_registry(monkeypatch)
    order = BetOrder(
        operator_id="betfair", market_id="1.123", selection_id="22",
        stake=Decimal("10"), limit_odds=Decimal("2.05"),
    )
    connector = build_execution_connector("betfair")
    assert isinstance(connector, HealthTrackedExecutionConnector)
    assert connector.automatic_execution is True
    result = connector.place_order(order, live=True)
    assert result.status == ExecutionStatus.REJECTED
    assert execution_available("betfair") is False
    assert execution_available("betflag") is True
    assert build_execution_connector("betfair").automatic_execution is False
    assert build_execution_connector("betflag").automatic_execution is True
    reset_all()


def test_betfair_failure_rescues_on_betflag(tmp_path, monkeypatch):
    install_fake_registry(monkeypatch)
    store = SQLiteStore(tmp_path / "independent.sqlite3")
    try:
        coord = ExecutionCoordinator(
            store,
            policy=ExecutionPolicy(
                max_rescue_slippage_bps=Decimal("500"),
                max_rescue_loss=Decimal("10"),
            ),
        )
        plan = coord.prepare(opportunity(), quotes(), live=True)
        coord.confirm_manual_leg(plan.execution_id, "L1", accepted=True)
        result = coord.resume(plan.execution_id, quotes(), live=True)
        assert result.status == RunStatus.RESCUED
        rescue_rows = [
            row for row in coord.exec_store.get_legs(plan.execution_id)
            if row["role"] == "rescue"
        ]
        assert len(rescue_rows) == 1
        assert rescue_rows[0]["operator_id"] == "betflag"
        assert rescue_rows[0]["status"] == ExecutionStatus.ACCEPTED.value
        assert HealthyBetflag.submitted == 1
        assert execution_available("betfair") is False
        assert execution_available("betflag") is True
    finally:
        store.close()
        reset_all()
