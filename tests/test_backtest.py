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
            event_id="e1", sport="tennis", commence_time=now + timedelta(hours=2),
            home="A", away="B", market=MarketType.H2H, expected_outcomes=2,
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
