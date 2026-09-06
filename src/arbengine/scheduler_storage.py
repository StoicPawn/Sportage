from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


SCHEDULER_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduler_source_state (
    source TEXT PRIMARY KEY,
    day_key TEXT NOT NULL,
    day_calls INTEGER NOT NULL DEFAULT 0,
    month_key TEXT NOT NULL,
    month_units REAL NOT NULL DEFAULT 0,
    last_fetch_at TEXT,
    next_due_at TEXT,
    last_status TEXT,
    last_reason TEXT
);
"""


@dataclass(frozen=True)
class SchedulerBudgetState:
    source: str
    day_key: str
    day_calls: int
    month_key: str
    month_units: float
    last_fetch_at: datetime | None
    next_due_at: datetime | None
    last_status: str | None
    last_reason: str | None


class SchedulerBudgetStore:
    """Persistent call/credit ledger used by the adaptive provider scheduler."""

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        self._owns_connection = conn is None
        self.conn = conn or sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEDULER_SCHEMA)
        self.conn.commit()

    @staticmethod
    def _keys(now: datetime) -> tuple[str, str]:
        now = now.astimezone(timezone.utc)
        return now.date().isoformat(), now.strftime("%Y-%m")

    @staticmethod
    def _parse(value: str | None) -> datetime | None:
        return None if not value else datetime.fromisoformat(value)

    def state(self, source: str, now: datetime | None = None) -> SchedulerBudgetState:
        now = now or datetime.now(timezone.utc)
        day_key, month_key = self._keys(now)
        row = self.conn.execute(
            "SELECT * FROM scheduler_source_state WHERE source=?", (source,)
        ).fetchone()
        if row is None:
            return SchedulerBudgetState(source, day_key, 0, month_key, 0.0, None, None, None, None)
        day_calls = int(row["day_calls"]) if row["day_key"] == day_key else 0
        month_units = float(row["month_units"]) if row["month_key"] == month_key else 0.0
        return SchedulerBudgetState(
            source=source,
            day_key=day_key,
            day_calls=day_calls,
            month_key=month_key,
            month_units=month_units,
            last_fetch_at=self._parse(row["last_fetch_at"]),
            next_due_at=self._parse(row["next_due_at"]),
            last_status=row["last_status"],
            last_reason=row["last_reason"],
        )

    def can_spend(
        self,
        source: str,
        *,
        units: float,
        daily_call_limit: int | None,
        monthly_unit_limit: float | None,
        now: datetime | None = None,
    ) -> tuple[bool, str | None]:
        state = self.state(source, now)
        if daily_call_limit is not None and state.day_calls >= daily_call_limit:
            return False, "daily_call_limit"
        if monthly_unit_limit is not None and state.month_units + units > monthly_unit_limit:
            return False, "monthly_unit_limit"
        return True, None

    def blocked_until(self, reason: str, now: datetime | None = None) -> datetime:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if reason == "daily_call_limit":
            tomorrow = now.date() + timedelta(days=1)
            return datetime.combine(tomorrow, datetime.min.time(), tzinfo=timezone.utc)
        if reason == "monthly_unit_limit":
            if now.month == 12:
                return datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
            return datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)
        return now

    def record(
        self,
        source: str,
        *,
        now: datetime,
        units: float,
        next_due_at: datetime | None,
        status: str,
        reason: str | None = None,
        count_call: bool = True,
    ) -> SchedulerBudgetState:
        current = self.state(source, now)
        day_calls = current.day_calls + (1 if count_call else 0)
        month_units = current.month_units + (units if count_call else 0.0)
        last_fetch_at = now if count_call else current.last_fetch_at
        self.conn.execute(
            """INSERT INTO scheduler_source_state
            (source, day_key, day_calls, month_key, month_units, last_fetch_at,
             next_due_at, last_status, last_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source) DO UPDATE SET
                day_key=excluded.day_key,
                day_calls=excluded.day_calls,
                month_key=excluded.month_key,
                month_units=excluded.month_units,
                last_fetch_at=excluded.last_fetch_at,
                next_due_at=excluded.next_due_at,
                last_status=excluded.last_status,
                last_reason=excluded.last_reason""",
            (
                source,
                current.day_key,
                day_calls,
                current.month_key,
                month_units,
                None if last_fetch_at is None else last_fetch_at.isoformat(),
                None if next_due_at is None else next_due_at.isoformat(),
                status,
                reason,
            ),
        )
        self.conn.commit()
        return self.state(source, now)

    def list_states(self, now: datetime | None = None) -> list[SchedulerBudgetState]:
        rows = self.conn.execute("SELECT source FROM scheduler_source_state ORDER BY source").fetchall()
        return [self.state(str(row["source"]), now) for row in rows]

    def close(self) -> None:
        if self._owns_connection:
            self.conn.close()
