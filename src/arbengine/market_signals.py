from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from statistics import median

from .models import ArbitrageOpportunity, Quote


SIGNAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS market_signal_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    event_id TEXT NOT NULL,
    market_signature TEXT NOT NULL,
    status TEXT NOT NULL,
    gross_implied_sum TEXT NOT NULL,
    gross_roi TEXT NOT NULL,
    net_roi TEXT,
    bookmaker_count INTEGER NOT NULL,
    outcome_count INTEGER NOT NULL,
    best_legs_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_market_signal_scan ON market_signal_snapshots(scan_id);
CREATE INDEX IF NOT EXISTS idx_market_signal_event_market
ON market_signal_snapshots(event_id, market_signature, observed_at);
CREATE INDEX IF NOT EXISTS idx_market_signal_status_time
ON market_signal_snapshots(status, observed_at);
"""


@dataclass(frozen=True)
class MarketSignalSnapshot:
    observed_at: datetime
    event_id: str
    market_signature: str
    status: str
    gross_implied_sum: Decimal
    gross_roi: Decimal
    net_roi: Decimal | None
    bookmaker_count: int
    outcome_count: int
    best_legs: dict[str, tuple[str, Decimal]]

    @property
    def event_market_key(self) -> str:
        return f"{self.event_id}|{self.market_signature}"


@dataclass(frozen=True)
class SignalLifecycle:
    event_market_key: str
    status: str
    started_at: datetime
    ended_at: datetime
    duration_seconds: float
    snapshots: int
    max_gross_roi: Decimal
    max_net_roi: Decimal | None


@dataclass(frozen=True)
class LifecycleSummary:
    lifecycles: int
    median_duration_seconds: float
    p90_duration_seconds: float
    max_duration_seconds: float
    median_snapshots: float
    max_gross_roi: Decimal
    max_net_roi: Decimal | None


def build_market_signals(
    quotes: list[Quote],
    opportunities: list[ArbitrageOpportunity],
    *,
    observed_at: datetime | None = None,
    near_gap: Decimal = Decimal("0.02"),
) -> list[MarketSignalSnapshot]:
    observed_at = observed_at or datetime.now(timezone.utc)
    net_by_key = {opp.event_market_key: opp.net_roi for opp in opportunities}
    groups: dict[tuple[str, str], list[Quote]] = defaultdict(list)
    for quote in quotes:
        groups[(quote.event_id, quote.market_signature)].append(quote)

    result: list[MarketSignalSnapshot] = []
    for (event_id, market_signature), group in groups.items():
        expected = max((q.expected_outcomes for q in group), default=0)
        outcomes = sorted({q.outcome for q in group})
        if expected < 2 or len(outcomes) != expected:
            continue

        best_legs: dict[str, tuple[str, Decimal]] = {}
        implied = Decimal("0")
        for outcome in outcomes:
            candidates = [q for q in group if q.outcome == outcome]
            if not candidates:
                break
            best = max(candidates, key=lambda q: q.odds)
            best_legs[outcome] = (best.bookmaker, best.odds)
            implied += Decimal("1") / best.odds
        if len(best_legs) != expected:
            continue

        gross_roi = Decimal("1") / implied - Decimal("1")
        key = f"{event_id}|{market_signature}"
        net_roi = net_by_key.get(key)
        if net_roi is not None:
            status = "net_arbitrage"
        elif implied < Decimal("1"):
            status = "gross_arbitrage"
        elif implied <= Decimal("1") + near_gap:
            status = "near_arbitrage"
        else:
            status = "normal"

        result.append(
            MarketSignalSnapshot(
                observed_at=observed_at,
                event_id=event_id,
                market_signature=market_signature,
                status=status,
                gross_implied_sum=implied,
                gross_roi=gross_roi,
                net_roi=net_roi,
                bookmaker_count=len({q.operator_id or q.bookmaker for q in group}),
                outcome_count=expected,
                best_legs=best_legs,
            )
        )
    return result


class MarketSignalStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn.executescript(SIGNAL_SCHEMA)
        self.conn.commit()

    def save(self, scan_id: int, signals: list[MarketSignalSnapshot]) -> None:
        if not signals:
            return
        self.conn.executemany(
            """INSERT INTO market_signal_snapshots
            (scan_id, observed_at, event_id, market_signature, status,
             gross_implied_sum, gross_roi, net_roi, bookmaker_count, outcome_count, best_legs_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    scan_id,
                    signal.observed_at.isoformat(),
                    signal.event_id,
                    signal.market_signature,
                    signal.status,
                    str(signal.gross_implied_sum),
                    str(signal.gross_roi),
                    None if signal.net_roi is None else str(signal.net_roi),
                    signal.bookmaker_count,
                    signal.outcome_count,
                    json.dumps({k: [book, str(odds)] for k, (book, odds) in signal.best_legs.items()}),
                )
                for signal in signals
            ],
        )
        self.conn.commit()

    def list(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        status: str | None = None,
    ) -> list[MarketSignalSnapshot]:
        clauses: list[str] = []
        params: list[object] = []
        if start is not None:
            clauses.append("observed_at >= ?")
            params.append(start.isoformat())
        if end is not None:
            clauses.append("observed_at <= ?")
            params.append(end.isoformat())
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"SELECT * FROM market_signal_snapshots {where} ORDER BY observed_at, id", params
        ).fetchall()
        result: list[MarketSignalSnapshot] = []
        for row in rows:
            payload = json.loads(row["best_legs_json"])
            result.append(
                MarketSignalSnapshot(
                    observed_at=datetime.fromisoformat(row["observed_at"]),
                    event_id=row["event_id"],
                    market_signature=row["market_signature"],
                    status=row["status"],
                    gross_implied_sum=Decimal(row["gross_implied_sum"]),
                    gross_roi=Decimal(row["gross_roi"]),
                    net_roi=None if row["net_roi"] is None else Decimal(row["net_roi"]),
                    bookmaker_count=int(row["bookmaker_count"]),
                    outcome_count=int(row["outcome_count"]),
                    best_legs={k: (v[0], Decimal(v[1])) for k, v in payload.items()},
                )
            )
        return result


