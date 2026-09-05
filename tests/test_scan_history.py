from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arbengine.models import MarketType, Quote
from arbengine.scan_history import ScanHistory
from arbengine.storage import SQLiteStore


def _quote(now: datetime) -> Quote:
    return Quote(
        event_id="event-1",
        sport="tennis",
        commence_time=now + timedelta(hours=2),
        home="A",
        away="B",
        market=MarketType.H2H,
        outcome="A",
        bookmaker="Book A",
        odds=Decimal("2.10"),
        expected_outcomes=2,
        observed_at=now,
        source="test",
    )


def test_scan_history_persists_successful_snapshot(tmp_path):
    store = SQLiteStore(tmp_path / "history.sqlite3")
    history = ScanHistory(store)
    now = datetime.now(timezone.utc)

    session = history.start("MockProvider")
    session.save_quotes([_quote(now)])
    receipt = session.complete()

    row = store.get_scan(receipt.scan_id)
    quotes = store.load_quotes_for_scan(receipt.scan_id)
    store.close()

    assert row is not None
    assert row["status"] == "ok"
    assert row["quote_count"] == 1
    assert row["opportunity_count"] == 0
    assert row["duration_ms"] is not None
    assert len(quotes) == 1


def test_scan_history_keeps_quotes_when_processing_fails(tmp_path):
    store = SQLiteStore(tmp_path / "failed.sqlite3")
    history = ScanHistory(store)
    now = datetime.now(timezone.utc)

    session = history.start("BrokenProvider")
    session.save_quotes([_quote(now)])
    receipt = session.fail(RuntimeError("engine exploded"))

    row = store.get_scan(receipt.scan_id)
    quotes = store.load_quotes_for_scan(receipt.scan_id)
    store.close()

    assert row is not None
    assert row["status"] == "error"
    assert row["quote_count"] == 1
    assert row["error_type"] == "RuntimeError"
    assert "engine exploded" in row["error_message"]
    assert len(quotes) == 1


def test_identical_quotes_are_kept_across_scans(tmp_path):
    store = SQLiteStore(tmp_path / "persistence.sqlite3")
    history = ScanHistory(store)
    now = datetime.now(timezone.utc)
    quote = _quote(now)

    first = history.start("MockProvider")
    first.save_quotes([quote])
    first.complete()

    second = history.start("MockProvider")
    second.save_quotes([quote])
    second.complete()

    quote_rows = store.conn.execute("SELECT COUNT(*) c FROM quote_snapshots").fetchone()["c"]
    scan_rows = store.conn.execute("SELECT COUNT(*) c FROM scan_runs").fetchone()["c"]
    store.close()

    assert quote_rows == 2
    assert scan_rows == 2
