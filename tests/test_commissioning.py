from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from arbengine.canary import CanaryGuard
from arbengine.commissioning import (
    CommissioningCheck,
    CommissioningError,
    CommissioningReport,
    CommissioningStore,
    assert_current_commissioning,
    run_commissioning,
    safety_config_fingerprint,
)
from arbengine.connectors.base import AccountSnapshot
from arbengine.models import MarketType, Quote
from arbengine.storage import SQLiteStore
from arbengine.venue_certification import (
    CertificationCheck,
    VenueCertification,
    VenueCertificationStore,
)


NOW = datetime.now(timezone.utc)
START = NOW + timedelta(hours=2)


def _safety_env(monkeypatch) -> None:
    monkeypatch.setenv("SPORTAGE_REQUIRE_COMMISSIONING", "true")
    monkeypatch.setenv("SPORTAGE_CANARY_MODE", "true")
    monkeypatch.setenv("SPORTAGE_REQUIRE_LIVE_CERTIFICATION", "true")
    monkeypatch.setenv("SPORTAGE_REQUIRE_ACCOUNT_FUNDS", "true")
    monkeypatch.setenv("SPORTAGE_LIVE_EXECUTION", "false")
    monkeypatch.setenv("BETFLAG_ENVIRONMENT", "production")
    monkeypatch.setenv("SPORTAGE_RESCUE_BALANCE_BUFFER_PCT", "0.10")


def _record_certifications(conn) -> None:
    store = VenueCertificationStore(conn)
    now = datetime.now(timezone.utc)
    for operator in ("betfair", "betflag"):
        store.record(
            VenueCertification(
                operator_id=operator,
                environment="production",
                certified_at=now,
                expires_at=now + timedelta(hours=12),
                success=True,
                checks=[CertificationCheck(name="test", ok=True, message="ok")],
            )
        )


def _account(operator: str, store=None) -> AccountSnapshot:
    snapshot = AccountSnapshot(
        operator_id=operator,
        environment="production",
        available_balance=Decimal("20"),
        total_balance=Decimal("20"),
        locked_balance=Decimal("0"),
        exposure=Decimal("0"),
    )
    if store is not None:
        store.save(snapshot)
    return snapshot


def _quote(operator: str, outcome: str, selection: str, *, home: str = "Alpha", away: str = "Beta") -> Quote:
    return Quote(
        event_id=f"{operator}-source-event",
        source_event_id=f"{operator}-source-event",
        operator_id=operator,
        sport="football",
        commence_time=START,
        home=home,
        away=away,
        market=MarketType.H2H,
        outcome=outcome,
        bookmaker="Betfair Exchange" if operator == "betfair" else "BetFlag",
        odds=Decimal("2.05"),
        expected_outcomes=2,
        observed_at=datetime.now(timezone.utc),
        source="betfair_api_ng" if operator == "betfair" else "betflag_exchange_api",
        source_market_id=f"{operator}-market",
        source_selection_id=selection,
        source_market_version="10",
        available_size=Decimal("100"),
    )


class FakeProvider:
    def __init__(self, quotes):
        self.quotes = quotes

    def fetch_quotes(self):
        return list(self.quotes)


def _providers(operator: str):
    return FakeProvider([
        _quote(operator, "Alpha", "1"),
        _quote(operator, "Beta", "2"),
    ])


def test_successful_commissioning_persists_and_config_change_invalidates(tmp_path, monkeypatch):
    _safety_env(monkeypatch)
    store = SQLiteStore(tmp_path / "commission.sqlite3")
    try:
        _record_certifications(store.conn)
        report = run_commissioning(
            store.conn,
            ttl_minutes=30,
            refresh_certifications=False,
            provider_factory=_providers,
            account_fetcher=_account,
        )
        assert report.success is True
        assert {check.name for check in report.checks} == {
            "master_switch_off",
            "safety_configuration",
            "clean_execution_state",
            "production_venue_certification",
            "flat_funded_accounts",
            "cross_exchange_market_mapping",
        }
        commissioning = CommissioningStore(store.conn)
        assert commissioning.valid_current() is True
        assert_current_commissioning(store.conn)

        monkeypatch.setenv("SPORTAGE_RESCUE_BALANCE_BUFFER_PCT", "0.20")
        assert commissioning.valid_current() is False
        with pytest.raises(CommissioningError, match="configuration changed"):
            assert_current_commissioning(store.conn)
    finally:
        store.close()


