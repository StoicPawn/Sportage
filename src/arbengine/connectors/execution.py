from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from arbengine.operators import operator_spec

from .base import AccountSnapshot, BetOrder, ExecutionConnector, ExecutionPreflight, ExecutionResult, ExecutionStatus
from .betfair import BetfairExchangeExecutionConnector
from .betflag import BetFlagExchangeExecutionConnector
from .execution_health import execution_available, mark_unhealthy
from .manual_retail import (
    Bet365ExecutionConnector,
    BetssonExecutionConnector,
    BwinExecutionConnector,
    CodereExecutionConnector,
    EurobetExecutionConnector,
    GoldbetExecutionConnector,
    LottomaticaExecutionConnector,
    Planetwin365ExecutionConnector,
    SNAIExecutionConnector,
    SisalExecutionConnector,
    WilliamHillExecutionConnector,
    WinamaxExecutionConnector,
)

BetfairExchangeExecutionConnector.automatic_execution = True
BetFlagExchangeExecutionConnector.automatic_execution = True

_EXECUTION_CONNECTORS: dict[str, type[ExecutionConnector]] = {
    "bet365": Bet365ExecutionConnector,
    "betfair": BetfairExchangeExecutionConnector,
    "snai": SNAIExecutionConnector,
    "sisal": SisalExecutionConnector,
    "eurobet": EurobetExecutionConnector,
    "goldbet": GoldbetExecutionConnector,
    "lottomatica": LottomaticaExecutionConnector,
    "planetwin365": Planetwin365ExecutionConnector,
    "betsson": BetssonExecutionConnector,
    "codere": CodereExecutionConnector,
    "betflag": BetFlagExchangeExecutionConnector,
    "bwin": BwinExecutionConnector,
    "william_hill": WilliamHillExecutionConnector,
    "winamax": WinamaxExecutionConnector,
}


def _cooldown_seconds() -> float:
    try:
        return max(1.0, float(os.getenv("SPORTAGE_EXECUTION_VENUE_COOLDOWN_SECONDS", "60")))
    except ValueError:
        return 60.0


def _enabled(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).strip().lower() not in {"0", "false", "no", "off"}


def _db_path() -> Path:
    db = Path(os.getenv("ARB_DB_PATH", "data/arbitrage.sqlite3"))
    db.parent.mkdir(parents=True, exist_ok=True)
    return db


def _live_certification_valid(operator_id: str) -> tuple[bool, str]:
    if not _enabled("SPORTAGE_REQUIRE_LIVE_CERTIFICATION"):
        return True, "Live certification gate explicitly disabled."
    from arbengine.venue_certification import VenueCertificationStore, execution_environment

    environment = execution_environment(operator_id)
    if operator_id == "betflag" and environment != "production":
        return False, "BetFlag staging is test-only and cannot hedge/rescue real-money live execution."

    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    try:
        store = VenueCertificationStore(conn)
        report = store.latest(operator_id, environment)
        if report is None:
            return False, f"No venue certification for {operator_id}/{environment}."
        if not report.success:
            return False, f"Latest venue certification failed for {operator_id}/{environment}."
        if not store.valid(operator_id, environment):
            return False, f"Venue certification expired for {operator_id}/{environment}."
        return True, f"Venue certification valid for {operator_id}/{environment} until {report.expires_at.isoformat()}."
    finally:
        conn.close()


def _live_order_funded(operator_id: str, order: BetOrder) -> tuple[bool, str, AccountSnapshot | None]:
    if not _enabled("SPORTAGE_REQUIRE_ACCOUNT_FUNDS"):
        return True, "Account funds gate explicitly disabled.", None
    from arbengine.account_funds import AccountSnapshotStore, assert_order_funded, refresh_account_snapshot

    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    try:
        store = AccountSnapshotStore(conn)
        snapshot = refresh_account_snapshot(operator_id, store)
        required = assert_order_funded(order, snapshot)
        return (
            True,
            f"{operator_id} free balance {snapshot.available_balance:.2f}; order liability {required:.2f}.",
            snapshot,
        )
    except Exception as exc:
        return False, f"Account funds check failed: {type(exc).__name__}: {exc}", None
    finally:
        conn.close()


def _authorize_canary_order(order: BetOrder) -> tuple[bool, str, int | None]:
    # Do not consume canary attempt budget when the master switch guarantees that
    # the underlying connector cannot send a real order.
    if not _enabled("SPORTAGE_LIVE_EXECUTION", "false"):
        return True, "Master live switch is off; no canary API attempt reserved.", None
    from arbengine.canary import CanaryGuard

    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    try:
        try:
            attempt_id = CanaryGuard(conn).authorize_order(order)
            return True, "Canary order authorized.", attempt_id
        except Exception as exc:
            return False, f"Canary gate: {type(exc).__name__}: {exc}", None
    finally:
        conn.close()


