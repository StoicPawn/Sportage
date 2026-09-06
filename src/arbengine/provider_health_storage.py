from __future__ import annotations

import sqlite3

from .providers.health import ProviderFetchReport


HEALTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_health (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    duration_ms REAL NOT NULL,
    raw_quote_count INTEGER NOT NULL DEFAULT 0,
    normalized_quote_count INTEGER NOT NULL DEFAULT 0,
    operator_count INTEGER NOT NULL DEFAULT 0,
    error_type TEXT,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_source_health_scan ON source_health(scan_id);
CREATE INDEX IF NOT EXISTS idx_source_health_source_scan ON source_health(source, scan_id);

CREATE TABLE IF NOT EXISTS operator_coverage (
    scan_id INTEGER NOT NULL,
    operator_id TEXT NOT NULL,
    quote_count INTEGER NOT NULL,
    event_count INTEGER NOT NULL,
    market_count INTEGER NOT NULL,
    source_count INTEGER NOT NULL,
    freshest_observed_at TEXT,
    oldest_quote_age_seconds REAL NOT NULL DEFAULT 0,
    PRIMARY KEY(scan_id, operator_id)
);
CREATE INDEX IF NOT EXISTS idx_operator_coverage_operator_scan
ON operator_coverage(operator_id, scan_id);
"""


class ProviderHealthStore:
    """Persists feed health next to Sportage scan history using the same SQLite connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn.executescript(HEALTH_SCHEMA)
        self.conn.commit()

    def save_report(self, scan_id: int, report: ProviderFetchReport) -> None:
        self.conn.executemany(
            """INSERT INTO source_health
            (scan_id, source, status, started_at, completed_at, duration_ms,
             raw_quote_count, normalized_quote_count, operator_count, error_type, error_message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    scan_id,
                    item.source,
                    item.status,
                    item.started_at.isoformat(),
                    item.completed_at.isoformat(),
                    item.duration_ms,
                    item.raw_quote_count,
                    item.normalized_quote_count,
                    item.operator_count,
                    item.error_type,
                    item.error_message,
                )
                for item in report.source_health
            ],
        )
        self.conn.executemany(
            """INSERT OR REPLACE INTO operator_coverage
            (scan_id, operator_id, quote_count, event_count, market_count, source_count,
             freshest_observed_at, oldest_quote_age_seconds)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    scan_id,
                    item.operator_id,
                    item.quote_count,
                    item.event_count,
                    item.market_count,
                    item.source_count,
                    None if item.freshest_observed_at is None else item.freshest_observed_at.isoformat(),
                    item.oldest_quote_age_seconds,
                )
                for item in report.operator_coverage
            ],
        )
        self.conn.commit()

    def latest_source_health(self) -> list[sqlite3.Row]:
        row = self.conn.execute("SELECT MAX(scan_id) AS scan_id FROM source_health").fetchone()
        if row is None or row["scan_id"] is None:
            return []
        return list(
            self.conn.execute(
                "SELECT * FROM source_health WHERE scan_id=? ORDER BY source",
                (int(row["scan_id"]),),
            )
        )

    def latest_operator_coverage(self) -> list[sqlite3.Row]:
        row = self.conn.execute("SELECT MAX(scan_id) AS scan_id FROM operator_coverage").fetchone()
        if row is None or row["scan_id"] is None:
            return []
        return list(
            self.conn.execute(
                "SELECT * FROM operator_coverage WHERE scan_id=? ORDER BY operator_id",
                (int(row["scan_id"]),),
            )
        )
