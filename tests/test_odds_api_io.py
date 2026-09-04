from datetime import datetime, timezone
from decimal import Decimal

from arbengine.models import MarketType
from arbengine.providers.odds_api_io import OddsApiIoProvider


def test_parse_moneyline_event():
    provider = object.__new__(OddsApiIoProvider)
    provider.sport = "football"
    now = datetime.now(timezone.utc)
    payload = {
        "id": 123456,
        "home": "Inter",
        "away": "Juventus",
        "date": "2026-09-10T18:45:00Z",
        "sport": {"name": "Football", "slug": "football"},
        "bookmakers": {
            "Bet365": [{
                "name": "ML",
                "updatedAt": "2026-09-05T00:00:00Z",
                "odds": [{"home": "2.10", "draw": "3.40", "away": "3.80"}],
            }]
        },
    }
    quotes = provider._parse_event(payload, now)
    assert len(quotes) == 3
    assert {q.market for q in quotes} == {MarketType.ONE_X_TWO}
    assert {q.expected_outcomes for q in quotes} == {3}
    assert max(q.odds for q in quotes) == Decimal("3.80")
