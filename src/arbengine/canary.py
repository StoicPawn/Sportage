from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .connectors.base import BetOrder, required_account_funds


CANARY_SCHEMA = """
CREATE TABLE IF NOT EXISTS canary_order_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    operator_id TEXT NOT NULL,
    customer_order_ref TEXT,
    liability TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'authorized',
    message TEXT
);
CREATE INDEX IF NOT EXISTS idx_canary_order_attempts_day
ON canary_order_attempts(created_at, operator_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_canary_unique_order_ref
ON canary_order_attempts(operator_id, customer_order_ref)
WHERE customer_order_ref IS NOT NULL;
"""


class CanaryRiskError(ValueError):
    pass


class CanaryPolicy(BaseModel):
    enabled: bool = True
    max_leg_stake: Decimal = Field(default=Decimal("5.00"), gt=0)
    max_order_liability: Decimal = Field(default=Decimal("7.50"), gt=0)
    max_execution_capital: Decimal = Field(default=Decimal("12.00"), gt=0)
    max_live_executions_per_day: int = Field(default=3, ge=1)
    max_daily_prepared_capital: Decimal = Field(default=Decimal("36.00"), gt=0)
    max_active_live_executions: int = Field(default=1, ge=1)
    max_api_order_attempts_per_day: int = Field(default=6, ge=1)
    max_daily_api_liability: Decimal = Field(default=Decimal("30.00"), gt=0)


def _env_enabled(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).strip().lower() not in {"0", "false", "no", "off"}


def load_canary_policy(path: str | Path | None = None) -> CanaryPolicy:
    if path is None:
        path = os.getenv("SPORTAGE_CANARY_POLICY", "config/canary_policy.example.json")
    candidate = Path(path)
    policy = (
        CanaryPolicy.model_validate(json.loads(candidate.read_text(encoding="utf-8")))
        if candidate.exists()
        else CanaryPolicy()
    )
    if not _env_enabled("SPORTAGE_CANARY_MODE"):
        return policy.model_copy(update={"enabled": False})
    return policy


def _day_start(now: datetime | None = None) -> str:
    value = now or datetime.now(timezone.utc)
    return value.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def _order_from_payload(payload: dict[str, Any]) -> BetOrder:
    return BetOrder.model_validate(payload)


def _plan_liabilities(plan: dict[str, Any]) -> list[Decimal]:
    liabilities: list[Decimal] = []
    for leg in plan.get("legs") or []:
        order_payload = leg.get("order") if isinstance(leg, dict) else None
        if not isinstance(order_payload, dict):
            continue
        liabilities.append(required_account_funds(_order_from_payload(order_payload)))
    return liabilities


def _plan_stakes(plan: dict[str, Any]) -> list[Decimal]:
    stakes: list[Decimal] = []
    for leg in plan.get("legs") or []:
        order_payload = leg.get("order") if isinstance(leg, dict) else None
        if not isinstance(order_payload, dict):
            continue
        stakes.append(Decimal(str(order_payload.get("stake", "0"))))
    return stakes


