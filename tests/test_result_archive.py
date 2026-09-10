from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arbengine.models import MarketType, Quote
from arbengine.result_archive import run_result_cycle
from arbengine.storage import SQLiteStore


def test_betfair_closed_market_populates_event_and_settlement_tables(tmp_path, monkeypatch):
    db = tmp_path / "sportage.sqlite3"
    store = SQLiteStore(db)
    commence = datetime.now(timezone.utc) - timedelta(hours=2)
    quotes = [
        Quote(
            event_id="evt_test",
            source_event_id="native_event",
            operator_id="betfair",
            sport="football",
            commence_time=commence,
            home="Home FC",
            away="Away FC",
            market=MarketType.ONE_X_TWO,
            outcome="HOME FC",
            bookmaker="Betfair Exchange",
            odds=Decimal("2.1"),
            source="betfair_api_ng",
            source_market_id="1.2345",
            source_selection_id="11",
        ),
        Quote(
            event_id="evt_test",
            source_event_id="native_event",
            operator_id="betfair",
            sport="football",
            commence_time=commence,
            home="Home FC",
            away="Away FC",
            market=MarketType.ONE_X_TWO,
            outcome="AWAY FC",
            bookmaker="Betfair Exchange",
            odds=Decimal("3.2"),
            source="betfair_api_ng",
            source_market_id="1.2345",
            source_selection_id="22",
        ),
        Quote(
            event_id="evt_test",
            source_event_id="native_event",
            operator_id="betfair",
            sport="football",
            commence_time=commence,
            home="Home FC",
            away="Away FC",
            market=MarketType.ONE_X_TWO,
            outcome="DRAW",
            bookmaker="Betfair Exchange",
            odds=Decimal("3.4"),
            source="betfair_api_ng",
            source_market_id="1.2345",
            source_selection_id="33",
        ),
    ]
    scan_id = store.begin_scan("betfair")
    store.save_quotes(quotes, scan_id)
    store.finish_scan(scan_id, quote_count=3, opportunity_count=0)
    store.close()

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def call(self, method, params):
            assert method == "listMarketBook"
            assert params["marketIds"] == ["1.2345"]
            return {
                "result": [
                    {
                        "marketId": "1.2345",
                        "status": "CLOSED",
                        "runners": [
                            {"selectionId": 11, "status": "WINNER"},
                            {"selectionId": 22, "status": "LOSER"},
                            {"selectionId": 33, "status": "LOSER"},
                        ],
                    }
                ]
            }

    monkeypatch.setattr("arbengine.result_archive.session_token", lambda: "session")
    monkeypatch.setattr("arbengine.result_archive._BetfairClient", FakeClient)
    monkeypatch.setenv("BETFAIR_APP_KEY", "delayed-key")

    result = run_result_cycle(db)
    assert result["event_results"] == 1
    assert result["settled_markets"] == 1

    store = SQLiteStore(db)
    event = store.conn.execute("SELECT * FROM event_results WHERE event_id='evt_test'").fetchone()
    settlement = store.conn.execute("SELECT * FROM settlement_results WHERE event_id='evt_test'").fetchone()
    store.close()
    assert event["winning_outcome"] == "HOME FC"
    assert event["status"] == "settled"
    assert event["home_score"] is None
    assert settlement["winning_outcome"] == "HOME FC"
