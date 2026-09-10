from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from arbengine.betfair_auth import session_token
from arbengine.connectors.betfair import _BetfairClient
from arbengine.storage import SQLiteStore


def _ensure_schema(store: SQLiteStore) -> None:
    store.conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS event_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL,
            source_event_id TEXT,
            sport TEXT,
            commence_time TEXT,
            home TEXT,
            away TEXT,
            status TEXT NOT NULL,
            winning_outcome TEXT,
            home_score REAL,
            away_score REAL,
            source TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            payload TEXT NOT NULL,
            UNIQUE(event_id, source)
        );
        CREATE INDEX IF NOT EXISTS idx_event_results_time
        ON event_results(commence_time, sport, event_id);
        CREATE INDEX IF NOT EXISTS idx_event_results_outcome
        ON event_results(sport, winning_outcome, commence_time);
        """
    )
    store.conn.commit()


def _market_signature(row: Any) -> str:
    line = "" if row["market_line"] is None else str(row["market_line"])
    return f"{row['market']}:{row['period'] or 'full_time'}:{line}"


def _candidate_rows(store: SQLiteStore, *, max_age_hours: int, limit_events: int) -> list[Any]:
    now = datetime.now(timezone.utc)
    oldest = now - timedelta(hours=max_age_hours)
    cutoff = now - timedelta(minutes=30)
    # Keep all quote rows for each selected event so selection ids can be mapped
    # back to canonical outcome names after Betfair settles the market.
    event_ids = [
        row["event_id"]
        for row in store.conn.execute(
            """SELECT q.event_id, MIN(q.commence_time) AS commence
            FROM quote_snapshots q
            LEFT JOIN event_results r ON r.event_id=q.event_id AND r.source='betfair_api_ng'
            WHERE q.source='betfair_api_ng'
              AND q.commence_time <= ?
              AND q.commence_time >= ?
              AND r.id IS NULL
            GROUP BY q.event_id
            ORDER BY commence ASC
            LIMIT ?""",
            (cutoff.isoformat(), oldest.isoformat(), limit_events),
        ).fetchall()
    ]
    if not event_ids:
        return []
    placeholders = ",".join("?" for _ in event_ids)
    return store.conn.execute(
        f"""SELECT * FROM quote_snapshots
        WHERE source='betfair_api_ng' AND event_id IN ({placeholders})
        ORDER BY event_id, id""",
        event_ids,
    ).fetchall()


def _payload(row: Any) -> dict[str, Any]:
    try:
        value = json.loads(row["payload"])
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _save_settlement(store: SQLiteStore, row: Any, winner: str, raw_book: dict[str, Any]) -> None:
    observed = datetime.now(timezone.utc).isoformat()
    signature = _market_signature(row)
    payload = {
        "event_id": row["event_id"],
        "market_signature": signature,
        "winning_outcome": winner,
        "settled_at": observed,
        "source": "betfair_api_ng",
        "observed_at": observed,
        "betfair_market": raw_book,
    }
    store.conn.execute(
        """INSERT INTO settlement_results(
            event_id,market_signature,winning_outcome,settled_at,source,observed_at,payload
        ) VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(event_id,market_signature) DO UPDATE SET
            winning_outcome=excluded.winning_outcome,
            settled_at=excluded.settled_at,
            source=excluded.source,
            observed_at=excluded.observed_at,
            payload=excluded.payload""",
        (
            row["event_id"], signature, winner, observed, "betfair_api_ng", observed,
            json.dumps(payload, separators=(",", ":"), default=str),
        ),
    )


def run_result_cycle(
    db_path: str | Path = "data/arbitrage.sqlite3",
    *,
    max_age_hours: int = 72,
    limit_events: int = 100,
) -> dict[str, Any]:
    """Resolve completed Betfair markets into separate event/settlement tables.

    This watcher never sends orders. It reads previously observed Betfair market ids,
    asks API-NG for their current status, and records the WINNER runner once CLOSED.
    Exact scores remain nullable because MATCH_ODDS settlement proves the outcome but
    does not itself constitute a score feed.
    """
    store = SQLiteStore(db_path)
    _ensure_schema(store)
    rows = _candidate_rows(store, max_age_hours=max_age_hours, limit_events=limit_events)
    if not rows:
        store.close()
        return {"candidate_events": 0, "settled_markets": 0, "event_results": 0}

    by_market: dict[str, list[Any]] = defaultdict(list)
    event_meta: dict[str, Any] = {}
    for row in rows:
        native = _payload(row)
        market_id = str(native.get("source_market_id") or "").strip()
        if not market_id:
            continue
        by_market[market_id].append(row)
        event_meta.setdefault(row["event_id"], row)

    if not by_market:
        store.close()
        return {"candidate_events": len(event_meta), "settled_markets": 0, "event_results": 0}

    app_key = __import__("os").environ.get("BETFAIR_APP_KEY")
    token = session_token()
    client = _BetfairClient(app_key=app_key, session_token=token, timeout=20.0)
    settled_by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
    market_ids = list(by_market)
    for start in range(0, len(market_ids), 40):
        batch = market_ids[start : start + 40]
        result = client.call("listMarketBook", {"marketIds": batch})["result"]
        for book in result or []:
            if str(book.get("status") or "").upper() != "CLOSED":
                continue
            market_id = str(book.get("marketId") or "")
            source_rows = by_market.get(market_id, [])
            if not source_rows:
                continue
            winner_ids = {
                str(runner.get("selectionId"))
                for runner in (book.get("runners") or [])
                if str(runner.get("status") or "").upper() == "WINNER"
            }
            if not winner_ids:
                continue
            winner_name: str | None = None
            representative = source_rows[0]
            for source_row in source_rows:
                native = _payload(source_row)
                if str(native.get("source_selection_id") or "") in winner_ids:
                    winner_name = str(source_row["outcome"])
                    representative = source_row
                    break
            if not winner_name:
                continue
            _save_settlement(store, representative, winner_name, book)
            settled_by_event[representative["event_id"]].append(
                {"market_id": market_id, "market_signature": _market_signature(representative), "winner": winner_name}
            )

    observed = datetime.now(timezone.utc).isoformat()
    event_results = 0
    for event_id, settlements in settled_by_event.items():
        row = event_meta[event_id]
        # Prefer a full-time 1x2/h2h winner as the event-level result.
        preferred = next(
            (item for item in settlements if item["market_signature"].startswith(("1x2:full_time", "h2h:full_time"))),
            settlements[0],
        )
        event_payload = {
            "settlements": settlements,
            "note": "Outcome derived from official Betfair market settlement; exact score not supplied by MATCH_ODDS.",
        }
        store.conn.execute(
            """INSERT INTO event_results(
                event_id,source_event_id,sport,commence_time,home,away,status,winning_outcome,
                home_score,away_score,source,observed_at,payload
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(event_id,source) DO UPDATE SET
                status=excluded.status,winning_outcome=excluded.winning_outcome,
                home_score=excluded.home_score,away_score=excluded.away_score,
                observed_at=excluded.observed_at,payload=excluded.payload""",
            (
                event_id, row["source_event_id"], row["sport"], row["commence_time"], row["home"], row["away"],
                "settled", preferred["winner"], None, None, "betfair_api_ng", observed,
                json.dumps(event_payload, separators=(",", ":"), default=str),
            ),
        )
        event_results += 1
    store.conn.commit()
    store.close()
    return {
        "candidate_events": len(event_meta),
        "queried_markets": len(by_market),
        "settled_markets": sum(len(items) for items in settled_by_event.values()),
        "event_results": event_results,
    }
