from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any


EXECUTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_runs (
    execution_id TEXT PRIMARY KEY,
    event_market_key TEXT NOT NULL,
    status TEXT NOT NULL,
    live INTEGER NOT NULL DEFAULT 0,
    plan_json TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_execution_runs_status ON execution_runs(status, updated_at);
CREATE TABLE IF NOT EXISTS execution_legs (
    execution_id TEXT NOT NULL,
    leg_id TEXT NOT NULL,
    role TEXT NOT NULL,
    outcome TEXT NOT NULL,
    operator_id TEXT NOT NULL,
    status TEXT NOT NULL,
    order_json TEXT NOT NULL,
    result_json TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(execution_id, leg_id)
);
CREATE TABLE IF NOT EXISTS execution_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_execution_events_run ON execution_events(execution_id, id);
CREATE TABLE IF NOT EXISTS execution_locks (
    event_market_key TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL,
    acquired_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS execution_control (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    halted INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    updated_at TEXT NOT NULL
);
"""


class ExecutionStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn.executescript(EXECUTION_SCHEMA)
        self.conn.execute(
            "INSERT OR IGNORE INTO execution_control(singleton, halted, updated_at) VALUES (1, 0, ?)",
            (datetime.now(timezone.utc).isoformat(),),
        )
        self.conn.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def halt_state(self) -> tuple[bool, str | None]:
        row = self.conn.execute("SELECT halted, reason FROM execution_control WHERE singleton=1").fetchone()
        return bool(row["halted"]), row["reason"]

    def set_halt(self, reason: str) -> None:
        self.conn.execute(
            "UPDATE execution_control SET halted=1, reason=?, updated_at=? WHERE singleton=1",
            (reason[:2000], self._now()),
        )
        self.conn.commit()

    def clear_halt(self) -> None:
        self.conn.execute(
            "UPDATE execution_control SET halted=0, reason=NULL, updated_at=? WHERE singleton=1",
            (self._now(),),
        )
        self.conn.commit()

    def acquire_lock(self, event_market_key: str, execution_id: str) -> None:
        try:
            self.conn.execute(
                "INSERT INTO execution_locks(event_market_key, execution_id, acquired_at) VALUES (?, ?, ?)",
                (event_market_key, execution_id, self._now()),
            )
            self.conn.commit()
        except sqlite3.IntegrityError as exc:
            row = self.conn.execute(
                "SELECT execution_id FROM execution_locks WHERE event_market_key=?", (event_market_key,)
            ).fetchone()
            holder = row["execution_id"] if row else "unknown"
            raise RuntimeError(f"Execution lock already held by {holder}") from exc

    def release_lock(self, event_market_key: str, execution_id: str) -> None:
        self.conn.execute(
            "DELETE FROM execution_locks WHERE event_market_key=? AND execution_id=?",
            (event_market_key, execution_id),
        )
        self.conn.commit()

    def create_run(self, execution_id: str, event_market_key: str, status: str, live: bool, plan: dict[str, Any]) -> None:
        now = self._now()
        self.conn.execute(
            """INSERT INTO execution_runs
            (execution_id, event_market_key, status, live, plan_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (execution_id, event_market_key, status, int(live), json.dumps(plan, default=str), now, now),
        )
        self.conn.commit()

    def update_run(self, execution_id: str, status: str, reason: str | None = None, plan: dict[str, Any] | None = None) -> None:
        if plan is None:
            self.conn.execute(
                "UPDATE execution_runs SET status=?, reason=?, updated_at=? WHERE execution_id=?",
                (status, reason, self._now(), execution_id),
            )
        else:
            self.conn.execute(
                "UPDATE execution_runs SET status=?, reason=?, plan_json=?, updated_at=? WHERE execution_id=?",
                (status, reason, json.dumps(plan, default=str), self._now(), execution_id),
            )
        self.conn.commit()

    def get_run(self, execution_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM execution_runs WHERE execution_id=?", (execution_id,)).fetchone()

    def save_leg(
        self,
        execution_id: str,
        leg_id: str,
        role: str,
        outcome: str,
        operator_id: str,
        status: str,
        order: dict[str, Any],
        result: dict[str, Any] | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO execution_legs
            (execution_id, leg_id, role, outcome, operator_id, status, order_json, result_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(execution_id, leg_id) DO UPDATE SET
                status=excluded.status,
                order_json=excluded.order_json,
                result_json=excluded.result_json,
                updated_at=excluded.updated_at""",
            (
                execution_id, leg_id, role, outcome, operator_id, status,
                json.dumps(order, default=str), None if result is None else json.dumps(result, default=str), self._now(),
            ),
        )
        self.conn.commit()

    def get_legs(self, execution_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM execution_legs WHERE execution_id=? ORDER BY leg_id", (execution_id,)
        ))

    def event(self, execution_id: str, event_type: str, payload: dict[str, Any] | None = None) -> None:
        self.conn.execute(
            "INSERT INTO execution_events(execution_id, created_at, event_type, payload_json) VALUES (?, ?, ?, ?)",
            (execution_id, self._now(), event_type, json.dumps(payload or {}, default=str)),
        )
        self.conn.commit()

    def events(self, execution_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM execution_events WHERE execution_id=? ORDER BY id", (execution_id,)
        ))
