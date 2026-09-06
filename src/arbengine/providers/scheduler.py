from __future__ import annotations

import json
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from time import perf_counter

from pydantic import BaseModel, Field

from arbengine.connectors.betfair import BetfairExchangeMarketDataConnector
from arbengine.models import Quote
from arbengine.providers.base import OddsProvider
from arbengine.providers.health import OperatorCoverage, ProviderFetchReport, SourceHealth
from arbengine.providers.odds_api_io import OddsApiIoProvider
from arbengine.providers.the_odds_api import TheOddsAPIProvider
from arbengine.providers.unified import normalize_quote
from arbengine.scheduler_storage import SchedulerBudgetStore


_SOURCE_PRIORITY = {
    "betfair_api_ng": 100,
    "the_odds_api": 50,
    "odds_api_io": 40,
}


class SourceSchedulePolicy(BaseModel):
    enabled: bool = True
    base_interval_seconds: float = Field(default=300.0, gt=0)
    near_event_interval_seconds: float = Field(default=120.0, gt=0)
    hot_interval_seconds: float = Field(default=30.0, gt=0)
    error_retry_seconds: float = Field(default=60.0, gt=0)
    daily_call_limit: int | None = Field(default=None, gt=0)
    monthly_unit_limit: float | None = Field(default=None, gt=0)
    units_per_call: float = Field(default=1.0, gt=0)
    max_cache_age_seconds: float = Field(default=900.0, gt=0)


class SchedulerConfig(BaseModel):
    tick_seconds: float = Field(default=5.0, gt=0)
    near_event_window_seconds: float = Field(default=7200.0, gt=0)
    hot_implied_gap: float = Field(default=0.02, ge=0)
    max_workers: int = Field(default=3, ge=1, le=16)
    sources: dict[str, SourceSchedulePolicy]


@dataclass
class ScheduledSource:
    source: str
    policy: SourceSchedulePolicy
    provider: OddsProvider | None
    unavailable_reason: str | None = None


@dataclass(frozen=True)
class SchedulerSnapshot:
    mode: str
    nearest_event_seconds: float | None
    best_implied_gap: float | None


def load_scheduler_config(path: str | Path) -> SchedulerConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return SchedulerConfig.model_validate(payload)


def _merge_quotes(quotes: list[Quote]) -> list[Quote]:
    best: dict[tuple[str, str, str, str], Quote] = {}
    for quote in quotes:
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


def _coverage(quotes: list[Quote], now: datetime) -> list[OperatorCoverage]:
    by_operator: dict[str, list[Quote]] = defaultdict(list)
    for quote in quotes:
        if quote.operator_id:
            by_operator[quote.operator_id].append(quote)
    result: list[OperatorCoverage] = []
    for operator_id, items in sorted(by_operator.items()):
        freshest = max((q.observed_at for q in items), default=None)
        oldest = min((q.observed_at for q in items), default=now)
        result.append(
            OperatorCoverage(
                operator_id=operator_id,
                quote_count=len(items),
                event_count=len({q.event_id for q in items}),
                market_count=len({(q.event_id, q.market_signature) for q in items}),
                source_count=len({q.source for q in items}),
                freshest_observed_at=freshest,
                oldest_quote_age_seconds=max(0.0, (now - oldest).total_seconds()),
            )
        )
    return result


def _best_implied_gap(quotes: list[Quote]) -> float | None:
    groups: dict[tuple[str, str], list[Quote]] = defaultdict(list)
    for quote in quotes:
        groups[(quote.event_id, quote.market_signature)].append(quote)
    best_gap: Decimal | None = None
    for group in groups.values():
        expected = max((q.expected_outcomes for q in group), default=0)
        outcomes = {q.outcome for q in group}
        if expected < 2 or len(outcomes) != expected:
            continue
        implied = Decimal("0")
        complete = True
        for outcome in outcomes:
            prices = [q.odds for q in group if q.outcome == outcome]
            if not prices:
                complete = False
                break
            implied += Decimal("1") / max(prices)
        if not complete:
            continue
        gap = implied - Decimal("1")
        best_gap = gap if best_gap is None else min(best_gap, gap)
    return None if best_gap is None else float(best_gap)


