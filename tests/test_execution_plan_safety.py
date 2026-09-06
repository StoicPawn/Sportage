from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import arbengine.execution_coordinator as coordinator_module
from arbengine.connectors.base import ExecutionConnector, ExecutionResult, ExecutionStatus
from arbengine.execution_coordinator import ExecutionCoordinator, ExecutionPolicy
from arbengine.models import ArbitrageOpportunity, Leg, MarketType
from arbengine.storage import SQLiteStore


class ManualConnector(ExecutionConnector):
    automatic_execution = False

    def __init__(self, operator_id: str):
        self.operator_id = operator_id

    def place_order(self, order, *, live=False):
        return ExecutionResult(
            operator_id=self.operator_id,
            status=ExecutionStatus.MANUAL_REQUIRED,
            message="manual",
        )


class AutoConnector(ExecutionConnector):
    operator_id = "betfair"
    automatic_execution = True

    def place_order(self, order, *, live=False):
        return ExecutionResult(
            operator_id=self.operator_id,
            status=ExecutionStatus.DRY_RUN,
            message="dry",
        )


def test_prepare_rejects_more_than_one_manual_primary(tmp_path, monkeypatch):
    def build(operator: str):
        if operator == "betfair":
            return AutoConnector()
        return ManualConnector(operator)

    monkeypatch.setattr(coordinator_module, "build_execution_connector", build)
    now = datetime.now(timezone.utc)
    legs = [
        Leg(
            outcome="A",
            bookmaker="Bet365",
            operator_id="bet365",
            odds=Decimal("3.20"),
            effective_odds=Decimal("3.20"),
            stake=Decimal("100"),
            cash_outlay=Decimal("100"),
            net_return_if_win=Decimal("320"),
            quote_age_seconds=0,
        ),
        Leg(
            outcome="DRAW",
            bookmaker="Sisal",
            operator_id="sisal",
            odds=Decimal("3.40"),
            effective_odds=Decimal("3.40"),
            stake=Decimal("94"),
            cash_outlay=Decimal("94"),
            net_return_if_win=Decimal("319.60"),
            quote_age_seconds=0,
        ),
        Leg(
            outcome="B",
            bookmaker="Betfair Exchange",
            operator_id="betfair",
            odds=Decimal("3.30"),
            effective_odds=Decimal("3.30"),
            stake=Decimal("97"),
            cash_outlay=Decimal("97"),
            net_return_if_win=Decimal("320.10"),
            quote_age_seconds=0,
            source_market_id="1.999",
            source_selection_id="33",
            source_market_version="5",
        ),
    ]
    opportunity = ArbitrageOpportunity(
        event_id="evt-3way",
        sport="football",
        event="A vs B",
        commence_time=now + timedelta(hours=2),
        market=MarketType.ONE_X_TWO,
        gross_implied_sum=Decimal("0.93"),
        gross_roi=Decimal("0.075"),
        net_roi=Decimal("0.02"),
        capital_available=Decimal("1000"),
        capital_used=Decimal("291"),
        unallocated_cash=Decimal("709"),
        guaranteed_payout=Decimal("319.60"),
        gross_guaranteed_profit=Decimal("28.60"),
        guaranteed_profit=Decimal("5.82"),
        estimated_costs=Decimal("22.78"),
        legs=legs,
    )

    store = SQLiteStore(tmp_path / "safety.sqlite3")
    try:
        coordinator = ExecutionCoordinator(
            store,
            policy=ExecutionPolicy(require_rescue_venue=False),
        )
        with pytest.raises(ValueError, match="more than one manual primary"):
            coordinator.prepare(opportunity, [], live=True)
        assert store.conn.execute("SELECT COUNT(*) FROM execution_runs").fetchone()[0] == 0
        assert store.conn.execute("SELECT COUNT(*) FROM execution_locks").fetchone()[0] == 0
    finally:
        store.close()
