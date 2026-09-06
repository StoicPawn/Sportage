from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from statistics import median

from .market_signals import (
    MarketSignalSnapshot,
    MarketSignalStore,
    SignalLifecycle,
    build_lifecycles,
    summarize_lifecycles,
)
from .storage import SQLiteStore


DEFAULT_SURVIVAL_THRESHOLDS = (2.0, 5.0, 10.0, 15.0, 30.0, 60.0)


@dataclass(frozen=True)
class FunnelStats:
    near_or_better: int
    gross_or_better: int
    net_arbitrage: int
    executable: int


@dataclass(frozen=True)
class SurvivalPoint:
    seconds: float
    survivors: int
    total: int

    @property
    def survival_rate(self) -> float:
        return self.survivors / self.total if self.total else 0.0


@dataclass(frozen=True)
class BreakdownRow:
    label: str
    lifecycles: int
    median_duration_seconds: float
    max_duration_seconds: float
    max_net_roi: Decimal | None


@dataclass(frozen=True)
class ControlCenterReport:
    start: datetime | None
    end: datetime | None
    signal_snapshots: int
    distinct_markets: int
    net_lifecycles: int
    median_net_lifetime_seconds: float
    p90_net_lifetime_seconds: float
    max_net_lifetime_seconds: float
    max_net_roi: Decimal | None
    funnel: FunnelStats
    survival: tuple[SurvivalPoint, ...]
    by_sport: tuple[BreakdownRow, ...]
    by_market: tuple[BreakdownRow, ...]
    by_bookmaker: tuple[BreakdownRow, ...]


def _event_sports(store: SQLiteStore, event_ids: set[str]) -> dict[str, str]:
    if not event_ids:
        return {}
    placeholders = ",".join("?" for _ in event_ids)
    rows = store.conn.execute(
        f"""SELECT event_id, MAX(COALESCE(sport, 'unknown')) AS sport
        FROM quote_snapshots
        WHERE event_id IN ({placeholders})
        GROUP BY event_id""",
        tuple(sorted(event_ids)),
    ).fetchall()
    return {str(row["event_id"]): str(row["sport"] or "unknown") for row in rows}


def _signals_for_lifecycle(
    signals: list[MarketSignalSnapshot],
    lifecycle: SignalLifecycle,
) -> list[MarketSignalSnapshot]:
    return [
        item
        for item in signals
        if item.status == lifecycle.status
        and item.event_market_key == lifecycle.event_market_key
        and lifecycle.started_at <= item.observed_at <= lifecycle.ended_at
    ]


def _breakdown(
    lifecycles: list[SignalLifecycle],
    labels_by_lifecycle: list[set[str]],
) -> tuple[BreakdownRow, ...]:
    grouped: dict[str, list[SignalLifecycle]] = defaultdict(list)
    for lifecycle, labels in zip(lifecycles, labels_by_lifecycle, strict=True):
        for label in labels or {"unknown"}:
            grouped[label].append(lifecycle)

    rows: list[BreakdownRow] = []
    for label, items in grouped.items():
        durations = [item.duration_seconds for item in items]
        net_values = [item.max_net_roi for item in items if item.max_net_roi is not None]
        rows.append(
            BreakdownRow(
                label=label,
                lifecycles=len(items),
                median_duration_seconds=float(median(durations)) if durations else 0.0,
                max_duration_seconds=max(durations) if durations else 0.0,
                max_net_roi=max(net_values) if net_values else None,
            )
        )
    return tuple(sorted(rows, key=lambda row: (-row.lifecycles, row.label.lower())))


def build_control_center_report(
    store: SQLiteStore,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    execution_seconds: float = 15.0,
    max_gap_seconds: float = 90.0,
    survival_thresholds: tuple[float, ...] = DEFAULT_SURVIVAL_THRESHOLDS,
) -> ControlCenterReport:
    if execution_seconds < 0:
        raise ValueError("execution_seconds must be >= 0")
    if max_gap_seconds <= 0:
        raise ValueError("max_gap_seconds must be > 0")

    signal_store = MarketSignalStore(store.conn)
    signals = signal_store.list(start=start, end=end)
    market_keys = {item.event_market_key for item in signals}

    cumulative_near = {
        item.event_market_key
        for item in signals
        if item.status in {"near_arbitrage", "gross_arbitrage", "net_arbitrage"}
    }
    cumulative_gross = {
        item.event_market_key
        for item in signals
        if item.status in {"gross_arbitrage", "net_arbitrage"}
    }
    cumulative_net = {
        item.event_market_key for item in signals if item.status == "net_arbitrage"
    }

    net_signals = [item for item in signals if item.status == "net_arbitrage"]
    net_lifecycles = build_lifecycles(net_signals, max_gap_seconds=max_gap_seconds)
    net_summary = summarize_lifecycles(net_lifecycles)
    executable = sum(1 for lifecycle in net_lifecycles if lifecycle.duration_seconds >= execution_seconds)

    survival = tuple(
        SurvivalPoint(
            seconds=float(seconds),
            survivors=sum(1 for lifecycle in net_lifecycles if lifecycle.duration_seconds >= seconds),
            total=len(net_lifecycles),
        )
        for seconds in survival_thresholds
    )

    event_ids = {item.event_market_key.split("|", 1)[0] for item in net_lifecycles}
    sport_by_event = _event_sports(store, event_ids)

    sport_labels: list[set[str]] = []
    market_labels: list[set[str]] = []
    bookmaker_labels: list[set[str]] = []
    for lifecycle in net_lifecycles:
        event_id, market_signature = lifecycle.event_market_key.split("|", 1)
        episode_signals = _signals_for_lifecycle(net_signals, lifecycle)
        bookmakers = {
            bookmaker
            for signal in episode_signals
            for bookmaker, _ in signal.best_legs.values()
        }
        sport_labels.append({sport_by_event.get(event_id, "unknown")})
        market_labels.append({market_signature})
        bookmaker_labels.append(bookmakers or {"unknown"})

    return ControlCenterReport(
        start=start,
        end=end,
        signal_snapshots=len(signals),
        distinct_markets=len(market_keys),
        net_lifecycles=len(net_lifecycles),
        median_net_lifetime_seconds=net_summary.median_duration_seconds,
        p90_net_lifetime_seconds=net_summary.p90_duration_seconds,
        max_net_lifetime_seconds=net_summary.max_duration_seconds,
        max_net_roi=net_summary.max_net_roi,
        funnel=FunnelStats(
            near_or_better=len(cumulative_near),
            gross_or_better=len(cumulative_gross),
            net_arbitrage=len(cumulative_net),
            executable=executable,
        ),
        survival=survival,
        by_sport=_breakdown(net_lifecycles, sport_labels),
        by_market=_breakdown(net_lifecycles, market_labels),
        by_bookmaker=_breakdown(net_lifecycles, bookmaker_labels),
    )