class CanaryGuard:
    """Absolute live-risk limits independent from strategy profitability logic."""

    TERMINAL_STATUSES = {"completed", "rescued", "aborted"}

    def __init__(self, conn: sqlite3.Connection, policy: CanaryPolicy | None = None) -> None:
        self.conn = conn
        self.policy = policy or load_canary_policy()
        self.conn.executescript(CANARY_SCHEMA)
        self.conn.commit()

    def assert_plan(self, plan: dict[str, Any]) -> None:
        if not self.policy.enabled:
            return
        liabilities = _plan_liabilities(plan)
        stakes = _plan_stakes(plan)
        if not liabilities:
            raise CanaryRiskError("Live canary plan has no executable legs")
        if any(stake > self.policy.max_leg_stake for stake in stakes):
            largest = max(stakes)
            raise CanaryRiskError(
                f"Canary max leg stake is €{self.policy.max_leg_stake:.2f}; plan contains €{largest:.2f}"
            )
        if any(value > self.policy.max_order_liability for value in liabilities):
            largest = max(liabilities)
            raise CanaryRiskError(
                f"Canary max order liability is €{self.policy.max_order_liability:.2f}; plan contains €{largest:.2f}"
            )
        execution_capital = sum(liabilities, Decimal("0"))
        if execution_capital > self.policy.max_execution_capital:
            raise CanaryRiskError(
                f"Canary execution capital €{execution_capital:.2f} exceeds €{self.policy.max_execution_capital:.2f}"
            )

        start = _day_start()
        rows = list(
            self.conn.execute(
                "SELECT status, plan_json FROM execution_runs WHERE live=1 AND created_at>=?",
                (start,),
            )
        )
        if len(rows) >= self.policy.max_live_executions_per_day:
            raise CanaryRiskError(
                f"Canary daily live execution limit {self.policy.max_live_executions_per_day} reached"
            )
        active = sum(1 for row in rows if row["status"] not in self.TERMINAL_STATUSES)
        if active >= self.policy.max_active_live_executions:
            raise CanaryRiskError(
                f"Canary allows only {self.policy.max_active_live_executions} active live execution(s)"
            )

        daily_capital = Decimal("0")
        for row in rows:
            try:
                daily_capital += sum(_plan_liabilities(json.loads(row["plan_json"])), Decimal("0"))
            except Exception as exc:
                raise CanaryRiskError("Cannot audit an existing live plan for daily canary capital") from exc
        if daily_capital + execution_capital > self.policy.max_daily_prepared_capital:
            raise CanaryRiskError(
                f"Canary daily prepared capital would exceed €{self.policy.max_daily_prepared_capital:.2f}"
            )

    def authorize_order(self, order: BetOrder) -> int | None:
        if not self.policy.enabled:
            return None
        liability = required_account_funds(order)
        if liability > self.policy.max_order_liability:
            raise CanaryRiskError(
                f"Order liability €{liability:.2f} exceeds canary limit €{self.policy.max_order_liability:.2f}"
            )
        start = _day_start()
        row = self.conn.execute(
            """SELECT COUNT(*) AS n, COALESCE(SUM(CAST(liability AS REAL)), 0) AS liability
            FROM canary_order_attempts WHERE created_at>=?""",
            (start,),
        ).fetchone()
        attempts = int(row["n"])
        daily_liability = Decimal(str(row["liability"] or 0))
        if attempts >= self.policy.max_api_order_attempts_per_day:
            raise CanaryRiskError(
                f"Canary API order-attempt limit {self.policy.max_api_order_attempts_per_day} reached"
            )
        if daily_liability + liability > self.policy.max_daily_api_liability:
            raise CanaryRiskError(
                f"Canary daily API liability would exceed €{self.policy.max_daily_api_liability:.2f}"
            )
        try:
            cursor = self.conn.execute(
                """INSERT INTO canary_order_attempts
                (created_at, operator_id, customer_order_ref, liability, status)
                VALUES (?, ?, ?, ?, 'authorized')""",
                (
                    datetime.now(timezone.utc).isoformat(),
                    order.operator_id,
                    order.customer_order_ref,
                    str(liability),
                ),
            )
            self.conn.commit()
            return int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise CanaryRiskError(
                f"Duplicate canary order reference for {order.operator_id}: {order.customer_order_ref}"
            ) from exc

    def finish_order(self, attempt_id: int | None, status: str, message: str | None = None) -> None:
        if attempt_id is None:
            return
        self.conn.execute(
            "UPDATE canary_order_attempts SET status=?, message=? WHERE id=?",
            (status[:100], None if message is None else message[:2000], attempt_id),
        )
        self.conn.commit()

    def today_summary(self) -> dict[str, Any]:
        start = _day_start()
        live_rows = list(
            self.conn.execute(
                "SELECT status, plan_json FROM execution_runs WHERE live=1 AND created_at>=?",
                (start,),
            )
        )
        prepared = Decimal("0")
        for row in live_rows:
            prepared += sum(_plan_liabilities(json.loads(row["plan_json"])), Decimal("0"))
        attempt = self.conn.execute(
            """SELECT COUNT(*) AS n, COALESCE(SUM(CAST(liability AS REAL)), 0) AS liability
            FROM canary_order_attempts WHERE created_at>=?""",
            (start,),
        ).fetchone()
        return {
            "enabled": self.policy.enabled,
            "live_executions": len(live_rows),
            "active_live_executions": sum(
                1 for row in live_rows if row["status"] not in self.TERMINAL_STATUSES
            ),
            "prepared_capital": prepared,
            "api_order_attempts": int(attempt["n"]),
            "api_liability": Decimal(str(attempt["liability"] or 0)),
            "policy": self.policy.model_dump(mode="json"),
        }
