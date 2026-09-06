from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from arbengine.models import Quote


class BetSide(str, Enum):
    BACK = "BACK"
    LAY = "LAY"


class TimeInForce(str, Enum):
    DEFAULT = "default"
    FILL_OR_KILL = "fill_or_kill"


class ExecutionStatus(str, Enum):
    DRY_RUN = "dry_run"
    PENDING = "pending"
    ACCEPTED = "accepted"
    PARTIALLY_MATCHED = "partially_matched"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"
    MANUAL_REQUIRED = "manual_required"
    UNSUPPORTED = "unsupported"


class BetOrder(BaseModel):
    operator_id: str
    market_id: str
    selection_id: str
    side: BetSide = BetSide.BACK
    stake: Decimal = Field(gt=0)
    limit_odds: Decimal = Field(gt=1)
    customer_ref: str | None = None
    customer_order_ref: str | None = None
    customer_strategy_ref: str | None = "sportage"
    event_id: str | None = None
    outcome: str | None = None
    deep_link: str | None = None
    time_in_force: TimeInForce = TimeInForce.DEFAULT
    min_fill_size: Decimal | None = Field(default=None, gt=0)
    market_version: str | None = None


class ExecutionPreflight(BaseModel):
    operator_id: str
    ok: bool
    message: str
    market_open: bool | None = None
    current_odds: Decimal | None = None
    available_size: Decimal | None = None
    market_version: str | None = None
    raw: dict[str, Any] | list[Any] | None = None


class ExecutionResult(BaseModel):
    operator_id: str
    status: ExecutionStatus
    message: str
    bet_id: str | None = None
    customer_order_ref: str | None = None
    requested_stake: Decimal | None = None
    requested_odds: Decimal | None = None
    matched_stake: Decimal | None = None
    average_price_matched: Decimal | None = None
    remaining_stake: Decimal | None = None
    order_status: str | None = None
    raw: dict[str, Any] | list[Any] | None = None

    @property
    def fully_matched(self) -> bool:
        if self.requested_stake is None or self.matched_stake is None:
            return self.status == ExecutionStatus.ACCEPTED
        return self.matched_stake >= self.requested_stake


class MarketDataConnector(ABC):
    operator_id: str

    @abstractmethod
    def fetch_quotes(self) -> list[Quote]:
        raise NotImplementedError


class ExecutionConnector(ABC):
    operator_id: str

    def preflight(self, order: BetOrder) -> ExecutionPreflight:
        return ExecutionPreflight(
            operator_id=self.operator_id,
            ok=True,
            message="Connector has no live preflight endpoint; order contract validated locally.",
        )

    @abstractmethod
    def place_order(self, order: BetOrder, *, live: bool = False) -> ExecutionResult:
        raise NotImplementedError

    def reconcile_order(
        self,
        *,
        bet_id: str | None = None,
        customer_order_ref: str | None = None,
    ) -> ExecutionResult:
        return ExecutionResult(
            operator_id=self.operator_id,
            status=ExecutionStatus.UNSUPPORTED,
            message="Order reconciliation is not implemented for this connector.",
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
        return ExecutionResult(
            operator_id=self.operator_id,
            status=ExecutionStatus.UNSUPPORTED,
            message="Cancellation is not implemented for this connector.",
            bet_id=bet_id,
        )
