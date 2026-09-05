from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone

from arbengine.connectors.betfair import BetfairExchangeMarketDataConnector
from arbengine.models import MarketType, Quote
from arbengine.normalizer import canonical_name
from arbengine.operators import OPERATORS, canonical_operator_id
from arbengine.providers.base import OddsProvider
from arbengine.providers.odds_api_io import OddsApiIoProvider
from arbengine.providers.the_odds_api import TheOddsAPIProvider


_SOURCE_PRIORITY = {
    "betfair_api_ng": 100,
    "the_odds_api": 50,
    "odds_api_io": 40,
}


def _sport_family(value: str) -> str:
    key = canonical_name(value)
    if any(token in key for token in ("soccer", "football", "calcio")):
        return "football"
    if "tennis" in key:
        return "tennis"
    if "basket" in key:
        return "basketball"
    if "baseball" in key:
        return "baseball"
    if "hockey" in key:
        return "hockey"
    return key or "unknown"


def _canonical_event_id(quote: Quote) -> str:
    participants = sorted((canonical_name(quote.home), canonical_name(quote.away)))
    # Five-minute buckets absorb small source timestamp differences while retaining
    # the original source timestamp and source event id for audit/debugging.
    epoch_bucket = int(quote.commence_time.timestamp() // 300)
    raw = "|".join([_sport_family(quote.sport), *participants, str(epoch_bucket)])
    digest = hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()[:20]
    return f"evt_{digest}"


def _canonical_outcome(quote: Quote) -> str:
    key = canonical_name(quote.outcome)
    if key in {"draw", "the draw", "x", "pareggio"}:
        return "DRAW"
    return key.upper()


def normalize_quote(quote: Quote) -> Quote | None:
    operator_id = quote.operator_id or canonical_operator_id(quote.bookmaker)
    if operator_id not in OPERATORS:
        return None
    expected = quote.expected_outcomes
    market = quote.market
    if market in {MarketType.H2H, MarketType.MONEYLINE, MarketType.ONE_X_TWO}:
        market = MarketType.ONE_X_TWO if expected == 3 else MarketType.H2H
    source_event_id = quote.source_event_id or quote.event_id
    normalized = quote.model_copy(
        update={
            "source_event_id": source_event_id,
            "event_id": _canonical_event_id(quote),
            "operator_id": operator_id,
            "bookmaker": OPERATORS[operator_id].display_name,
            "sport": _sport_family(quote.sport),
            "market": market,
            "outcome": _canonical_outcome(quote),
        }
    )
    return normalized


class UnifiedOperatorProvider(OddsProvider):
    """Merge multiple sources into one canonical Sportage quote language.

    Each upstream is fetched once per scan. Unknown/non-approved operator aliases are
    dropped. When the same operator/market/outcome is supplied by multiple sources,
    the higher-priority official/direct source wins, then the freshest observation.
    """

    def __init__(self, sources: list[OddsProvider]) -> None:
        if not sources:
            raise ValueError("UnifiedOperatorProvider requires at least one source")
        self.sources = sources

    def fetch_quotes(self) -> list[Quote]:
        best: dict[tuple[str, str, str, str], Quote] = {}
        for source in self.sources:
            for raw in source.fetch_quotes():
                quote = normalize_quote(raw)
                if quote is None:
                    continue
                key = (
                    quote.event_id,
                    quote.market_signature,
                    quote.operator_id or quote.bookmaker,
                    quote.outcome,
                )
                previous = best.get(key)
                if previous is None:
                    best[key] = quote
                    continue
                new_rank = (_SOURCE_PRIORITY.get(quote.source, 0), quote.observed_at)
                old_rank = (_SOURCE_PRIORITY.get(previous.source, 0), previous.observed_at)
                if new_rank > old_rank:
                    best[key] = quote
        return sorted(
            best.values(),
            key=lambda q: (q.commence_time, q.event_id, q.market_signature, q.operator_id or "", q.outcome),
        )


def build_unified_provider() -> UnifiedOperatorProvider:
    sources: list[OddsProvider] = []
    if os.getenv("THE_ODDS_API_KEY"):
        sources.append(TheOddsAPIProvider())
    if os.getenv("ODDS_API_IO_KEY"):
        sources.append(OddsApiIoProvider())
    if os.getenv("BETFAIR_APP_KEY") and os.getenv("BETFAIR_SESSION_TOKEN"):
        sources.append(BetfairExchangeMarketDataConnector())
    if not sources:
        raise ValueError(
            "No market-data source configured. Set THE_ODDS_API_KEY, ODDS_API_IO_KEY, "
            "or BETFAIR_APP_KEY + BETFAIR_SESSION_TOKEN."
        )
    return UnifiedOperatorProvider(sources)
