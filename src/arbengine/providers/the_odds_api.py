from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal

import httpx

from arbengine.models import MarketType, Quote
from arbengine.providers.base import OddsProvider

_MARKET_MAP = {
    "h2h": MarketType.H2H,
    "totals": MarketType.TOTALS,
    "spreads": MarketType.SPREADS,
}


class TheOddsAPIProvider(OddsProvider):
    BASE_URL = "https://api.the-odds-api.com/v4"

    def __init__(
        self,
        api_key: str | None = None,
        sport: str = "upcoming",
        regions: str = "eu",
        markets: str = "h2h,spreads,totals",
        timeout: float = 15.0,
    ) -> None:
        self.api_key = api_key or os.getenv("THE_ODDS_API_KEY")
        if not self.api_key:
            raise ValueError("THE_ODDS_API_KEY is required for TheOddsAPIProvider")
        self.sport = sport
        self.regions = regions
        self.markets = markets
        self.timeout = timeout

    @staticmethod
    def _group_line(market_key: str, outcomes: list[dict]) -> Decimal | None:
        points = [Decimal(str(o["point"])) for o in outcomes if o.get("point") is not None]
        if not points:
            return None
        if market_key == "spreads":
            return max(abs(p) for p in points)
        return points[0]

    def fetch_quotes(self) -> list[Quote]:
        url = f"{self.BASE_URL}/sports/{self.sport}/odds"
        params = {
            "apiKey": self.api_key,
            "regions": self.regions,
            "markets": self.markets,
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        }
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()

        observed_at = datetime.now(timezone.utc)
        result: list[Quote] = []
        for event in payload:
            commence = datetime.fromisoformat(event["commence_time"].replace("Z", "+00:00"))
            for bookmaker in event.get("bookmakers", []):
                for market in bookmaker.get("markets", []):
                    key = market.get("key")
                    market_type = _MARKET_MAP.get(key)
                    if market_type is None:
                        continue
                    outcomes = market.get("outcomes", [])
                    if len(outcomes) < 2:
                        continue
                    market_line = self._group_line(key, outcomes)
                    for outcome in outcomes:
                        outcome_name = outcome["name"]
                        if key == "totals" and outcome.get("point") is not None:
                            outcome_name = f"{outcome_name} {outcome['point']}"
                        result.append(Quote(
                            event_id=event["id"],
                            sport=event.get("sport_key", "unknown"),
                            commence_time=commence,
                            home=event["home_team"],
                            away=event["away_team"],
                            market=market_type,
                            market_line=market_line,
                            outcome=outcome_name,
                            expected_outcomes=len(outcomes),
                            bookmaker=bookmaker.get("title", bookmaker.get("key", "unknown")),
                            odds=Decimal(str(outcome["price"])),
                            observed_at=observed_at,
                            source="the_odds_api",
                        ))
        return result