def test_commissioning_fails_locally_for_betflag_staging(tmp_path, monkeypatch):
    _safety_env(monkeypatch)
    monkeypatch.setenv("BETFLAG_ENVIRONMENT", "staging")
    store = SQLiteStore(tmp_path / "staging.sqlite3")
    try:
        report = run_commissioning(
            store.conn,
            refresh_certifications=False,
            provider_factory=_providers,
            account_fetcher=_account,
        )
        assert report.success is False
        checks = {check.name: check for check in report.checks}
        assert checks["safety_configuration"].ok is False
        assert checks["remote_checks"].ok is False
    finally:
        store.close()


def test_commissioning_rejects_cross_exchange_mapping_mismatch(tmp_path, monkeypatch):
    _safety_env(monkeypatch)
    store = SQLiteStore(tmp_path / "mapping.sqlite3")
    try:
        _record_certifications(store.conn)

        def mismatched(operator: str):
            if operator == "betfair":
                return _providers(operator)
            return FakeProvider([
                _quote(operator, "Gamma", "1", home="Gamma", away="Delta"),
                _quote(operator, "Delta", "2", home="Gamma", away="Delta"),
            ])

        report = run_commissioning(
            store.conn,
            refresh_certifications=False,
            provider_factory=mismatched,
            account_fetcher=_account,
        )
        assert report.success is False
        mapping = next(check for check in report.checks if check.name == "cross_exchange_market_mapping")
        assert mapping.ok is False
        assert mapping.details["common_executable_markets"] == 0
    finally:
        store.close()


def test_live_plan_gate_requires_current_commissioning_even_if_canary_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("SPORTAGE_REQUIRE_COMMISSIONING", "true")
    monkeypatch.setenv("SPORTAGE_CANARY_MODE", "false")
    store = SQLiteStore(tmp_path / "gate.sqlite3")
    try:
        guard = CanaryGuard(store.conn)
        with pytest.raises(CommissioningError, match="no commissioning certificate"):
            guard.assert_plan({"legs": []})

        now = datetime.now(timezone.utc)
        CommissioningStore(store.conn).record(
            CommissioningReport(
                started_at=now,
                completed_at=now,
                expires_at=now + timedelta(minutes=30),
                success=True,
                config_fingerprint=safety_config_fingerprint(),
                checks=[CommissioningCheck(name="test", ok=True, message="ok")],
            )
        )
        # Canary is disabled, but commissioning has passed, so the plan reaches the
        # canary bypass instead of being rejected for missing commissioning.
        guard.assert_plan({"legs": []})
    finally:
        store.close()


def test_failed_latest_commissioning_invalidates_previous_success(tmp_path, monkeypatch):
    monkeypatch.setenv("SPORTAGE_REQUIRE_COMMISSIONING", "true")
    store = SQLiteStore(tmp_path / "latest.sqlite3")
    try:
        now = datetime.now(timezone.utc)
        fingerprint = safety_config_fingerprint()
        commissioning = CommissioningStore(store.conn)
        commissioning.record(
            CommissioningReport(
                started_at=now,
                completed_at=now,
                expires_at=now + timedelta(minutes=30),
                success=True,
                config_fingerprint=fingerprint,
                checks=[CommissioningCheck(name="first", ok=True, message="ok")],
            )
        )
        commissioning.record(
            CommissioningReport(
                started_at=now + timedelta(seconds=1),
                completed_at=now + timedelta(seconds=1),
                expires_at=now + timedelta(seconds=1),
                success=False,
                config_fingerprint=fingerprint,
                checks=[CommissioningCheck(name="second", ok=False, message="failed")],
            )
        )
        assert commissioning.valid_current(fingerprint=fingerprint) is False
        with pytest.raises(CommissioningError, match="latest commissioning run failed"):
            assert_current_commissioning(store.conn)
    finally:
        store.close()
