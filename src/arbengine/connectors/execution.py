from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from arbengine.operators import operator_spec

from .base import BetOrder, ExecutionConnector, ExecutionPreflight, ExecutionResult, ExecutionStatus
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

# Only verified official APIs may advertise automatic execution.
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


def _live_certification_required() -> bool:
    return os.getenv("SPORTAGE_REQUIRE_LIVE_CERTIFICATION", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _live_certification_valid(operator_id: str) -> tuple[bool, str]:
    """Read the durable certification gate lazily to avoid connector import cycles."""
    if not _live_certification_required():
        return True, "Live certification gate explicitly disabled."
    from arbengine.venue_certification import VenueCertificationStore, execution_environment

    db = Path(os.getenv("ARB_DB_PATH", "data/arbitrage.sqlite3"))
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        store = VenueCertificationStore(conn)
        environment = execution_environment(operator_id)
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


class HealthTrackedExecutionConnector(ExecutionConnector):
    """Automatic connector wrapper with circuit breaker and durable live certification gate."""

    def __init__(self, inner: ExecutionConnector) -> None:
        self.inner = inner
        self.operator_id = inner.operator_id
        self.automatic_execution = bool(
            getattr(inner, "automatic_execution", False) and execution_available(self.operator_id)
        )

    def certification_probe(self):
        return self.inner.certification_probe()

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
        if live:
            certified, reason = _live_certification_valid(self.operator_id)
            if not certified:
                self._trip(f"live certification gate: {reason}")
                return ExecutionResult(
                    operator_id=self.operator_id,
                    status=ExecutionStatus.REJECTED,
                    message=f"Live execution blocked: {reason}",
                    customer_order_ref=order.customer_order_ref,
                    requested_stake=order.stake,
                    requested_odds=order.limit_odds,
                )
        try:
            result = self.inner.place_order(order, live=live)
        except Exception as exc:
            if live:
                self._trip(f"placement {type(exc).__name__}: {exc}")
            raise
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
        # BetFlag can use original market/order context because its public API has no
        # documented client idempotency key. Betfair reconciles directly by customerOrderRef.
        if self.operator_id == "betflag":
            return self.inner.reconcile_order(
                bet_id=bet_id,
                customer_order_ref=customer_order_ref,
                market_id=market_id,
                order=order,
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
        # Cancellation/reconciliation remain available even when certification expires;
        # emergency risk reduction must never be blocked by a readiness gate.
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