class AdaptiveScheduledProvider(OddsProvider):
    """Budget-aware provider hub with source-specific adaptive polling.

    The outer shadow loop may tick frequently; each upstream is called only when its
    own schedule says it is due and its configured call/credit budget permits it.
    Cached quotes retain their original observed_at timestamp, so the arbitrage
    engine can still reject stale data independently of scheduler cache retention.
    """

    def __init__(
        self,
        config: SchedulerConfig,
        sources: list[ScheduledSource],
        budget_store: SchedulerBudgetStore | None = None,
    ) -> None:
        self.config = config
        self.sources = sources
        self.tick_seconds = config.tick_seconds
        self.budget_store = budget_store or SchedulerBudgetStore()
        self.cache: dict[str, list[Quote]] = {item.source: [] for item in sources}
        self.last_report: ProviderFetchReport | None = None
        self.last_snapshot = SchedulerSnapshot("base", None, None)

    def attach_budget_connection(self, conn) -> None:
        self.budget_store = SchedulerBudgetStore(conn)

    def _fresh_cache(self, item: ScheduledSource, now: datetime) -> list[Quote]:
        cutoff = item.policy.max_cache_age_seconds
        fresh = [
            q for q in self.cache.get(item.source, [])
            if -5.0 <= (now - q.observed_at).total_seconds() <= cutoff
        ]
        self.cache[item.source] = fresh
        return fresh

    def _market_snapshot(self, now: datetime) -> SchedulerSnapshot:
        all_cached: list[Quote] = []
        for item in self.sources:
            all_cached.extend(self._fresh_cache(item, now))
        merged = _merge_quotes(all_cached)
        future_seconds = [
            (q.commence_time - now).total_seconds()
            for q in merged
            if q.commence_time > now
        ]
        nearest = min(future_seconds) if future_seconds else None
        gap = _best_implied_gap(merged)
        if gap is not None and gap <= self.config.hot_implied_gap:
            mode = "hot"
        elif nearest is not None and nearest <= self.config.near_event_window_seconds:
            mode = "near_event"
        else:
            mode = "base"
        return SchedulerSnapshot(mode, nearest, gap)

    @staticmethod
    def _interval(policy: SourceSchedulePolicy, mode: str) -> float:
        if mode == "hot":
            return policy.hot_interval_seconds
        if mode == "near_event":
            return policy.near_event_interval_seconds
        return policy.base_interval_seconds

    @staticmethod
    def _fetch_one(item: ScheduledSource) -> tuple[list[Quote], float, Exception | None]:
        started = perf_counter()
        try:
            assert item.provider is not None
            raw = list(item.provider.fetch_quotes())
            normalized = [quote for value in raw if (quote := normalize_quote(value)) is not None]
            return normalized, (perf_counter() - started) * 1000.0, None
        except Exception as exc:
            return [], (perf_counter() - started) * 1000.0, exc

    def fetch_report(self, now: datetime | None = None) -> ProviderFetchReport:
        now = now or datetime.now(timezone.utc)
        before = self._market_snapshot(now)
        due: list[ScheduledSource] = []
        health: dict[str, SourceHealth] = {}

        for item in self.sources:
            cached = self._fresh_cache(item, now)
            if not item.policy.enabled:
                health[item.source] = SourceHealth(
                    item.source, "disabled", now, now, 0.0,
                    normalized_quote_count=len(cached),
                    operator_count=len({q.operator_id for q in cached if q.operator_id}),
                )
                continue
            if item.provider is None:
                health[item.source] = SourceHealth(
                    item.source, "unconfigured", now, now, 0.0,
                    normalized_quote_count=len(cached),
                    operator_count=len({q.operator_id for q in cached if q.operator_id}),
                    error_type="MissingCredentials",
                    error_message=item.unavailable_reason,
                )
                continue

            state = self.budget_store.state(item.source, now)
            if state.next_due_at is not None and now < state.next_due_at:
                health[item.source] = SourceHealth(
                    item.source, "cached", now, now, 0.0,
                    raw_quote_count=0,
                    normalized_quote_count=len(cached),
                    operator_count=len({q.operator_id for q in cached if q.operator_id}),
                )
                continue

            allowed, reason = self.budget_store.can_spend(
                item.source,
                units=item.policy.units_per_call,
                daily_call_limit=item.policy.daily_call_limit,
                monthly_unit_limit=item.policy.monthly_unit_limit,
                now=now,
            )
            if not allowed:
                blocked_until = self.budget_store.blocked_until(reason or "budget", now)
                self.budget_store.record(
                    item.source,
                    now=now,
                    units=0,
                    next_due_at=blocked_until,
                    status="budget_exhausted",
                    reason=reason,
                    count_call=False,
                )
                health[item.source] = SourceHealth(
                    item.source, "budget_exhausted", now, now, 0.0,
                    normalized_quote_count=len(cached),
                    operator_count=len({q.operator_id for q in cached if q.operator_id}),
                    error_type="BudgetExhausted",
                    error_message=reason,
                )
                continue
            due.append(item)

        fetched: dict[str, tuple[list[Quote], float, Exception | None]] = {}
        if due:
            workers = max(1, min(self.config.max_workers, len(due)))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sportage-scheduler") as pool:
                futures = {pool.submit(self._fetch_one, item): item for item in due}
                for future in as_completed(futures):
                    item = futures[future]
                    fetched[item.source] = future.result()

        # Update cache first, then choose the next cadence from the newest combined state.
        for item in due:
            quotes, _, exc = fetched[item.source]
            if exc is None:
                self.cache[item.source] = quotes
        after = self._market_snapshot(now)
        self.last_snapshot = after

        for item in due:
            quotes, duration_ms, exc = fetched[item.source]
            interval = item.policy.error_retry_seconds if exc is not None else self._interval(item.policy, after.mode)
            next_due = now + timedelta(seconds=interval)
            self.budget_store.record(
                item.source,
                now=now,
                units=item.policy.units_per_call,
                next_due_at=next_due,
                status="error" if exc is not None else "ok",
                reason=None if exc is None else type(exc).__name__,
                count_call=True,
            )
            cached = self._fresh_cache(item, now)
            health[item.source] = SourceHealth(
                source=item.source,
                status="error" if exc is not None else "ok",
                started_at=now,
                completed_at=datetime.now(timezone.utc),
                duration_ms=duration_ms,
                raw_quote_count=len(quotes) if exc is None else 0,
                normalized_quote_count=len(cached),
                operator_count=len({q.operator_id for q in cached if q.operator_id}),
                error_type=None if exc is None else type(exc).__name__,
                error_message=None if exc is None else str(exc)[:2000],
            )

        all_quotes: list[Quote] = []
        for item in self.sources:
            all_quotes.extend(self._fresh_cache(item, now))
        merged = _merge_quotes(all_quotes)
        report = ProviderFetchReport(
            quotes=merged,
            source_health=[health[item.source] for item in self.sources],
            operator_coverage=_coverage(merged, now),
            fetched_at=now,
        )
        self.last_report = report
        return report

    def fetch_quotes(self) -> list[Quote]:
        report = self.fetch_report()
        if report.usable_source_count == 0 and report.failed_source_count > 0:
            raise RuntimeError("No usable market-data source or cache is available")
        return report.quotes


