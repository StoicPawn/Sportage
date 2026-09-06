from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arbengine.backtest import BacktestConfig
from arbengine.coverage_backtest import run_coverage_aware_backtest
from arbengine.provider_health_storage import ProviderHealthStore
from arbengine.storage import SQLiteStore


def _add_scan(store: SQLiteStore, when: datetime) -> int:
    scan_id = store.begin_scan("UnifiedOperatorProvider", started_at=when)
    store.finish_scan(scan_id, 0, 0, status="ok", completed_at=when)
    return scan_id


def test_backtest_filters_scans_below_operator_coverage_threshold(tmp_path):
    store = SQLiteStore(tmp_path / "coverage.sqlite3")
    health = ProviderHealthStore(store.conn)
    now = datetime.now(timezone.utc)

    scan1 = _add_scan(store, now)
    scan2 = _add_scan(store, now + timedelta(minutes=1))
    scan3 = _add_scan(store, now + timedelta(minutes=2))

    # scan1 has 2 covered operators, scan2 has 1, scan3 has no health coverage.
    store.conn.executemany(
        """INSERT INTO operator_coverage
        (scan_id, operator_id, quote_count, event_count, market_count, source_count,
         freshest_observed_at, oldest_quote_age_seconds)
        VALUES (?, ?, 3, 1, 1, 1, ?, 0)""",
        [
            (scan1, "bet365", now.isoformat()),
            (scan1, "sisal", now.isoformat()),
            (scan2, "bet365", now.isoformat()),
        ],
    )
    store.conn.commit()

    result, stats = run_coverage_aware_backtest(
        store,
        BacktestConfig(
            initial_bankroll=Decimal("1000"),
            stake_per_opportunity=Decimal("100"),
            start=now - timedelta(seconds=1),
            end=now + timedelta(minutes=3),
        ),
        min_covered_operators=2,
    )

    assert stats.total_scans == 3
    assert stats.eligible_scans == 1
    assert stats.rejected_scans == 2
    assert result.scans == 1
    store.close()


def test_zero_coverage_threshold_preserves_legacy_backtest_behavior(tmp_path):
    store = SQLiteStore(tmp_path / "legacy.sqlite3")
    now = datetime.now(timezone.utc)
    for minute in range(3):
        _add_scan(store, now + timedelta(minutes=minute))

    result, stats = run_coverage_aware_backtest(
        store,
        BacktestConfig(
            initial_bankroll=Decimal("1000"),
            stake_per_opportunity=Decimal("100"),
            start=now - timedelta(seconds=1),
            end=now + timedelta(minutes=3),
        ),
        min_covered_operators=0,
    )

    assert stats.total_scans == 3
    assert stats.eligible_scans == 3
    assert stats.rejected_scans == 0
    assert result.scans == 3
    store.close()
