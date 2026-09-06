import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arbengine.market_signals import (
    MarketSignalSnapshot,
    MarketSignalStore,
    build_lifecycles,
    build_market_signals,
    summarize_lifecycles,
)
from arbengine.models import MarketType, Quote


def q(now, bookmaker, outcome, odds):
    return Quote(
        event_id="evt-test",
        sport="football",
        commence_time=now + timedelta(hours=2),
        home="A",
        away="B",
        market=MarketType.H2H,
        outcome=outcome,
        bookmaker=bookmaker,
        odds=Decimal(odds),
        expected_outcomes=2,
        observed_at=now,
        source="test",
    )


def test_build_market_signals_detects_gross_and_near_arbitrage():
    now = datetime(2026, 9, 6, 8, 0, tzinfo=timezone.utc)
    gross = build_market_signals(
        [q(now, "Bet365", "A", "2.05"), q(now, "Sisal", "B", "2.05")],
        [],
        observed_at=now,
    )
    assert len(gross) == 1
    assert gross[0].status == "gross_arbitrage"
    assert gross[0].gross_roi > 0

    near = build_market_signals(
        [q(now, "Bet365", "A", "1.99"), q(now, "Sisal", "B", "1.99")],
        [],
        observed_at=now,
        near_gap=Decimal("0.02"),
    )
    assert near[0].status == "near_arbitrage"
    assert near[0].gross_roi < 0


def test_lifecycle_splits_after_gap_and_summarizes_duration():
    t0 = datetime(2026, 9, 6, 8, 0, tzinfo=timezone.utc)

    def snap(seconds):
        return MarketSignalSnapshot(
            observed_at=t0 + timedelta(seconds=seconds),
            event_id="evt",
            market_signature="h2h:full_time:",
            status="net_arbitrage",
            gross_implied_sum=Decimal("0.98"),
            gross_roi=Decimal("0.02"),
            net_roi=Decimal("0.015"),
            bookmaker_count=2,
            outcome_count=2,
            best_legs={"A": ("Bet365", Decimal("2.1")), "B": ("Sisal", Decimal("2.1"))},
        )

    lifecycles = build_lifecycles([snap(0), snap(20), snap(40), snap(200), snap(220)], max_gap_seconds=60)
    assert len(lifecycles) == 2
    assert lifecycles[0].duration_seconds == 40
    assert lifecycles[1].duration_seconds == 20

    summary = summarize_lifecycles(lifecycles)
    assert summary.lifecycles == 2
    assert summary.median_duration_seconds == 30
    assert summary.max_duration_seconds == 40
    assert summary.max_net_roi == Decimal("0.015")


def test_signal_store_round_trip():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    store = MarketSignalStore(conn)
    now = datetime(2026, 9, 6, 8, 0, tzinfo=timezone.utc)
    signals = build_market_signals(
        [q(now, "Bet365", "A", "2.05"), q(now, "Sisal", "B", "2.05")],
        [],
        observed_at=now,
    )
    store.save(1, signals)
    loaded = store.list(status="gross_arbitrage")
    assert len(loaded) == 1
    assert loaded[0].event_market_key == signals[0].event_market_key
    assert loaded[0].best_legs == signals[0].best_legs
