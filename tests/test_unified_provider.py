from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arbengine.models import MarketType, Quote
from arbengine.providers.base import OddsProvider
from arbengine.providers.unified import UnifiedOperatorProvider
from arbengine.storage import SQLiteStore


class StaticProvider(OddsProvider):
    def __init__(self, quotes):
        self.quotes = quotes

    def fetch_quotes(self):
        return list(self.quotes)


def test_unified_provider_merges_same_event_across_sources(tmp_path):
    now = datetime.now(timezone.utc)
    commence_a = now + timedelta(hours=2)
    commence_b = commence_a + timedelta(seconds=75)

    q1 = Quote(
        event_id="source-a-123",
        sport="soccer_italy_serie_a",
        commence_time=commence_a,
        home="Juventus",
        away="Inter",
        market=MarketType.H2H,
        outcome="Juventus",
        bookmaker="Bet365",
        odds=Decimal("2.10"),
        expected_outcomes=3,
        observed_at=now,
        source="the_odds_api",
    )
    q2 = Quote(
        event_id="1.999999",
        sport="Soccer",
        commence_time=commence_b,
        home="Inter",
        away="Juventus",
        market=MarketType.ONE_X_TWO,
        outcome="The Draw",
        bookmaker="Betfair Exchange EU",
        odds=Decimal("3.50"),
        expected_outcomes=3,
        observed_at=now,
        source="betfair_api_ng",
    )

    provider = UnifiedOperatorProvider([StaticProvider([q1]), StaticProvider([q2])])
    quotes = provider.fetch_quotes()

    assert len(quotes) == 2
    assert quotes[0].event_id == quotes[1].event_id
    assert {q.source_event_id for q in quotes} == {"source-a-123", "1.999999"}
    assert {q.operator_id for q in quotes} == {"bet365", "betfair"}
    assert all(q.market == MarketType.ONE_X_TWO for q in quotes)
    assert {q.sport for q in quotes} == {"football"}
    assert any(q.outcome == "DRAW" for q in quotes)

    store = SQLiteStore(tmp_path / "unified.sqlite3")
    scan_id = store.begin_scan("unified", started_at=now)
    store.save_quotes(quotes, scan_id=scan_id)
    store.finish_scan(scan_id, len(quotes), 0)
    rows = store.conn.execute(
        "SELECT operator_id, source_event_id FROM quote_snapshots ORDER BY id"
    ).fetchall()
    store.close()
    assert {row["operator_id"] for row in rows} == {"bet365", "betfair"}
    assert {row["source_event_id"] for row in rows} == {"source-a-123", "1.999999"}
