from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import arbengine.account_funds as funds_module
from arbengine.account_funds import (
    AccountSnapshotStore,
    LiveFundingError,
    assert_live_funding,
)
from arbengine.connectors.base import (
    AccountSnapshot,
    BetOrder,
    BetSide,
    ExecutionConnector,
    ExecutionResult,
    ExecutionStatus,
    required_account_funds,
)
from arbengine.connectors.execution import HealthTrackedExecutionConnector
from arbengine.connectors.execution_health import reset_all
from arbengine.costs import CostBook
from arbengine.models import ArbitrageOpportunity, Leg, MarketType, Quote
from arbengine.storage import SQLiteStore


NOW = datetime.now(timezone.utc)


def opportunity() -> ArbitrageOpportunity:
    return ArbitrageOpportunity(
        event_id="evt-funds",
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
                odds=Decimal("2.10"), effective_odds=Decimal("2.10"), stake=Decimal("100"),
                cash_outlay=Decimal("100"), net_return_if_win=Decimal("210"), quote_age_seconds=0,
            ),
            Leg(
                outcome="B", bookmaker="Betfair Exchange", operator_id="betfair",
                odds=Decimal("2.05"), effective_odds=Decimal("2.05"), stake=Decimal("102.44"),
                cash_outlay=Decimal("102.44"), net_return_if_win=Decimal("210"), quote_age_seconds=0,
                source_market_id="bf-m", source_selection_id="22", source_market_version="1",
                available_size=Decimal("1000"),
            ),
        ],
    )


def rescue_quotes() -> list[Quote]:
    return [
        Quote(
            event_id="evt-funds", source_event_id="flag-event", operator_id="betflag",
            sport="football", commence_time=NOW + timedelta(hours=2), home="A", away="B",
            market=MarketType.H2H, outcome=outcome, bookmaker="BetFlag", odds=Decimal(odds),
            expected_outcomes=2, observed_at=datetime.now(timezone.utc), source="betflag_exchange_api",
            source_market_id="9001", source_selection_id=selection, source_market_version="2",
            available_size=Decimal("1000"),
        )
        for outcome, odds, selection in (("A", "2.08", "1"), ("B", "2.04", "2"))
    ]


def snapshot(operator: str, amount: str) -> AccountSnapshot:
    return AccountSnapshot(
        operator_id=operator,
        environment="production",
        available_balance=Decimal(amount),
        total_balance=Decimal(amount),
    )


def test_required_account_funds_uses_lay_liability():
    back = BetOrder(
        operator_id="betfair", market_id="m", selection_id="1",
        stake=Decimal("10"), limit_odds=Decimal("3"), side=BetSide.BACK,
    )
    lay = back.model_copy(update={"side": BetSide.LAY})
    assert required_account_funds(back) == Decimal("10")
    assert required_account_funds(lay) == Decimal("20")


def test_account_snapshot_round_trip(tmp_path):
    store = SQLiteStore(tmp_path / "funds.sqlite3")
    try:
        snapshots = AccountSnapshotStore(store.conn)
        original = AccountSnapshot(
            operator_id="betfair", environment="production", available_balance=Decimal("321.45"),
            exposure=Decimal("-20"), retained_commission=Decimal("1.25"), raw={"source": "test"},
        )
        snapshots.save(original)
        loaded = snapshots.latest("betfair", "production")
        assert loaded is not None
        assert loaded.available_balance == Decimal("321.45")
        assert loaded.exposure == Decimal("-20")
        assert loaded.raw == {"source": "test"}
    finally:
        store.close()


def test_live_funding_reserves_independent_venue(monkeypatch):
    monkeypatch.setenv("SPORTAGE_RESCUE_BALANCE_BUFFER_PCT", "0.10")
    balances = {"betfair": snapshot("betfair", "500"), "betflag": snapshot("betflag", "500")}
    monkeypatch.setattr(funds_module, "refresh_account_snapshot", lambda op, store=None: balances[op])

    plan = assert_live_funding(
        opportunity(), rescue_quotes(), {"betfair": ["betflag"]},
        cost_book=CostBook(), max_quote_age_seconds=10,
        max_rescue_slippage_bps=Decimal("100"),
    )
    assert plan.rescue_routes == {"betfair": "betflag"}
    by_id = {row.operator_id: row for row in plan.venues}
    assert by_id["betfair"].planned_requirement == Decimal("102.44")
    assert by_id["betflag"].rescue_requirement > Decimal("100")
    assert by_id["betflag"].rescue_requirement < Decimal("130")


def test_live_funding_rejects_underfunded_rescue(monkeypatch):
    balances = {"betfair": snapshot("betfair", "500"), "betflag": snapshot("betflag", "50")}
    monkeypatch.setattr(funds_module, "refresh_account_snapshot", lambda op, store=None: balances[op])
    with pytest.raises(LiveFundingError, match="enough free balance"):
        assert_live_funding(
            opportunity(), rescue_quotes(), {"betfair": ["betflag"]},
            cost_book=CostBook(), max_quote_age_seconds=10,
            max_rescue_slippage_bps=Decimal("100"),
        )


class FakeAuto(ExecutionConnector):
    operator_id = "betfair"
    automatic_execution = True

    def place_order(self, order, *, live=False):
        return ExecutionResult(
            operator_id=self.operator_id, status=ExecutionStatus.ACCEPTED, message="accepted",
            requested_stake=order.stake, requested_odds=order.limit_odds, matched_stake=order.stake,
        )


def test_live_connector_rechecks_funds_immediately_before_order(monkeypatch):
    monkeypatch.setenv("SPORTAGE_REQUIRE_LIVE_CERTIFICATION", "false")
    monkeypatch.setenv("SPORTAGE_REQUIRE_ACCOUNT_FUNDS", "true")
    order = BetOrder(
        operator_id="betfair", market_id="m", selection_id="1",
        stake=Decimal("100"), limit_odds=Decimal("2"),
    )

    monkeypatch.setattr(funds_module, "refresh_account_snapshot", lambda op, store=None: snapshot(op, "20"))
    connector = HealthTrackedExecutionConnector(FakeAuto())
    blocked = connector.place_order(order, live=True)
    assert blocked.status == ExecutionStatus.REJECTED
    assert "balance" in blocked.message.lower() or "funds" in blocked.message.lower()

    reset_all()
    monkeypatch.setattr(funds_module, "refresh_account_snapshot", lambda op, store=None: snapshot(op, "200"))
    connector = HealthTrackedExecutionConnector(FakeAuto())
    accepted = connector.place_order(order, live=True)
    assert accepted.status == ExecutionStatus.ACCEPTED
    reset_all()
