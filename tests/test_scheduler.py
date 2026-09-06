import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arbengine.models import MarketType, Quote
from arbengine.providers.base import OddsProvider
from arbengine.providers.scheduler import (
    AdaptiveScheduledProvider,
    ScheduledSource,
    SchedulerConfig,
    SourceSchedulePolicy,
)
from arbengine.scheduler_storage import SchedulerBudgetStore


class StaticProvider(OddsProvider):
    def __init__(self, quotes):
        self.quotes = quotes
        self.calls = 0

    def fetch_quotes(self):
        self.calls += 1
        return list(self.quotes)


def quote(now, bookmaker, outcome, odds):
    return Quote(
        event_id="provider-event",
        sport="football",
        commence_time=now + timedelta(hours=1),
        home="Alpha",
        away="Beta",
        market=MarketType.H2H,
        outcome=outcome,
        bookmaker=bookmaker,
        odds=Decimal(odds),
        expected_outcomes=2,
        observed_at=now,
        source="odds_api_io",
    )


def memory_budget_store():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return SchedulerBudgetStore(conn)


def test_daily_budget_stops_new_calls_but_keeps_fresh_cache():
    now = datetime(2026, 9, 6, 8, 0, tzinfo=timezone.utc)
    provider = StaticProvider([
        quote(now, "Bet365", "Alpha", "2.05"),
        quote(now, "Bet365", "Beta", "1.95"),
    ])
    policy = SourceSchedulePolicy(
        base_interval_seconds=1,
        near_event_interval_seconds=1,
        hot_interval_seconds=1,
        daily_call_limit=1,
        max_cache_age_seconds=300,
    )
    scheduler = AdaptiveScheduledProvider(
        SchedulerConfig(sources={"odds_api_io": policy}),
        [ScheduledSource("odds_api_io", policy, provider)],
        budget_store=memory_budget_store(),
    )

    first = scheduler.fetch_report(now=now)
    assert provider.calls == 1
    assert first.successful_source_count == 1

    second = scheduler.fetch_report(now=now + timedelta(seconds=2))
    assert provider.calls == 1
    assert second.source_health[0].status == "budget_exhausted"
    assert second.usable_source_count == 1
    assert len(second.quotes) == 2


def test_hot_market_shortens_next_due_interval():
    now = datetime(2026, 9, 6, 8, 0, tzinfo=timezone.utc)
    provider = StaticProvider([
        quote(now, "Bet365", "Alpha", "2.02"),
        quote(now, "Sisal", "Beta", "2.02"),
    ])
    policy = SourceSchedulePolicy(
        base_interval_seconds=300,
        near_event_interval_seconds=60,
        hot_interval_seconds=5,
        max_cache_age_seconds=300,
    )
    budget = memory_budget_store()
    scheduler = AdaptiveScheduledProvider(
        SchedulerConfig(hot_implied_gap=0.02, sources={"odds_api_io": policy}),
        [ScheduledSource("odds_api_io", policy, provider)],
        budget_store=budget,
    )

    report = scheduler.fetch_report(now=now)
    state = budget.state("odds_api_io", now)
    assert report.quotes
    assert scheduler.last_snapshot.mode == "hot"
    assert state.next_due_at == now + timedelta(seconds=5)


def test_not_due_tick_uses_cache_without_spending_budget():
    now = datetime(2026, 9, 6, 8, 0, tzinfo=timezone.utc)
    provider = StaticProvider([
        quote(now, "Bet365", "Alpha", "1.90"),
        quote(now, "Bet365", "Beta", "1.90"),
    ])
    policy = SourceSchedulePolicy(
        base_interval_seconds=300,
        near_event_interval_seconds=120,
        hot_interval_seconds=30,
        max_cache_age_seconds=300,
    )
    budget = memory_budget_store()
    scheduler = AdaptiveScheduledProvider(
        SchedulerConfig(sources={"odds_api_io": policy}),
        [ScheduledSource("odds_api_io", policy, provider)],
        budget_store=budget,
    )

    scheduler.fetch_report(now=now)
    calls_after_first = budget.state("odds_api_io", now).day_calls
    report = scheduler.fetch_report(now=now + timedelta(seconds=10))

    assert provider.calls == 1
    assert budget.state("odds_api_io", now).day_calls == calls_after_first
    assert report.source_health[0].status == "cached"
    assert report.usable_source_count == 1


def test_monthly_unit_budget_is_enforced():
    now = datetime(2026, 9, 6, 8, 0, tzinfo=timezone.utc)
    provider = StaticProvider([
        quote(now, "Bet365", "Alpha", "1.90"),
        quote(now, "Bet365", "Beta", "1.90"),
    ])
    policy = SourceSchedulePolicy(
        base_interval_seconds=1,
        near_event_interval_seconds=1,
        hot_interval_seconds=1,
        monthly_unit_limit=3,
        units_per_call=3,
        max_cache_age_seconds=300,
    )
    budget = memory_budget_store()
    scheduler = AdaptiveScheduledProvider(
        SchedulerConfig(sources={"the_odds_api": policy}),
        [ScheduledSource("the_odds_api", policy, provider)],
        budget_store=budget,
    )

    scheduler.fetch_report(now=now)
    report = scheduler.fetch_report(now=now + timedelta(seconds=2))
    assert provider.calls == 1
    assert report.source_health[0].status == "budget_exhausted"
    assert budget.state("the_odds_api", now).month_units == 3
