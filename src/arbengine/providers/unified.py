from __future__ import annotations

import hashlib
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from time import perf_counter

from arbengine.connectors.betfair import BetfairExchangeMarketDataConnector
from arbengine.models import MarketType, Quote
from arbengine.normalizer import canonical_name
from arbengine.operators import OPERATORS, canonical_operator_id
from arbengine.providers.base import OddsProvider
from arbengine.providers.health import OperatorCoverage, ProviderFetchReport, SourceHealth
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
    return quote.model_copy(
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


def _source_label(source: OddsProvider) -> str:
    return getattr(source, "source_name", source.__class__.__name__)


class UnifiedOperatorProvider(OddsProvider):
    """Merge multiple market-data sources into one canonical quote stream.

    Upstreams are fetched concurrently and isolated from each other. A failed source
    is recorded in the fetch report without discarding healthy-source data. Duplicate
    operator/event/market/outcome quotes prefer direct/official data and then freshness.
    """

    def __init__(self, sources: list[OddsProvider], max_workers: int | None = None) -> None:
        if not sources:
            raise ValueError("UnifiedOperatorProvider requires at least one source")
        self.sources = sources
        configured = int(os.getenv("SPORTAGE_PROVIDER_WORKERS", "4"))
        self.max_workers = max(1, min(max_workers or configured, len(sources)))
        self.last_report: ProviderFetchReport | None = None

    @staticmethod
    def _fetch_one(source: OddsProvider) -> tuple[list[Quote], dict[str, object]]:
        started_at = datetime.now(timezone.utc)
        started_perf = perf_counter()
        try:
            raw = list(source.fetch_quotes())
            completed_at = datetime.now(timezone.utc)
            return raw, {
                "source": _source_label(source),
                "status": "ok",
                "started_at": started_at,
                "completed_at": completed_at,
                "duration_ms": (perf_counter() - started_perf) * 1000.0,
                "error_type": None,
                "error_message": None,
            }
        except Exception as exc:  # source failure must not poison healthy sources
            completed_at = datetime.now(timezone.utc)
            return [], {
                "source": _source_label(source),
                "status": "error",
                "started_at": started_at,
                "completed_at": completed_at,
                "duration_ms": (perf_counter() - started_perf) * 1000.0,
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:2000],
            }

    def fetch_report(self) -> ProviderFetchReport:
        fetched_at = datetime.now(timezone.utc)
        order = {id(source): idx for idx, source in enumerate(self.sources)}
        fetched: list[tuple[int, list[Quote], dict[str, object]]] = []

        with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="sportage-feed") as pool:
            futures = {pool.submit(self._fetch_one, source): source for source in self.sources}
            for future in as_completed(futures):
                source = futures[future]
                raw, meta = future.result()
                fetched.append((order[id(source)], raw, meta))

        fetched.sort(key=lambda item: item[0])
        normalized_by_source: list[tuple[list[Quote], dict[str, object]]] = []
        source_health: list[SourceHealth] = []

        for _, raw_quotes, meta in fetched:
            normalized = [quote for raw in raw_quotes if (quote := normalize_quote(raw)) is not None]
            operator_ids = {q.operator_id for q in normalized if q.operator_id}
            source_health.append(
                SourceHealth(
                    source=str(meta["source"]),
                    status=str(meta["status"]),
                    started_at=meta["started_at"],  # type: ignore[arg-type]
                    completed_at=meta["completed_at"],  # type: ignore[arg-type]
                    duration_ms=float(meta["duration_ms"]),
                    raw_quote_count=len(raw_quotes),
                    normalized_quote_count=len(normalized),
                    operator_count=len(operator_ids),
                    error_type=meta["error_type"] if isinstance(meta["error_type"], str) else None,
                    error_message=meta["error_message"] if isinstance(meta["error_message"], str) else None,
                )
            )
            normalized_by_source.append((normalized, meta))

        best: dict[tuple[str, str, str, str], Quote] = {}
        for normalized, _ in normalized_by_source:
            for quote in normalized:
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

        quotes = sorted(
            best.values(),
            key=lambda q: (q.commence_time, q.event_id, q.market_signature, q.operator_id or "", q.outcome),
        )

        by_operator: dict[str, list[Quote]] = defaultdict(list)
        for quote in quotes:
            if quote.operator_id:
                by_operator[quote.operator_id].append(quote)

        coverage: list[OperatorCoverage] = []
        for operator_id, items in sorted(by_operator.items()):
            freshest = max((q.observed_at for q in items), default=None)
            oldest = min((q.observed_at for q in items), default=fetched_at)
            coverage.append(
                OperatorCoverage(
                    operator_id=operator_id,
                    quote_count=len(items),
                    event_count=len({q.event_id for q in items}),
                    market_count=len({(q.event_id, q.market_signature) for q in items}),
                    source_count=len({q.source for q in items}),
                    freshest_observed_at=freshest,
                    oldest_quote_age_seconds=max(0.0, (fetched_at - oldest).total_seconds()),
                )
            )

        report = ProviderFetchReport(
            quotes=quotes,
            source_health=source_health,
            operator_coverage=coverage,
            fetched_at=fetched_at,
        )
        self.last_report = report
        return report

    def fetch_quotes(self) -> list[Quote]:
        report = self.fetch_report()
        if report.successful_source_count == 0:
            details = "; ".join(
                f"{item.source}: {item.error_type or 'error'} {item.error_message or ''}".strip()
                for item in report.source_health
            )
            raise RuntimeError(f"All configured market-data sources failed. {details}")
        return report.quotes


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
