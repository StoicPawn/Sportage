from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import ArbitrageOpportunity, Quote

SCHEMA = """
CREATE TABLE IF NOT EXISTS scan_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    provider TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    quote_count INTEGER NOT NULL DEFAULT 0,
    opportunity_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_scan_runs_started ON scan_runs(started_at);
CREATE TABLE IF NOT EXISTS quote_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER,
    observed_at TEXT NOT NULL,
    event_id TEXT NOT NULL,
    sport TEXT,
    commence_time TEXT,
    home TEXT,
    away TEXT,
    market TEXT NOT NULL,
    period TEXT DEFAULT 'full_time',
    market_line TEXT,
    expected_outcomes INTEGER DEFAULT 2,
    bookmaker TEXT NOT NULL,
    outcome TEXT NOT NULL,
    odds TEXT NOT NULL,
    source TEXT,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quote_event_market ON quote_snapshots(event_id, market);
CREATE INDEX IF NOT EXISTS idx_quote_scan ON quote_snapshots(scan_id);
CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER,
    detected_at TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    event_id TEXT NOT NULL,
    market TEXT NOT NULL,
    net_roi TEXT NOT NULL,
    gross_roi TEXT,
    capital_used TEXT,
    guaranteed_profit TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_opportunity_fingerprint ON opportunities(fingerprint);
CREATE INDEX IF NOT EXISTS idx_opportunity_scan ON opportunities(scan_id);
"""


class SQLiteStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate_legacy_schema()
        self.conn.commit()

    def _columns(self, table: str) -> set[str]:
        return {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}

    def _ensure_column(self, table: str, name: str, definition: str) -> None:
        if name not in self._columns(table):
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def _migrate_legacy_schema(self) -> None:
        for name, definition in {
            "scan_id": "INTEGER", "sport": "TEXT", "commence_time": "TEXT", "home": "TEXT",
            "away": "TEXT", "period": "TEXT DEFAULT 'full_time'", "market_line": "TEXT",
            "expected_outcomes": "INTEGER DEFAULT 2", "source": "TEXT"
        }.items():
            self._ensure_column("quote_snapshots", name, definition)
        for name, definition in {
            "scan_id": "INTEGER", "net_roi": "TEXT", "gross_roi": "TEXT", "capital_used": "TEXT"
        }.items():
            self._ensure_column("opportunities", name, definition)
        cols = self._columns("opportunities")
        if "roi" in cols and "net_roi" in cols:
            self.conn.execute("UPDATE opportunities SET net_roi = roi WHERE net_roi IS NULL")

    def begin_scan(self, provider: str, started_at: datetime | None = None) -> int:
        started_at = started_at or datetime.now(timezone.utc)
        cur = self.conn.execute(
            "INSERT INTO scan_runs(started_at, provider, status) VALUES (?, ?, 'running')",
            (started_at.isoformat(), provider),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_scan(self, scan_id: int, quote_count: int, opportunity_count: int, status: str = "ok") -> None:
        self.conn.execute(
            "UPDATE scan_runs SET completed_at=?, quote_count=?, opportunity_count=?, status=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), quote_count, opportunity_count, status, scan_id),
        )
        self.conn.commit()

    def save_quotes(self, quotes: list[Quote], scan_id: int | None = None) -> None:
        self.conn.executemany(
            """INSERT INTO quote_snapshots
            (scan_id, observed_at, event_id, sport, commence_time, home, away, market, period, market_line,
             expected_outcomes, bookmaker, outcome, odds, source, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(
                scan_id, q.observed_at.isoformat(), q.event_id, q.sport, q.commence_time.isoformat(),
                q.home, q.away, q.market.value, q.period,
                None if q.market_line is None else str(q.market_line), q.expected_outcomes,
                q.bookmaker, q.outcome, str(q.odds), q.source, q.model_dump_json()
            ) for q in quotes],
        )
        self.conn.commit()

    def save_opportunities(self, opportunities: list[ArbitrageOpportunity], scan_id: int | None = None) -> None:
        self.conn.executemany(
            """INSERT INTO opportunities
            (scan_id, detected_at, fingerprint, event_id, market, net_roi, gross_roi, capital_used,
             guaranteed_profit, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(
                scan_id, o.detected_at.isoformat(), o.fingerprint, o.event_id, o.market.value,
                str(o.net_roi), str(o.gross_roi), str(o.capital_used), str(o.guaranteed_profit),
                json.dumps(o.model_dump(mode="json"))
            ) for o in opportunities],
        )
        self.conn.commit()

    def list_scans(self, start: datetime | None = None, end: datetime | None = None) -> list[sqlite3.Row]:
        clauses = ["status='ok'"]
        params: list[str] = []
        if start is not None:
            clauses.append("started_at >= ?"); params.append(start.isoformat())
        if end is not None:
            clauses.append("started_at <= ?"); params.append(end.isoformat())
        return list(self.conn.execute(
            f"SELECT * FROM scan_runs WHERE {' AND '.join(clauses)} ORDER BY started_at, id", params
        ))

    def load_quotes_for_scan(self, scan_id: int) -> list[Quote]:
        rows = self.conn.execute(
            "SELECT payload FROM quote_snapshots WHERE scan_id=? ORDER BY id", (scan_id,)
        ).fetchall()
        return [Quote.model_validate_json(row["payload"]) for row in rows]

    def latest_scan(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM scan_runs WHERE status='ok' ORDER BY started_at DESC, id DESC LIMIT 1"
        ).fetchone()

    def summary(self) -> dict[str, int | float]:
        scans = self.conn.execute("SELECT COUNT(*) c FROM scan_runs WHERE status='ok'").fetchone()["c"]
        quotes = self.conn.execute("SELECT COUNT(*) c FROM quote_snapshots").fetchone()["c"]
        opportunities = self.conn.execute("SELECT COUNT(*) c FROM opportunities").fetchone()["c"]
        best = self.conn.execute(
            "SELECT MAX(CAST(net_roi AS REAL)) v FROM opportunities WHERE net_roi IS NOT NULL"
        ).fetchone()["v"]
        return {"scans": scans, "quotes": quotes, "opportunities": opportunities, "best_net_roi": best or 0.0}

    def close(self) -> None:
        self.conn.close()
