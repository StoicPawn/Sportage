from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from arbengine.models import MarketType, Quote
from arbengine.provider_health_storage import ProviderHealthStore
from arbengine.providers.base import OddsProvider
from arbengine.providers.unified import UnifiedOperatorProvider
from arbengine.storage import SQLiteStore


class GoodProvider(OddsProvider):
    def fetch_quotes(self):
        now = datetime.now(timezone.utc)
        common = dict(
            event_id="source-event-1",
            sport="soccer_italy_serie_a",
            commence_time=now + timedelta(hours=2),
            home="Juventus",
            away="Inter",
            market=MarketType.ONE_X_TWO,
            expected_outcomes=3,
            observed_at=now,
            source="odds_api_io",
        )
        return [
            Quote(**common, bookmaker="Bet365", outcome="Juventus", odds=Decimal("2.10")),
            Quote(**common, bookmaker="Bet365", outcome="Draw", odds=Decimal("3.40")),
            Quote(**common, bookmaker="Bet365", outcome="Inter", odds=Decimal("3.70")),
            Quote(**common, bookmaker="Sisal IT", outcome="Juventus", odds=Decimal("2.08")),
            Quote(**common, bookmaker="Sisal IT", outcome="Draw", odds=Decimal("3.45")),
            Quote(**common, bookmaker="Sisal IT", outcome="Inter", odds=Decimal("3.75")),
        ]


class FailingProvider(OddsProvider):
    def fetch_quotes(self):
        raise RuntimeError("upstream unavailable")


def test_partial_source_failure_keeps_healthy_quotes_and_reports_coverage(tmp_path):
    provider = UnifiedOperatorProvider([FailingProvider(), GoodProvider()], max_workers=2)
    report = provider.fetch_report()

    assert report.successful_source_count == 1
    assert report.failed_source_count == 1
    assert report.partial_failure is True
    assert report.covered_operator_ids == {"bet365", "sisal"}
    assert len(report.quotes) == 6

    statuses = {item.source: item.status for item in report.source_health}
    assert statuses["FailingProvider"] == "error"
    assert statuses["GoodProvider"] == "ok"
    assert {item.operator_id for item in report.operator_coverage} == {"bet365", "sisal"}

    store = SQLiteStore(tmp_path / "health.sqlite3")
    scan_id = store.begin_scan("UnifiedOperatorProvider")
    health = ProviderHealthStore(store.conn)
    health.save_report(scan_id, report)

    source_rows = health.latest_source_health()
    coverage_rows = health.latest_operator_coverage()
    store.close()

    assert len(source_rows) == 2
    assert {row["status"] for row in source_rows} == {"ok", "error"}
    assert {row["operator_id"] for row in coverage_rows} == {"bet365", "sisal"}
    assert all(row["quote_count"] == 3 for row in coverage_rows)


def test_fetch_quotes_raises_only_when_every_source_failed():
    provider = UnifiedOperatorProvider([FailingProvider(), FailingProvider()], max_workers=2)
    report = provider.fetch_report()
    assert report.successful_source_count == 0
    assert report.failed_source_count == 2
    assert report.quotes == []

    with pytest.raises(RuntimeError, match="All configured market-data sources failed"):
        provider.fetch_quotes()


def test_latest_coverage_does_not_leak_from_previous_successful_scan(tmp_path):
    store = SQLiteStore(tmp_path / "aligned.sqlite3")
    health = ProviderHealthStore(store.conn)

    good_report = UnifiedOperatorProvider([GoodProvider()]).fetch_report()
    good_scan = store.begin_scan("UnifiedOperatorProvider")
    health.save_report(good_scan, good_report)
    assert health.latest_operator_coverage()

    failed_report = UnifiedOperatorProvider([FailingProvider()]).fetch_report()
    failed_scan = store.begin_scan("UnifiedOperatorProvider")
    health.save_report(failed_scan, failed_report)

    latest_sources = health.latest_source_health()
    latest_coverage = health.latest_operator_coverage()
    store.close()

    assert latest_sources
    assert all(row["scan_id"] == failed_scan for row in latest_sources)
    assert all(row["status"] == "error" for row in latest_sources)
    assert latest_coverage == []