def _finish_canary_order(attempt_id: int | None, status: str, message: str | None = None) -> None:
    if attempt_id is None:
        return
    from arbengine.canary import CanaryGuard

    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    try:
        CanaryGuard(conn).finish_order(attempt_id, status, message)
    finally:
        conn.close()


class HealthTrackedExecutionConnector(ExecutionConnector):
    """Official connector protected by certification, funds, canary and venue-health gates."""

    def __init__(self, inner: ExecutionConnector) -> None:
        self.inner = inner
        self.operator_id = inner.operator_id
        self.automatic_execution = bool(
            getattr(inner, "automatic_execution", False) and execution_available(self.operator_id)
        )

    def certification_probe(self):
        return self.inner.certification_probe()

    def account_snapshot(self) -> AccountSnapshot:
        from arbengine.account_funds import refresh_account_snapshot

        return refresh_account_snapshot(self.operator_id)

    def _trip(self, reason: str) -> None:
        if getattr(self.inner, "automatic_execution", False):
            mark_unhealthy(self.operator_id, reason, cooldown_seconds=_cooldown_seconds())
            self.automatic_execution = False

    def preflight(self, order: BetOrder) -> ExecutionPreflight:
        if not execution_available(self.operator_id):
            self.automatic_execution = False
            return ExecutionPreflight(
                operator_id=self.operator_id,
                ok=False,
                message="Execution venue is temporarily disabled by Sportage circuit breaker.",
            )
        try:
            result = self.inner.preflight(order)
        except Exception as exc:
            self._trip(f"preflight {type(exc).__name__}: {exc}")
            raise
        if not result.ok:
            self._trip(f"preflight rejected: {result.message}")
        return result

    def place_order(self, order: BetOrder, *, live: bool = False) -> ExecutionResult:
        attempt_id: int | None = None
        if live:
            certified, reason = _live_certification_valid(self.operator_id)
            if not certified:
                self._trip(f"live certification gate: {reason}")
                return ExecutionResult(
                    operator_id=self.operator_id, status=ExecutionStatus.REJECTED,
                    message=f"Live execution blocked: {reason}",
                    customer_order_ref=order.customer_order_ref,
                    requested_stake=order.stake, requested_odds=order.limit_odds,
                )
            funded, funding_reason, _ = _live_order_funded(self.operator_id, order)
            if not funded:
                self._trip(f"live account funds gate: {funding_reason}")
                return ExecutionResult(
                    operator_id=self.operator_id, status=ExecutionStatus.REJECTED,
                    message=f"Live execution blocked: {funding_reason}",
                    customer_order_ref=order.customer_order_ref,
                    requested_stake=order.stake, requested_odds=order.limit_odds,
                )
            canary_ok, canary_reason, attempt_id = _authorize_canary_order(order)
            if not canary_ok:
                # Canary exhaustion is a system risk limit, not a venue-health failure.
                return ExecutionResult(
                    operator_id=self.operator_id, status=ExecutionStatus.REJECTED,
                    message=f"Live execution blocked: {canary_reason}",
                    customer_order_ref=order.customer_order_ref,
                    requested_stake=order.stake, requested_odds=order.limit_odds,
                )
        try:
            result = self.inner.place_order(order, live=live)
        except Exception as exc:
            if live:
                _finish_canary_order(attempt_id, "exception", f"{type(exc).__name__}: {exc}")
                self._trip(f"placement {type(exc).__name__}: {exc}")
            raise
        if live:
            _finish_canary_order(attempt_id, result.status.value, result.message)
        if live and result.status in {
            ExecutionStatus.REJECTED,
            ExecutionStatus.PARTIALLY_MATCHED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.UNKNOWN,
        }:
            self._trip(f"placement result: {result.status.value}: {result.message}")
        return result

    def reconcile_order(
        self,
        *,
        bet_id: str | None = None,
        customer_order_ref: str | None = None,
        market_id: str | None = None,
        order: BetOrder | None = None,
    ) -> ExecutionResult:
        if self.operator_id == "betflag":
            return self.inner.reconcile_order(
                bet_id=bet_id, customer_order_ref=customer_order_ref,
                market_id=market_id, order=order,
            )
        return self.inner.reconcile_order(
            bet_id=bet_id,
            customer_order_ref=customer_order_ref,
        )

    def cancel_order(
        self,
        bet_id: str,
        *,
        market_id: str | None = None,
        live: bool = False,
    ) -> ExecutionResult:
        # Never block risk reduction on certification, funds or canary state.
        return self.inner.cancel_order(bet_id, market_id=market_id, live=live)


def build_execution_connector(operator: str) -> ExecutionConnector:
    spec = operator_spec(operator)
    connector_cls = _EXECUTION_CONNECTORS[spec.operator_id]
    connector = connector_cls()
    if getattr(connector, "automatic_execution", False):
        return HealthTrackedExecutionConnector(connector)
    return connector


def execution_connector_ids() -> tuple[str, ...]:
    return tuple(sorted(_EXECUTION_CONNECTORS))
