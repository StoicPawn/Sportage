from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import arbengine.live_readiness as readiness_module
import arbengine.venue_certification as certification_module
from arbengine.connectors.base import (
    BetOrder,
    ExecutionConnector,
    ExecutionPreflight,
    ExecutionResult,
    ExecutionStatus,
)
from arbengine.connectors.execution import HealthTrackedExecutionConnector
from arbengine.live_readiness import LiveReadinessError, assert_live_readiness
from arbengine.models import ArbitrageOpportunity, Leg, MarketType, Quote
from arbengine.storage import SQLiteStore
from arbengine.venue_certification import (
    CertificationCheck,
    VenueCertification,
    VenueCertificationStore,
    VenueCertifier,
)


NOW = datetime.now(timezone.utc)


def _quote(operator_id: str, outcome: str, selection: str) -> Quote:
    return Quote(
        event_id="evt-cert",
        source_event_id=f"{operator_id}-evt",
        operator_id=operator_id,
        sport="football",
        commence_time=NOW + timedelta(hours=2),
        home="A",
        away="B",
        market=MarketType.H2H,
        outcome=outcome,
        bookmaker="BetFlag" if operator_id == "betflag" else "Betfair Exchange",
        odds=Decimal("2.05"),
        expected_outcomes=2,
        observed_at=datetime.now(timezone.utc),
        source=f"{operator_id}_direct",
        source_market_id=f"{operator_id}-market",
        source_selection_id=selection,
        source_market_version="7",
        available_size=Decimal("100"),
    )


def _opportunity() -> ArbitrageOpportunity:
    return ArbitrageOpportunity(
        event_id="evt-cert",
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
                source_market_id="betfair-market",
                source_selection_id="22",
                source_market_version="7",
                available_size=Decimal("100"),
            ),
        ],
    )


class FakeProvider:
    def fetch_quotes(self):
        return [_quote("betfair", "A", "11"), _quote("betfair", "B", "22")]


class FakeExec(ExecutionConnector):
    operator_id = "betfair"
    automatic_execution = True

    def preflight(self, order):
        return ExecutionPreflight(
            operator_id=self.operator_id,
            ok=True,
            message="probe ok",
            market_open=True,
            current_odds=order.limit_odds,
            available_size=Decimal("100"),
            market_version="8",
        )

    def place_order(self, order, *, live=False):
        return ExecutionResult(
            operator_id=self.operator_id,
            status=ExecutionStatus.ACCEPTED,
            message="fake accepted",
            requested_stake=order.stake,
            requested_odds=order.limit_odds,
            matched_stake=order.stake,
        )


@pytest.fixture
def db_store(tmp_path):
    store = SQLiteStore(tmp_path / "cert.sqlite3")
    try:
        yield store
    finally:
        store.close()


def _record_valid(store: VenueCertificationStore, operator_id: str, environment: str) -> None:
    now = datetime.now(timezone.utc)
    store.record(
        VenueCertification(
            operator_id=operator_id,
            environment=environment,
            certified_at=now,
            expires_at=now + timedelta(hours=24),
            success=True,
            checks=[CertificationCheck(name="test", ok=True, message="ok")],
        )
    )


def test_certifier_persists_success_and_expiry(db_store, monkeypatch):
    monkeypatch.setenv("BETFAIR_APP_KEY", "app")
    monkeypatch.setenv("BETFAIR_SESSION_TOKEN", "session")
    monkeypatch.setattr(
        certification_module,
        "_read_only_auth_probe",
        lambda operator_id, connector, market_id=None: {"ok": True, "message": "auth ok"},
    )
    store = VenueCertificationStore(db_store.conn)
    certifier = VenueCertifier(
        store,
        provider_factory=lambda operator, environment: FakeProvider(),
        execution_factory=lambda operator, environment: FakeExec(),
    )

    report = certifier.certify("betfair", ttl_hours=12)

    assert report.success is True
    assert {check.name for check in report.checks} == {
        "credentials",
        "direct_market_data",
        "authenticated_session",
        "execution_preflight",
    }
    assert store.valid("betfair", "production") is True
    assert store.latest("betfair", "production") is not None


def test_certification_fails_closed_when_credentials_missing(db_store, monkeypatch):
    monkeypatch.delenv("BETFAIR_APP_KEY", raising=False)
    monkeypatch.delenv("BETFAIR_SESSION_TOKEN", raising=False)
    store = VenueCertificationStore(db_store.conn)
    report = VenueCertifier(store).certify("betfair")
    assert report.success is False
    assert store.valid("betfair", "production") is False


def test_live_readiness_requires_independent_certified_rescue(db_store, monkeypatch):
    monkeypatch.setenv("BETFLAG_ENVIRONMENT", "staging")
    monkeypatch.setattr(
        readiness_module,
        "_automatic",
        lambda operator_id: operator_id in {"betfair", "betflag"},
    )
    certifications = VenueCertificationStore(db_store.conn)
    _record_valid(certifications, "betfair", "production")
    _record_valid(certifications, "betflag", "staging")
    quotes = [_quote("betflag", "A", "1"), _quote("betflag", "B", "2")]

    rescue = assert_live_readiness(_opportunity(), quotes, certifications)
    assert rescue == {"betfair": ["betflag"]}


def test_live_readiness_rejects_missing_independent_certification(db_store, monkeypatch):
    monkeypatch.setenv("BETFLAG_ENVIRONMENT", "staging")
    monkeypatch.setattr(
        readiness_module,
        "_automatic",
        lambda operator_id: operator_id in {"betfair", "betflag"},
    )
    certifications = VenueCertificationStore(db_store.conn)
    _record_valid(certifications, "betfair", "production")
    quotes = [_quote("betflag", "A", "1"), _quote("betflag", "B", "2")]

    with pytest.raises(LiveReadinessError, match="No independent certified rescue venue"):
        assert_live_readiness(_opportunity(), quotes, certifications)


def test_live_connector_gate_blocks_without_certification(tmp_path, monkeypatch):
    db = tmp_path / "gate.sqlite3"
    monkeypatch.setenv("ARB_DB_PATH", str(db))
    monkeypatch.setenv("SPORTAGE_REQUIRE_LIVE_CERTIFICATION", "true")
    inner = FakeExec()
    connector = HealthTrackedExecutionConnector(inner)
    order = BetOrder(
        operator_id="betfair",
        market_id="m",
        selection_id="s",
        stake=Decimal("5"),
        limit_odds=Decimal("2.0"),
    )

    blocked = connector.place_order(order, live=True)
    assert blocked.status == ExecutionStatus.REJECTED
    assert "certification" in blocked.message.lower()

    store = SQLiteStore(db)
    try:
        _record_valid(VenueCertificationStore(store.conn), "betfair", "production")
    finally:
        store.close()

    connector = HealthTrackedExecutionConnector(FakeExec())
    accepted = connector.place_order(order, live=True)
    assert accepted.status == ExecutionStatus.ACCEPTED