def build_adaptive_provider(
    config_path: str | Path,
    *,
    markets: str = "h2h,spreads,totals",
) -> AdaptiveScheduledProvider:
    config = load_scheduler_config(config_path)
    scheduled: list[ScheduledSource] = []
    for source, policy in config.sources.items():
        provider: OddsProvider | None = None
        reason: str | None = None
        if source == "betfair":
            if os.getenv("BETFAIR_APP_KEY") and os.getenv("BETFAIR_SESSION_TOKEN"):
                provider = BetfairExchangeMarketDataConnector()
            else:
                reason = "BETFAIR_APP_KEY and BETFAIR_SESSION_TOKEN are required"
        elif source == "odds_api_io":
            if os.getenv("ODDS_API_IO_KEY"):
                provider = OddsApiIoProvider()
            else:
                reason = "ODDS_API_IO_KEY is required"
        elif source == "the_odds_api":
            if os.getenv("THE_ODDS_API_KEY"):
                provider = TheOddsAPIProvider(markets=markets)
            else:
                reason = "THE_ODDS_API_KEY is required"
        else:
            reason = f"Unknown scheduler source '{source}'"
        scheduled.append(ScheduledSource(source, policy, provider, reason))
    if not scheduled:
        raise ValueError("Scheduler config contains no sources")
    return AdaptiveScheduledProvider(config, scheduled)
