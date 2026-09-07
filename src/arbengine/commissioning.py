from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_UP
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field

from .account_funds import AccountSnapshotStore, refresh_account_snapshot
from .canary import load_canary_policy
from .connectors.base import AccountSnapshot
from .connectors.betfair import BetfairExchangeMarketDataConnector
from .connectors.betflag import BetFlagExchangeMarketDataConnector, _BetFlagClient
from .execution_storage import ExecutionStore
from .models import Quote
from .providers.base import OddsProvider
from .providers.unified import normalize_quote
from .venue_certification import VenueCertificationStore, VenueCertifier


COMMISSIONING_SCHEMA = """
CREATE TABLE IF NOT EXISTS commissioning_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    success INTEGER NOT NULL,
    config_fingerprint TEXT NOT NULL,
    report_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_commissioning_runs_latest
ON commissioning_runs(id DESC, success, expires_at);
"""


class CommissioningError(RuntimeError):
    pass


class CommissioningCheck(BaseModel):
    name: str
    ok: bool
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class CommissioningReport(BaseModel):
    started_at: datetime
    completed_at: datetime
    expires_at: datetime
    success: bool
    config_fingerprint: str
    checks: list[CommissioningCheck]


class CommissioningStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn.executescript(COMMISSIONING_SCHEMA)
        self.conn.commit()

    def record(self, report: CommissioningReport) -> int:
        cur = self.conn.execute(
            """INSERT INTO commissioning_runs
            (started_at, completed_at, expires_at, success, config_fingerprint, report_json)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (
                report.started_at.isoformat(),
                report.completed_at.isoformat(),
                report.expires_at.isoformat(),
                int(report.success),
                report.config_fingerprint,
                json.dumps(report.model_dump(mode="json"), default=str),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def latest(self) -> CommissioningReport | None:
        row = self.conn.execute(
            "SELECT report_json FROM commissioning_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return CommissioningReport.model_validate(json.loads(row["report_json"]))

    def valid_current(self, *, fingerprint: str | None = None, now: datetime | None = None) -> bool:
        report = self.latest()
        if report is None or not report.success:
            return False
        fingerprint = fingerprint or safety_config_fingerprint()
        now = now or datetime.now(timezone.utc)
        return report.config_fingerprint == fingerprint and report.expires_at > now


def _enabled(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).strip().lower() not in {"0", "false", "no", "off"}


def _file_digest(env_name: str, default: str) -> dict[str, str | None]:
    value = os.getenv(env_name, default)
    path = Path(value)
    if not path.exists():
        return {"path": value, "sha256": None}
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"path": value, "sha256": digest}


def safety_config_fingerprint() -> str:
    """Hash only safety-relevant configuration, never credentials or secret values."""

    payload = {
        "betflag_environment": os.getenv("BETFLAG_ENVIRONMENT", "staging").strip().lower(),
        "canary_mode": os.getenv("SPORTAGE_CANARY_MODE", "true").strip().lower(),
        "require_live_certification": os.getenv("SPORTAGE_REQUIRE_LIVE_CERTIFICATION", "true").strip().lower(),
        "require_account_funds": os.getenv("SPORTAGE_REQUIRE_ACCOUNT_FUNDS", "true").strip().lower(),
        "rescue_balance_buffer_pct": os.getenv("SPORTAGE_RESCUE_BALANCE_BUFFER_PCT", "0.10").strip(),
        "canary_policy": _file_digest("SPORTAGE_CANARY_POLICY", "config/canary_policy.example.json"),
        "execution_policy": _file_digest("SPORTAGE_EXECUTION_POLICY", "config/execution_policy.example.json"),
        "cost_config": _file_digest("ARB_COST_CONFIG", "config/costs.example.json"),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def assert_current_commissioning(conn: sqlite3.Connection) -> CommissioningReport:
    if not _enabled("SPORTAGE_REQUIRE_COMMISSIONING"):
        now = datetime.now(timezone.utc)
        return CommissioningReport(
            started_at=now,
            completed_at=now,
            expires_at=now,
            success=True,
            config_fingerprint=safety_config_fingerprint(),
            checks=[CommissioningCheck(name="commissioning_gate", ok=True, message="Commissioning gate disabled.")],
        )
    store = CommissioningStore(conn)
    report = store.latest()
    if report is None:
        raise CommissioningError("Live execution blocked: no commissioning certificate. Run sportage-commission run.")
    fingerprint = safety_config_fingerprint()
    if not report.success:
        raise CommissioningError("Live execution blocked: latest commissioning run failed.")
    if report.config_fingerprint != fingerprint:
        raise CommissioningError("Live execution blocked: safety configuration changed after commissioning.")
    if report.expires_at <= datetime.now(timezone.utc):
        raise CommissioningError(
            f"Live execution blocked: commissioning expired at {report.expires_at.isoformat()}."
        )
    return report


ProviderFactory = Callable[[str], OddsProvider]
AccountFetcher = Callable[[str, AccountSnapshotStore | None], AccountSnapshot]


def _provider(operator_id: str) -> OddsProvider:
    if operator_id == "betfair":
        return BetfairExchangeMarketDataConnector()
    if operator_id == "betflag":
        return BetFlagExchangeMarketDataConnector(client=_BetFlagClient(environment="production"))
    raise ValueError(operator_id)


def _rescue_buffer() -> Decimal:
    try:
        value = Decimal(os.getenv("SPORTAGE_RESCUE_BALANCE_BUFFER_PCT", "0.10"))
    except Exception:
        value = Decimal("0.10")
    return max(Decimal("0"), min(value, Decimal("1")))


def _local_checks(conn: sqlite3.Connection) -> list[CommissioningCheck]:
    checks: list[CommissioningCheck] = []
    live_enabled = _enabled("SPORTAGE_LIVE_EXECUTION", "false")
    checks.append(
        CommissioningCheck(
            name="master_switch_off",
            ok=not live_enabled,
            message=(
                "SPORTAGE_LIVE_EXECUTION is false; commissioning is read-only."
                if not live_enabled
                else "SPORTAGE_LIVE_EXECUTION must be false during commissioning."
            ),
        )
    )

    betflag_env = os.getenv("BETFLAG_ENVIRONMENT", "staging").strip().lower() or "staging"
    policy = load_canary_policy()
    gates_ok = (
        policy.enabled
        and _enabled("SPORTAGE_REQUIRE_LIVE_CERTIFICATION")
        and _enabled("SPORTAGE_REQUIRE_ACCOUNT_FUNDS")
        and betflag_env == "production"
    )
    checks.append(
        CommissioningCheck(
            name="safety_configuration",
            ok=gates_ok,
            message=(
                "Canary, certification and account-funds gates enabled; BetFlag production selected."
                if gates_ok
                else "Require canary=true, live-certification=true, account-funds=true and BETFLAG_ENVIRONMENT=production."
            ),
            details={
                "canary_enabled": policy.enabled,
                "require_live_certification": _enabled("SPORTAGE_REQUIRE_LIVE_CERTIFICATION"),
                "require_account_funds": _enabled("SPORTAGE_REQUIRE_ACCOUNT_FUNDS"),
                "betflag_environment": betflag_env,
            },
        )
    )

    execution = ExecutionStore(conn)
    halted, reason = execution.halt_state()
    active = conn.execute(
        """SELECT COUNT(*) AS n FROM execution_runs
        WHERE live=1 AND status NOT IN ('completed','rescued','aborted')"""
    ).fetchone()["n"]
    locks = conn.execute("SELECT COUNT(*) AS n FROM execution_locks").fetchone()["n"]
    clean = not halted and int(active) == 0 and int(locks) == 0
    checks.append(
        CommissioningCheck(
            name="clean_execution_state",
            ok=clean,
            message=(
                "No global halt, active live executions or execution locks."
                if clean
                else "Commissioning requires a flat internal state: clear halt/exposures/locks first."
            ),
            details={"halted": halted, "halt_reason": reason, "active_live_runs": int(active), "locks": int(locks)},
        )
    )
    return checks


def _certification_check(
    conn: sqlite3.Connection,
    *,
    refresh_certifications: bool,
) -> CommissioningCheck:
    store = VenueCertificationStore(conn)
    details: dict[str, Any] = {}
    if refresh_certifications:
        certifier = VenueCertifier(store)
        for operator in ("betfair", "betflag"):
            report = certifier.certify(operator, environment="production")
            details[f"{operator}_refreshed"] = report.success
    validity = {operator: store.valid(operator, "production") for operator in ("betfair", "betflag")}
    details.update(validity)
    ok = all(validity.values())
    return CommissioningCheck(
        name="production_venue_certification",
        ok=ok,
        message=(
            "Betfair and BetFlag production certifications are current."
            if ok
            else "Both Betfair and BetFlag need a current successful production certification."
        ),
        details=details,
    )


def _account_check(
    conn: sqlite3.Connection,
    *,
    account_fetcher: AccountFetcher,
) -> CommissioningCheck:
    snapshots = AccountSnapshotStore(conn)
    policy = load_canary_policy()
    minimum = (policy.max_order_liability * (Decimal("1") + _rescue_buffer())).quantize(
        Decimal("0.01"), rounding=ROUND_UP
    )
    details: dict[str, Any] = {"minimum_free_balance": str(minimum)}
    ok = True
    messages: list[str] = []
    for operator in ("betfair", "betflag"):
        try:
            snapshot = account_fetcher(operator, snapshots)
            details[operator] = {
                "environment": snapshot.environment,
                "available_balance": str(snapshot.available_balance),
                "locked_balance": None if snapshot.locked_balance is None else str(snapshot.locked_balance),
                "exposure": None if snapshot.exposure is None else str(snapshot.exposure),
            }
            existing_exposure = abs(snapshot.exposure or Decimal("0")) > Decimal("0.01")
            locked = (snapshot.locked_balance or Decimal("0")) > Decimal("0.01")
            venue_ok = (
                snapshot.environment == "production"
                and snapshot.available_balance >= minimum
                and not existing_exposure
                and not locked
            )
            if not venue_ok:
                ok = False
                messages.append(f"{operator} not flat/funded for canary")
        except Exception as exc:
            ok = False
            details[operator] = {"error": f"{type(exc).__name__}: {exc}"}
            messages.append(f"{operator} account read failed")
    return CommissioningCheck(
        name="flat_funded_accounts",
        ok=ok,
        message=(
            f"Both exchange accounts are flat and have at least €{minimum:.2f} free."
            if ok
            else "; ".join(messages) or "Account funding check failed."
        ),
        details=details,
    )


def _market_mapping_check(provider_factory: ProviderFactory) -> CommissioningCheck:
    by_operator: dict[str, list[Quote]] = {}
    details: dict[str, Any] = {}
    for operator in ("betfair", "betflag"):
        try:
            raw = list(provider_factory(operator).fetch_quotes())
            normalized = [q for item in raw if (q := normalize_quote(item)) is not None]
            executable = [
                q for q in normalized
                if q.operator_id == operator
                and q.expected_outcomes == 2
                and q.source_market_id
                and q.source_selection_id
                and q.available_size is not None
                and q.available_size > 0
            ]
            by_operator[operator] = executable
            details[f"{operator}_raw"] = len(raw)
            details[f"{operator}_executable"] = len(executable)
        except Exception as exc:
            by_operator[operator] = []
            details[f"{operator}_error"] = f"{type(exc).__name__}: {exc}"

    groups: dict[tuple[str, str], dict[str, set[str]]] = {}
    for operator, quotes in by_operator.items():
        for quote in quotes:
            key = (quote.event_id, quote.market_signature)
            groups.setdefault(key, {}).setdefault(operator, set()).add(quote.outcome)

    common: list[tuple[str, str, list[str]]] = []
    for (event_id, market_signature), operators in groups.items():
        betfair = operators.get("betfair", set())
        betflag = operators.get("betflag", set())
        if len(betfair) == 2 and betfair == betflag:
            common.append((event_id, market_signature, sorted(betfair)))

    details["common_executable_markets"] = len(common)
    if common:
        event_id, signature, outcomes = common[0]
        details["sample"] = {"event_id": event_id, "market_signature": signature, "outcomes": outcomes}
    return CommissioningCheck(
        name="cross_exchange_market_mapping",
        ok=bool(common),
        message=(
            f"Found {len(common)} two-outcome market(s) mapped consistently on Betfair and BetFlag."
            if common
            else "No current two-outcome market maps consistently across both exchanges with native ids and depth."
        ),
        details=details,
    )


def run_commissioning(
    conn: sqlite3.Connection,
    *,
    ttl_minutes: float = 30.0,
    refresh_certifications: bool = True,
    provider_factory: ProviderFactory | None = None,
    account_fetcher: AccountFetcher = refresh_account_snapshot,
) -> CommissioningReport:
    started = datetime.now(timezone.utc)
    fingerprint = safety_config_fingerprint()
    checks = _local_checks(conn)

    # Do not make remote calls while local safety prerequisites are already red.
    if all(check.ok for check in checks):
        try:
            checks.append(
                _certification_check(conn, refresh_certifications=refresh_certifications)
            )
        except Exception as exc:
            checks.append(
                CommissioningCheck(
                    name="production_venue_certification",
                    ok=False,
                    message=f"Certification refresh failed: {type(exc).__name__}: {exc}",
                )
            )
        checks.append(_account_check(conn, account_fetcher=account_fetcher))
        checks.append(_market_mapping_check(provider_factory or _provider))
    else:
        checks.append(
            CommissioningCheck(
                name="remote_checks",
                ok=False,
                message="Skipped remote checks until all local safety prerequisites pass.",
            )
        )

    success = all(check.ok for check in checks)
    completed = datetime.now(timezone.utc)
    expires = completed + timedelta(minutes=max(1.0, ttl_minutes)) if success else completed
    report = CommissioningReport(
        started_at=started,
        completed_at=completed,
        expires_at=expires,
        success=success,
        config_fingerprint=fingerprint,
        checks=checks,
    )
    CommissioningStore(conn).record(report)
    return report
