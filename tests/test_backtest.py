from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arbengine.backtest import BacktestConfig, run_backtest
from arbengine.models import MarketType, Quote
from arbengine.storage import SQLiteStore


def test_backtest_trades_event_market_once(tmp_path):
    db = tmp_path / "arb.sqlite3"
    store = SQLiteStore(db)
    now = datetime.now(timezone.utc)

    for i in range(2):
        scan_time = now + timedelta(minutes=i)
        sid = store.begin_scan("mock", started_at=scan_time)
        common = dict(
            event_id="e1",
            sport="tennis",
            commence_time=now + timedelta(hours=2),
            home="A",
            away="B",
            market=MarketType.H2H,
            expected_outcomes=2,
            observed_at=scan_time,
        )
        quotes = [
            Quote(**common, outcome="A", bookmaker="book1", odds=Decimal("2.10")),
            Quote(**common, outcome="B", bookmaker="book2", odds=Decimal("2.05")),
        ]
        store.save_quotes(quotes, scan_id=sid)
        store.finish_scan(sid, len(quotes), 0)

    result = run_backtest(
        store,
        BacktestConfig(
            initial_bankroll=Decimal("1000"),
            stake_per_opportunity=Decimal("500"),
            min_net_roi=Decimal("0.01"),
            start=now - timedelta(minutes=1),
            end=now + timedelta(minutes=5),
        ),
    )
    store.close()

    assert len(result.trades) == 1
    assert result.projected_profit > 0
    assert result.signals_seen == 2


def test_backtest_requires_signal_persistence(tmp_path):
    db = tmp_path / "persistence.sqlite3"
    store = SQLiteStore(db)
    now = datetime.now(timezone.utc)

    def add_scan(scan_time: datetime, has_arb: bool) -> None:
        sid = store.begin_scan("mock", started_at=scan_time)
        common = dict(
            event_id="e-persist",
            sport="tennis",
            commence_time=now + timedelta(hours=2),
            home="A",
            away="B",
            market=MarketType.H2H,
            expected_outcomes=2,
            observed_at=scan_time,
        )
        odds_b = Decimal("2.05") if has_arb else Decimal("1.80")
        quotes = [
            Quote(**common, outcome="A", bookmaker="book1", odds=Decimal("2.10")),
            Quote(**common, outcome="B", bookmaker="book2", odds=odds_b),
        ]
        store.save_quotes(quotes, scan_id=sid)
        store.finish_scan(sid, len(quotes), 0)

    add_scan(now, True)
    add_scan(now + timedelta(seconds=30), True)

    result = run_backtest(
        store,
        BacktestConfig(
            initial_bankroll=Decimal("1000"),
            stake_per_opportunity=Decimal("500"),
            min_net_roi=Decimal("0.01"),
            min_signal_persistence_seconds=Decimal("30"),
            start=now - timedelta(seconds=1),
            end=now + timedelta(minutes=1),
        ),
    )
    store.close()

    assert len(result.trades) == 1
    assert result.trades[0].persistence_seconds == 30
    assert result.signals_rejected_for_persistence == 1


def test_backtest_resets_persistence_when_signal_disappears(tmp_path):
    db = tmp_path / "persistence-reset.sqlite3"
    store = SQLiteStore(db)
    now = datetime.now(timezone.utc)

    for offset, b_odds in [(0, "2.05"), (30, "1.80"), (60, "2.05")]:
        scan_time = now + timedelta(seconds=offset)
        sid = store.begin_scan("mock", started_at=scan_time)
        common = dict(
            event_id="e-reset",
            sport="tennis",
            commence_time=now + timedelta(hours=2),
            home="A",
            away="B",
            market=MarketType.H2H,
            expected_outcomes=2,
            observed_at=scan_time,
        )
        quotes = [
            Quote(**common, outcome="A", bookmaker="book1", odds=Decimal("2.10")),
            Quote(**common, outcome="B", bookmaker="book2", odds=Decimal(b_odds)),
        ]
        store.save_quotes(quotes, scan_id=sid)
        store.finish_scan(sid, len(quotes), 0)

    result = run_backtest(
        store,
        BacktestConfig(
            initial_bankroll=Decimal("1000"),
            stake_per_opportunity=Decimal("500"),
            min_net_roi=Decimal("0.01"),
            min_signal_persistence_seconds=30,
            start=now - timedelta(seconds=1),
            end=now + timedelta(minutes=2),
        ),
    )
    store.close()

    assert len(result.trades) == 0
    assert result.signals_seen == 2


def test_backtest_enforces_concurrent_bookmaker_liquidity(tmp_path):
    from arbengine.liquidity import LiquidityBook, LiquidityConfig

    db = tmp_path / "liquidity.sqlite3"
    store = SQLiteStore(db)
    now = datetime.now(timezone.utc)
    sid = store.begin_scan("mock", started_at=now)
    quotes = []
    for event_id in ["e-liq-1", "e-liq-2"]:
        common = dict(
            event_id=event_id,
            sport="tennis",
            commence_time=now + timedelta(hours=2),
            home=f"{event_id}-A",
            away=f"{event_id}-B",
            market=MarketType.H2H,
            expected_outcomes=2,
            observed_at=now,
        )
        quotes.extend([
            Quote(**common, outcome=f"{event_id}-A", bookmaker="book1", odds=Decimal("2.10")),
            Quote(**common, outcome=f"{event_id}-B", bookmaker="book2", odds=Decimal("2.05")),
        ])
    store.save_quotes(quotes, scan_id=sid)
    store.finish_scan(sid, len(quotes), 0)

    liquidity = LiquidityBook(
        LiquidityConfig(
            default_balance=Decimal("0"),
            bookmakers={"book1": Decimal("240"), "book2": Decimal("1000")},
        )
    )
    result = run_backtest(
        store,
        BacktestConfig(
            initial_bankroll=Decimal("1000"),
            stake_per_opportunity=Decimal("500"),
            min_net_roi=Decimal("0.01"),
            enforce_bookmaker_liquidity=True,
            start=now - timedelta(seconds=1),
            end=now + timedelta(minutes=1),
        ),
        liquidity_book=liquidity,
    )
    store.close()

    assert len(result.trades) == 1
    assert result.signals_rejected_for_liquidity == 1
    assert result.peak_locked_outlay_by_bookmaker["book1"] <= Decimal("240")
    assert result.turnover_by_bookmaker["book1"] > 0
