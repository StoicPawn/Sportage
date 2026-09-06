from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arbengine.control_center import build_control_center_report
from arbengine.market_signals import MarketSignalSnapshot, MarketSignalStore
from arbengine.models import MarketType, Quote
from arbengine.storage import SQLiteStore


def signal(t0, seconds, event_id, status, *, roi="0.015", books=("Bet365", "Sisal")):
    return MarketSignalSnapshot(
        observed_at=t0 + timedelta(seconds=seconds),
        event_id=event_id,
        market_signature="h2h:full_time:",
        status=status,
        gross_implied_sum=Decimal("0.98") if status in {"gross_arbitrage", "net_arbitrage"} else Decimal("1.01"),
        gross_roi=Decimal("0.02") if status in {"gross_arbitrage", "net_arbitrage"} else Decimal("-0.01"),
        net_roi=Decimal(roi) if status == "net_arbitrage" else None,
        bookmaker_count=2,
        outcome_count=2,
        best_legs={
            "A": (books[0], Decimal("2.1")),
            "B": (books[1], Decimal("2.1")),
        },
    )


def quote(t0, event_id, sport, bookmaker, outcome):
    return Quote(
        event_id=event_id,
        sport=sport,
        commence_time=t0 + timedelta(hours=2),
        home="A",
        away="B",
        market=MarketType.H2H,
        outcome=outcome,
        bookmaker=bookmaker,
        odds=Decimal("2.1"),
        expected_outcomes=2,
        observed_at=t0,
        source="test",
    )


def test_control_center_funnel_survival_and_breakdowns(tmp_path):
    t0 = datetime(2026, 9, 6, 8, 0, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "control.sqlite3")
    signals = MarketSignalStore(store.conn)

    snapshots = [
        signal(t0, 0, "evt1", "near_arbitrage"),
        signal(t0, 10, "evt1", "gross_arbitrage"),
        signal(t0, 20, "evt1", "net_arbitrage"),
        signal(t0, 35, "evt1", "net_arbitrage"),
        signal(t0, 50, "evt1", "net_arbitrage", roi="0.025"),
        signal(t0, 0, "evt2", "near_arbitrage"),
        signal(t0, 0, "evt3", "gross_arbitrage"),
        signal(t0, 100, "evt4", "net_arbitrage", books=("Bet365", "Betfair Exchange")),
        signal(t0, 105, "evt4", "net_arbitrage", books=("Bet365", "Betfair Exchange")),
    ]
    for idx, snapshot in enumerate(snapshots, start=1):
        signals.save(idx, [snapshot])

    store.save_quotes(
        [
            quote(t0, "evt1", "football", "Bet365", "A"),
            quote(t0, "evt1", "football", "Sisal", "B"),
            quote(t0, "evt4", "tennis", "Bet365", "A"),
            quote(t0, "evt4", "tennis", "Betfair Exchange", "B"),
        ]
    )

    report = build_control_center_report(store, execution_seconds=15, max_gap_seconds=60)
    store.close()

    assert report.signal_snapshots == 9
    assert report.distinct_markets == 4
    assert report.net_lifecycles == 2
    assert report.funnel.near_or_better == 4
    assert report.funnel.gross_or_better == 3
    assert report.funnel.net_arbitrage == 2
    assert report.funnel.executable == 1
    assert report.median_net_lifetime_seconds == 17.5
    assert report.max_net_lifetime_seconds == 30
    assert report.max_net_roi == Decimal("0.025")

    survival = {point.seconds: point for point in report.survival}
    assert survival[2.0].survivors == 2
    assert survival[5.0].survivors == 2
    assert survival[10.0].survivors == 1
    assert survival[15.0].survival_rate == 0.5
    assert survival[30.0].survivors == 1
    assert survival[60.0].survivors == 0

    sports = {row.label: row for row in report.by_sport}
    assert sports["football"].lifecycles == 1
    assert sports["tennis"].lifecycles == 1

    books = {row.label: row for row in report.by_bookmaker}
    assert books["Bet365"].lifecycles == 2
    assert books["Sisal"].lifecycles == 1
    assert books["Betfair Exchange"].lifecycles == 1


def test_control_center_empty_history(tmp_path):
    store = SQLiteStore(tmp_path / "empty.sqlite3")
    report = build_control_center_report(store)
    store.close()

    assert report.signal_snapshots == 0
    assert report.net_lifecycles == 0
    assert report.funnel.executable == 0
    assert all(point.survival_rate == 0 for point in report.survival)