def build_lifecycles(
    signals: list[MarketSignalSnapshot],
    *,
    max_gap_seconds: float = 90.0,
) -> list[SignalLifecycle]:
    grouped: dict[tuple[str, str], list[MarketSignalSnapshot]] = defaultdict(list)
    for signal in signals:
        grouped[(signal.event_market_key, signal.status)].append(signal)

    result: list[SignalLifecycle] = []
    for (key, status), items in grouped.items():
        items.sort(key=lambda item: item.observed_at)
        run: list[MarketSignalSnapshot] = []

        def close_run() -> None:
            if not run:
                return
            net_values = [item.net_roi for item in run if item.net_roi is not None]
            result.append(
                SignalLifecycle(
                    event_market_key=key,
                    status=status,
                    started_at=run[0].observed_at,
                    ended_at=run[-1].observed_at,
                    duration_seconds=max(0.0, (run[-1].observed_at - run[0].observed_at).total_seconds()),
                    snapshots=len(run),
                    max_gross_roi=max(item.gross_roi for item in run),
                    max_net_roi=max(net_values) if net_values else None,
                )
            )

        for item in items:
            if run and (item.observed_at - run[-1].observed_at).total_seconds() > max_gap_seconds:
                close_run()
                run = []
            run.append(item)
        close_run()

    return sorted(result, key=lambda item: item.started_at)


def summarize_lifecycles(lifecycles: list[SignalLifecycle]) -> LifecycleSummary:
    if not lifecycles:
        return LifecycleSummary(0, 0.0, 0.0, 0.0, 0.0, Decimal("0"), None)
    durations = sorted(item.duration_seconds for item in lifecycles)
    p90_index = min(len(durations) - 1, max(0, int(0.9 * len(durations)) - 1))
    net_values = [item.max_net_roi for item in lifecycles if item.max_net_roi is not None]
    return LifecycleSummary(
        lifecycles=len(lifecycles),
        median_duration_seconds=float(median(durations)),
        p90_duration_seconds=float(durations[p90_index]),
        max_duration_seconds=float(max(durations)),
        median_snapshots=float(median([item.snapshots for item in lifecycles])),
        max_gross_roi=max(item.max_gross_roi for item in lifecycles),
        max_net_roi=max(net_values) if net_values else None,
    )
