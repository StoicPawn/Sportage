from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .account_funds import AccountSnapshotStore, refresh_account_snapshot
from .betfair_auth import configured as betfair_configured
from .betfair_auth import session_token as betfair_session_token
from .direct_venues import DIRECT_VENUES, ItalyPolicy
from .venue_certification import VenueCertificationStore, VenueCertifier, execution_environment


SCHEMA = """
CREATE TABLE IF NOT EXISTS direct_venue_status (
    venue_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    italy_policy TEXT NOT NULL,
    adm_concession TEXT,
    personal_account_api INTEGER NOT NULL,
    market_data_api INTEGER NOT NULL,
    order_api INTEGER NOT NULL,
    configured INTEGER NOT NULL,
    health TEXT NOT NULL,
    execution_ready INTEGER NOT NULL,
    live_enabled INTEGER NOT NULL,
    environment TEXT,
    checked_at TEXT NOT NULL,
    last_success_at TEXT,
    message TEXT NOT NULL,
    details_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS direct_venue_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    venue_id TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    configured INTEGER NOT NULL,
    health TEXT NOT NULL,
    execution_ready INTEGER NOT NULL,
    environment TEXT,
    message TEXT NOT NULL,
    details_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_direct_venue_checks_venue_time
ON direct_venue_checks(venue_id, checked_at DESC);
"""


def _truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _betflag_configured() -> bool:
    has_session = bool(os.getenv("BETFLAG_SESSION_TOKEN"))
    has_login = bool(os.getenv("BETFLAG_USERNAME") and os.getenv("BETFLAG_PASSWORD"))
    return bool(os.getenv("BETFLAG_API_KEY") and os.getenv("BETFLAG_API_KEY_NAME") and (has_session or has_login))


def _configured(venue_id: str) -> bool:
    if venue_id == "betfair":
        return betfair_configured()
    if venue_id == "betflag":
        return _betflag_configured()
    spec = DIRECT_VENUES[venue_id]
    return bool(spec.credential_env) and all(os.getenv(name) for name in spec.credential_env)


def _record(conn: sqlite3.Connection, payload: dict[str, object]) -> None:
    existing = conn.execute(
        "SELECT last_success_at FROM direct_venue_status WHERE venue_id=?", (payload["venue_id"],)
    ).fetchone()
    last_success = payload.get("last_success_at")
    if not last_success and existing is not None:
        last_success = existing[0]
    payload["last_success_at"] = last_success
    columns = (
        "venue_id", "display_name", "kind", "italy_policy", "adm_concession",
        "personal_account_api", "market_data_api", "order_api", "configured", "health",
        "execution_ready", "live_enabled", "environment", "checked_at", "last_success_at",
        "message", "details_json",
    )
    conn.execute(
        f"INSERT INTO direct_venue_status ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) "
        "ON CONFLICT(venue_id) DO UPDATE SET "
        + ",".join(f"{c}=excluded.{c}" for c in columns if c != "venue_id"),
        tuple(payload.get(c) for c in columns),
    )
    history_columns = (
        "venue_id", "checked_at", "configured", "health", "execution_ready",
        "environment", "message", "details_json",
    )
    conn.execute(
        f"INSERT INTO direct_venue_checks ({','.join(history_columns)}) VALUES ({','.join('?' for _ in history_columns)})",
        tuple(payload.get(c) for c in history_columns),
    )
    conn.commit()


def monitor_once(db_path: str | Path | None = None) -> list[dict[str, object]]:
    path = Path(db_path or os.getenv("ARB_DB_PATH", "data/arbitrage.sqlite3"))
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    certification_store = VenueCertificationStore(conn)
    account_store = AccountSnapshotStore(conn)
    certifier = VenueCertifier(certification_store)
    live_enabled = _truthy("SPORTAGE_LIVE_EXECUTION")
    now = datetime.now(timezone.utc)
    output: list[dict[str, object]] = []

    try:
        for venue_id, spec in DIRECT_VENUES.items():
            configured = _configured(venue_id)
            environment = execution_environment(venue_id) if venue_id in {"betfair", "betflag"} else None
            details: dict[str, object] = {
                "docs_url": spec.docs_url,
                "self_service_api": spec.self_service_api,
                "credential_names": list(spec.credential_env),
            }
            health = "disabled_policy"
            ready = False
            message = spec.notes
            success_at: str | None = None

            if spec.italy_policy != ItalyPolicy.ADM_ELIGIBLE:
                message = f"Not enabled on Italian ACEPC ({spec.italy_policy.value}). {spec.notes}"
            elif not configured:
                health = "missing_credentials"
                message = "Official Italian direct venue is supported; credentials/API entitlement are not configured yet."
            elif venue_id in {"betfair", "betflag"}:
                try:
                    if venue_id == "betfair" and not os.getenv("BETFAIR_SESSION_TOKEN"):
                        # Keep the freshly-created session only in this process. It is never persisted.
                        os.environ["BETFAIR_SESSION_TOKEN"] = betfair_session_token()
                    report = certifier.certify(venue_id, environment=environment, ttl_hours=1.0)
                    details["certification"] = report.model_dump(mode="json")
                    snapshot = refresh_account_snapshot(venue_id, account_store)
                    details["account"] = {
                        "observed_at": snapshot.observed_at.isoformat(),
                        "available_balance": str(snapshot.available_balance),
                        "total_balance": None if snapshot.total_balance is None else str(snapshot.total_balance),
                    }
                    production_environment = venue_id == "betfair" or environment == "production"
                    ready = bool(report.success and production_environment)
                    health = "healthy" if report.success else "certification_failed"
                    message = (
                        "Authenticated direct API, market/preflight certification and account-funds read succeeded."
                        if ready
                        else "Direct API is reachable but production execution certification is not yet complete."
                    )
                    if report.success:
                        success_at = now.isoformat()
                except Exception as exc:
                    health = "error"
                    message = f"{type(exc).__name__}: {str(exc)[:500]}"
                    details["error_type"] = type(exc).__name__

            payload: dict[str, object] = {
                "venue_id": venue_id,
                "display_name": spec.display_name,
                "kind": spec.kind.value,
                "italy_policy": spec.italy_policy.value,
                "adm_concession": spec.adm_concession,
                "personal_account_api": int(spec.personal_account_api),
                "market_data_api": int(spec.market_data_api),
                "order_api": int(spec.order_api),
                "configured": int(configured),
                "health": health,
                "execution_ready": int(ready),
                "live_enabled": int(live_enabled),
                "environment": environment,
                "checked_at": now.isoformat(),
                "last_success_at": success_at,
                "message": message,
                "details_json": json.dumps(details, default=str, sort_keys=True),
            }
            _record(conn, payload)
            output.append(payload)
    finally:
        conn.close()
    return output


def main() -> None:
    rows = monitor_once()
    ready = [str(row["venue_id"]) for row in rows if row["execution_ready"]]
    healthy = [str(row["venue_id"]) for row in rows if row["health"] == "healthy"]
    configured = [str(row["venue_id"]) for row in rows if row["configured"]]
    print(
        json.dumps(
            {
                "checked": len(rows),
                "configured": configured,
                "healthy": healthy,
                "execution_ready": ready,
                "live_enabled": _truthy("SPORTAGE_LIVE_EXECUTION"),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
