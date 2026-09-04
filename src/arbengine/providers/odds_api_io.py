from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import httpx

from arbengine.models import MarketType, Quote
from arbengine.providers.base import OddsProvider


class OddsApiIoProvider(OddsProvider):
    """Free-first odds-api.io adapter with cached event discovery and batch odds."""

    BASE_URL = "https://api.odds-api.io/v3"

    def __init__(
        self,
        api_key: str | None = None,
        sport: str | None = None,
        league: str | None = None,
        bookmakers: str | None = None,
        event_limit: int | None = None,
        events_cache_ttl_seconds: float = 900.0,
        timeout: float = 15.0,
    ) -> None:
        self.api_key = api_key or os.getenv("ODDS_API_IO_KEY")
        if not self.api_key:
            raise ValueError("ODDS_API_IO_KEY is required for OddsApiIoProvider")
        self.sport = sport or os.getenv("ODDS_API_IO_SPORT", "football")
        self.league = league or os.getenv("ODDS_API_IO_LEAGUE", "italy-serie-a")
        self.bookmakers = bookmakers or os.getenv("ODDS_API_IO_BOOKMAKERS", "Bet365,Unibet")
        self.event_limit = min(10, event_limit or int(os.getenv("ODDS_API_IO_EVENT_LIMIT", "10")))
        self.events_cache_ttl_seconds = events_cache_ttl_seconds
        self.timeout = timeout
        self._events_cache: list[dict] = []
        self._events_cache_at = 0.0

    def _get_events(self, client: httpx.Client) -> list[dict]:
        now_mono = time.monotonic()
        if self._events_cache and now_mono - self._events_cache_at < self.events_cache_ttl_seconds:
            return self._events_cache
        response = client.get(
            f"{self.BASE_URL}/events",
            params={
                "apiKey": self.api_key,
                "sport": self.sport,
                "league": self.league,
                "status": "pending",
                "limit": self.event_limit,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("Unexpected odds-api.io /events response")
        self._events_cache = payload[: self.event_limit]
        self._events_cache_at = now_mono
        return self._events_cache

    @staticmethod
    def _decimal(value: object) -> Decimal | None:
        if value is None:
            return None
        try:
            odds = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        return odds if odds > 1 else None

    @staticmethod
    def _parse_time(value: str | None, fallback: datetime) -> datetime:
        if not value:
            return fallback
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    def _parse_event(self, event: dict, observed_at: datetime) -> list[Quote]:
        result: list[Quote] = []
        home = str(event.get("home", "Home"))
        away = str(event.get("away", "Away"))
        event_id = str(event.get("id"))
        commence = self._parse_time(event.get("date"), observed_at)
        sport_obj = event.get("sport") or {}
        sport = sport_obj.get("slug") if isinstance(sport_obj, dict) else str(sport_obj)
        sport = sport or self.sport
        bookmakers = event.get("bookmakers") or {}
        if not isinstance(bookmakers, dict):
            return result

        for bookmaker, markets in bookmakers.items():
            if not isinstance(markets, list):
                continue
            for market in markets:
                name = str(market.get("name", "")).strip().lower()
                if name not in {"ml", "moneyline", "1x2"}:
                    continue
                market_updated = self._parse_time(market.get("updatedAt"), observed_at)
                for row in market.get("odds") or []:
                    if not isinstance(row, dict):
                        continue
                    home_odds = self._decimal(row.get("home"))
                    draw_odds = self._decimal(row.get("draw"))
                    away_odds = self._decimal(row.get("away"))
                    expected = 3 if draw_odds is not None else 2
                    market_type = MarketType.ONE_X_TWO if expected == 3 else MarketType.H2H
                    for outcome, odds in [(home, home_odds), ("Draw", draw_odds), (away, away_odds)]:
                        if odds is None:
                            continue
                        result.append(Quote(
                            event_id=event_id,
                            sport=sport,
                            commence_time=commence,
                            home=home,
                            away=away,
                            market=market_type,
                            outcome=outcome,
                            bookmaker=str(bookmaker),
                            odds=odds,
                            expected_outcomes=expected,
                            observed_at=market_updated,
                            source="odds_api_io",
                        ))
        return result

    def fetch_quotes(self) -> list[Quote]:
        observed_at = datetime.now(timezone.utc)
        with httpx.Client(timeout=self.timeout) as client:
            events = self._get_events(client)
            ids = [str(e.get("id")) for e in events if e.get("id") is not None]
            if not ids:
                return []
            response = client.get(
                f"{self.BASE_URL}/odds/multi",
                params={
                    "apiKey": self.api_key,
                    "eventIds": ",".join(ids),
                    "bookmakers": self.bookmakers,
                },
            )
            response.raise_for_status()
            payload = response.json()

        if isinstance(payload, dict):
            payload = [payload]
        if not isinstance(payload, list):
            raise ValueError("Unexpected odds-api.io /odds/multi response")
        result: list[Quote] = []
        for event in payload:
            if isinstance(event, dict):
                result.extend(self._parse_event(event, observed_at))
        return result
