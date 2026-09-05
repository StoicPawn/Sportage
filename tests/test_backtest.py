from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arbengine.backtest import BacktestConfig, run_backtest
from arbengine.liquidity import LiquidityBook, LiquidityConfig
from arbengine.models import MarketType, Quote, SettlementResult
from arbengine.storage import SQLiteStore


def _add_h2h_scan(
    store: SQLiteStore,
    scan_time: datetime,
    event_id: str,
    commence_time: datetime,
    odds_b: Decimal = Decimal("2.05"),
) -> None:
    sid = store.begin_scan("mock", started_at=scan_time)
    common = dict(
        event_id=event_id,
        sport="tennis",
        commence_time=commence_time,
        home="A",
        away="B",
        market=MarketType.H2H,
        expected_outcomes=2,
        observed_at=scan_time,
    )
    quotes = [
        Quote(**common, outcome="A", bookmaker="book1", odds=Decimal("2.10")),
        Quote(**common, outcome="B", bookmaker="book2", odds=odds_b),
    ]
    store.save_quotes(quotes, scan_id=sid)
    store.finish_scan(sid, len(quotes), 0)


def test_backtest_trades_event_market_once(tmp_path):
    db = tmp_path / "arb.sqlite3"
    store = SQLiteStore(db)
    now = datetime.now(timezone.utc)

    for i in range(2):
        _add_h2h_scan(store, now + timedelta(minutes=i), "e1", now + timedelta(hours=2))

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

    _add_h2h_scan(store, now, "e-persist", now + timedelta(hours=2))
    _add_h2h_scan(store, now + timedelta(seconds=30), "e-persist", now + timedelta(hours=2))

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
        _add_h2h_scan(
            store,
            now + timedelta(seconds=offset),
            "e-reset",
            now + timedelta(hours=2),
            Decimal(b_odds),
        )

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


def test_backtest_waits_for_execution_latency_and_reprices(tmp_path):
    db = tmp_path / "latency.sqlite3"
    store = SQLiteStore(db)
    now = datetime.now(timezone.utc)
    commence = now + timedelta(hours=2)

    _add_h2h_scan(store, now, "e-latency", commence)
    _add_h2h_scan(store, now + timedelta(seconds=30), "e-latency", commence)
    _add_h2h_scan(store, now + timedelta(seconds=60), "e-latency", commence)

    result = run_backtest(
        store,
        BacktestConfig(
            initial_bankroll=Decimal("1000"),
            stake_per_opportunity=Decimal("500"),
            min_net_roi=Decimal("0.01"),
            execution_latency_seconds=60,
            start=now - timedelta(seconds=1),
            end=now + timedelta(minutes=2),
        ),
    )
    store.close()

    assert len(result.trades) == 1
    assert result.trades[0].detected_at == now + timedelta(seconds=60)
    assert result.signals_rejected_for_latency == 2


def test_backtest_enforces_concurrent_bookmaker_liquidity(tmp_path):
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


def test_results_mode_settles_actual_bookmaker_wallets(tmp_path):
    db = tmp_path / "results.sqlite3"
    store = SQLiteStore(db)
    now = datetime.now(timezone.utc)
    commence = now + timedelta(minutes=1)

    _add_h2h_scan(store, now, "e-result", commence)
    result_record = SettlementResult(
        event_id="e-result",
        market_signature="h2h:full_time:",
        winning_outcome="A",
        settled_at=now + timedelta(seconds=70),
        source="test",
    )
    store.save_settlement_result(result_record)

    # A later empty scan advances the replay clock beyond settlement.
    later_scan = store.begin_scan("mock", started_at=now + timedelta(minutes=2))
    store.finish_scan(later_scan, 0, 0)

    liquidity = LiquidityBook(
        LiquidityConfig(
            default_balance=Decimal("0"),
            bookmakers={"book1": Decimal("500"), "book2": Decimal("500")},
        )
    )
    result = run_backtest(
        store,
        BacktestConfig(
            initial_bankroll=Decimal("1000"),
            stake_per_opportunity=Decimal("500"),
            min_net_roi=Decimal("0.01"),
            settlement_mode="results",
            enforce_bookmaker_liquidity=True,
            settlement_hours=0,
            start=now - timedelta(seconds=1),
            end=now + timedelta(minutes=3),
        ),
        liquidity_book=liquidity,
    )

    stored = store.get_settlement_result("e-result", "h2h:full_time:")
    store.close()

    assert stored is not None and stored.winning_outcome == "A"
    assert len(result.trades) == 1
    assert result.trades[0].winning_outcome == "A"
    assert result.locked_capital == 0
    assert result.projected_profit > 0
    assert result.ending_balance_by_bookmaker["book1"] > Decimal("500")
    assert result.ending_balance_by_bookmaker["book2"] < Decimal("500")
    assert sum(result.ending_balance_by_bookmaker.values()) > Decimal("1000")


def test_results_mode_rejects_trade_without_exact_result(tmp_path):
    db = tmp_path / "missing-result.sqlite3"
    store = SQLiteStore(db)
    now = datetime.now(timezone.utc)
    _add_h2h_scan(store, now, "e-no-result", now + timedelta(hours=2))

    result = run_backtest(
        store,
        BacktestConfig(
            initial_bankroll=Decimal("1000"),
            stake_per_opportunity=Decimal("500"),
            min_net_roi=Decimal("0.01"),
            settlement_mode="results",
            start=now - timedelta(seconds=1),
            end=now + timedelta(minutes=1),
        ),
    )
    store.close()

    assert len(result.trades) == 0
    assert result.signals_rejected_for_missing_result == 1
