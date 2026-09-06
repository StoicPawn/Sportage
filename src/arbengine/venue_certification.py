from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable

from pydantic import BaseModel, Field

from .connectors.base import BetOrder, ExecutionConnector, TimeInForce
from .connectors.betfair import BetfairExchangeExecutionConnector, BetfairExchangeMarketDataConnector
from .connectors.betflag import (
    BetFlagExchangeExecutionConnector,
    BetFlagExchangeMarketDataConnector,
    _BetFlagClient,
)
from .connectors.execution import build_execution_connector
from .models import Quote
from .providers.base import OddsProvider


CERTIFICATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS venue_certifications (
    operator_id TEXT NOT NULL,
    environment TEXT NOT NULL,
    certified_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    success INTEGER NOT NULL,
    checks_json TEXT NOT NULL,
    PRIMARY KEY(operator_id, environment)
);
CREATE INDEX IF NOT EXISTS idx_venue_certifications_expiry
ON venue_certifications(success, expires_at);
"""


class CertificationCheck(BaseModel):
    name: str
    ok: bool
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class VenueCertification(BaseModel):
    operator_id: str
    environment: str
    certified_at: datetime
    expires_at: datetime
    success: bool
    checks: list[CertificationCheck]


class VenueCertificationStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn.executescript(CERTIFICATION_SCHEMA)
        self.conn.commit()

    def record(self, report: VenueCertification) -> None:
        self.conn.execute(
            """INSERT INTO venue_certifications
            (operator_id, environment, certified_at, expires_at, success, checks_json)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(operator_id, environment) DO UPDATE SET
                certified_at=excluded.certified_at,
                expires_at=excluded.expires_at,
                success=excluded.success,
                checks_json=excluded.checks_json""",
            (
                report.operator_id,
                report.environment,
                report.certified_at.isoformat(),
                report.expires_at.isoformat(),
                int(report.success),
                json.dumps([item.model_dump(mode="json") for item in report.checks], default=str),
            ),
        )
        self.conn.commit()

    def latest(self, operator_id: str, environment: str) -> VenueCertification | None:
        row = self.conn.execute(
            "SELECT * FROM venue_certifications WHERE operator_id=? AND environment=?",
            (operator_id, environment),
        ).fetchone()
        if row is None:
            return None
        return VenueCertification(
            operator_id=row["operator_id"],
            environment=row["environment"],
            certified_at=datetime.fromisoformat(row["certified_at"]),
            expires_at=datetime.fromisoformat(row["expires_at"]),
            success=bool(row["success"]),
            checks=[CertificationCheck.model_validate(item) for item in json.loads(row["checks_json"])],
        )

    def valid(
        self,
        operator_id: str,
        environment: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        report = self.latest(operator_id, environment)
        if report is None or not report.success:
            return False
        now = now or datetime.now(timezone.utc)
        return report.expires_at > now

    def all_latest(self) -> list[VenueCertification]:
        rows = self.conn.execute(
            "SELECT operator_id, environment FROM venue_certifications ORDER BY operator_id, environment"
        ).fetchall()
        result: list[VenueCertification] = []
        for row in rows:
            item = self.latest(row["operator_id"], row["environment"])
            if item is not None:
                result.append(item)
        return result


def execution_environment(operator_id: str) -> str:
    if operator_id == "betflag":
        return os.getenv("BETFLAG_ENVIRONMENT", "staging").strip().lower() or "staging"
    if operator_id == "betfair":
        return "production"
    return "manual"


def _credentials_check(operator_id: str, environment: str) -> CertificationCheck:
    if operator_id == "betfair":
        missing = [
            name
            for name in ("BETFAIR_APP_KEY", "BETFAIR_SESSION_TOKEN")
            if not os.getenv(name)
        ]
        return CertificationCheck(
            name="credentials",
            ok=not missing,
            message="Betfair API credentials configured." if not missing else f"Missing: {', '.join(missing)}",
        )
    if operator_id == "betflag":
        missing: list[str] = []
        if not os.getenv("BETFLAG_API_KEY"):
            missing.append("BETFLAG_API_KEY")
        if os.getenv("BETFLAG_API_KEY") and not os.getenv("BETFLAG_API_KEY_NAME"):
            missing.append("BETFLAG_API_KEY_NAME")
        if not os.getenv("BETFLAG_SESSION_TOKEN") and not (
            os.getenv("BETFLAG_USERNAME") and os.getenv("BETFLAG_PASSWORD")
        ):
            missing.append("BETFLAG_SESSION_TOKEN or BETFLAG_USERNAME+BETFLAG_PASSWORD")
        return CertificationCheck(
            name="credentials",
            ok=not missing,
            message=(
                f"BetFlag {environment} credentials configured."
                if not missing
                else f"Missing: {', '.join(missing)}"
            ),
        )
    return CertificationCheck(
        name="credentials",
        ok=False,
        message=f"{operator_id} is not an automatic Sportage venue.",
    )


ProviderFactory = Callable[[str, str], OddsProvider]
ExecutionFactory = Callable[[str, str], ExecutionConnector]


def _provider(operator_id: str, environment: str) -> OddsProvider:
    if operator_id == "betfair":
        return BetfairExchangeMarketDataConnector()
    if operator_id == "betflag":
        client = _BetFlagClient(environment=environment)
        return BetFlagExchangeMarketDataConnector(client=client)
    raise ValueError(f"No direct certification provider for {operator_id}")


def _execution(operator_id: str, environment: str) -> ExecutionConnector:
    if operator_id == "betfair":
        return BetfairExchangeExecutionConnector()
    if operator_id == "betflag":
        client = _BetFlagClient(environment=environment)
        return BetFlagExchangeExecutionConnector(client=client)
    return build_execution_connector(operator_id)


def _read_only_auth_probe(
    operator_id: str,
    connector: ExecutionConnector,
    *,
    market_id: str | None = None,
) -> dict[str, Any]:
    if operator_id == "betfair":
        # listCurrentOrders is authenticated but does not create/change an order.
        response = connector.client.call("listCurrentOrders", {"orderProjection": "ALL"})  # type: ignore[attr-defined]
        result = response.get("result") or {}
        return {
            "ok": True,
            "message": "Betfair session authenticated; current orders endpoint readable.",
            "current_order_count": len(result.get("currentOrders") or []),
        }
    if operator_id == "betflag":
        if not market_id:
            return {"ok": False, "message": "BetFlag auth probe needs a current market id."}
        client = connector.client  # type: ignore[attr-defined]
        data = client.request("GET", f"/offers/all/{market_id}", session=True)
        rows = ((data.get("offerte") or {}).get("o") or []) if isinstance(data, dict) else []
        return {
            "ok": True,
            "message": "BetFlag session authenticated; account offers endpoint readable.",
            "environment": client.environment,
            "visible_account_offer_count": len(rows),
        }
    probe = connector.certification_probe()
    return dict(probe)


class VenueCertifier:
    """Read-only/live-safe certification of an automatic execution venue.

    Certification never calls place_order. It authenticates, fetches direct market
    data, verifies native execution references/depth and performs a fresh preflight
    against one executable quote. The result is persisted and can be used as a live
    execution gate.
    """

    def __init__(
        self,
        store: VenueCertificationStore,
        *,
        provider_factory: ProviderFactory | None = None,
        execution_factory: ExecutionFactory | None = None,
    ) -> None:
        self.store = store
        self.provider_factory = provider_factory or _provider
        self.execution_factory = execution_factory or _execution

    def certify(
        self,
        operator_id: str,
        *,
        environment: str | None = None,
        ttl_hours: float = 24.0,
        min_probe_stake: Decimal = Decimal("1.00"),
    ) -> VenueCertification:
        environment = (environment or execution_environment(operator_id)).lower()
        now = datetime.now(timezone.utc)
        checks: list[CertificationCheck] = []

        credential = _credentials_check(operator_id, environment)
        checks.append(credential)
        if not credential.ok:
            report = VenueCertification(
                operator_id=operator_id,
                environment=environment,
                certified_at=now,
                expires_at=now,
                success=False,
                checks=checks,
            )
            self.store.record(report)
            return report

        try:
            connector = self.execution_factory(operator_id, environment)
        except Exception as exc:
            connector = None
            checks.append(
                CertificationCheck(
                    name="connector_init",
                    ok=False,
                    message=f"{type(exc).__name__}: {exc}",
                )
            )

        try:
            quotes = list(self.provider_factory(operator_id, environment).fetch_quotes())
            native = [
                q
                for q in quotes
                if q.operator_id == operator_id
                and q.source_market_id
                and q.source_selection_id
                and q.available_size is not None
                and q.available_size > 0
            ]
            checks.append(
                CertificationCheck(
                    name="direct_market_data",
                    ok=bool(native),
                    message=(
                        f"Observed {len(quotes)} direct quotes; {len(native)} have executable native refs/depth."
                        if native
                        else "No executable direct quote with native market/selection/depth was observed."
                    ),
                    details={"quote_count": len(quotes), "native_quote_count": len(native)},
                )
            )
        except Exception as exc:
            native = []
            checks.append(
                CertificationCheck(
                    name="direct_market_data",
                    ok=False,
                    message=f"{type(exc).__name__}: {exc}",
                )
            )

        if connector is not None:
            try:
                market_id = native[0].source_market_id if native else None
                probe = _read_only_auth_probe(operator_id, connector, market_id=market_id)
                checks.append(
                    CertificationCheck(
                        name="authenticated_session",
                        ok=bool(probe.get("ok", False)),
                        message=str(probe.get("message") or "Authentication probe completed."),
                        details={k: v for k, v in probe.items() if k not in {"ok", "message"}},
                    )
                )
            except Exception as exc:
                checks.append(
                    CertificationCheck(
                        name="authenticated_session",
                        ok=False,
                        message=f"{type(exc).__name__}: {exc}",
                    )
                )
        else:
            checks.append(
                CertificationCheck(
                    name="authenticated_session",
                    ok=False,
                    message="Skipped because connector initialization failed.",
                )
            )

        if connector is not None and native:
            quote = max(native, key=lambda q: q.available_size or Decimal("0"))
            probe_stake = min(min_probe_stake, quote.available_size or min_probe_stake)
            order = BetOrder(
                operator_id=operator_id,
                market_id=quote.source_market_id or "",
                selection_id=quote.source_selection_id or "",
                stake=probe_stake,
                limit_odds=quote.odds,
                outcome=quote.outcome,
                event_id=quote.event_id,
                time_in_force=TimeInForce.FILL_OR_KILL,
                min_fill_size=probe_stake,
                market_version=quote.source_market_version,
            )
            try:
                preflight = connector.preflight(order)
                checks.append(
                    CertificationCheck(
                        name="execution_preflight",
                        ok=preflight.ok,
                        message=preflight.message,
                        details={
                            "market_id": order.market_id,
                            "selection_id": order.selection_id,
                            "probe_stake": str(probe_stake),
                            "current_odds": None if preflight.current_odds is None else str(preflight.current_odds),
                            "available_size": None if preflight.available_size is None else str(preflight.available_size),
                            "market_version": preflight.market_version,
                        },
                    )
                )
            except Exception as exc:
                checks.append(
                    CertificationCheck(
                        name="execution_preflight",
                        ok=False,
                        message=f"{type(exc).__name__}: {exc}",
                    )
                )
        else:
            checks.append(
                CertificationCheck(
                    name="execution_preflight",
                    ok=False,
                    message="Skipped because executable market data or connector initialization failed.",
                )
            )

        success = all(check.ok for check in checks)
        report = VenueCertification(
            operator_id=operator_id,
            environment=environment,
            certified_at=now,
            expires_at=now + timedelta(hours=ttl_hours) if success else now,
            success=success,
            checks=checks,
        )
        self.store.record(report)
        return report
