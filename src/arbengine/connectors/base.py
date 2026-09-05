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


class ExecutionStatus(str, Enum):
    DRY_RUN = "dry_run"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
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
    event_id: str | None = None
    outcome: str | None = None
    deep_link: str | None = None


class ExecutionResult(BaseModel):
    operator_id: str
    status: ExecutionStatus
    message: str
    bet_id: str | None = None
    requested_stake: Decimal | None = None
    requested_odds: Decimal | None = None
    matched_stake: Decimal | None = None
    average_price_matched: Decimal | None = None
    raw: dict[str, Any] | list[Any] | None = None


class MarketDataConnector(ABC):
    operator_id: str

    @abstractmethod
    def fetch_quotes(self) -> list[Quote]:
        raise NotImplementedError


class ExecutionConnector(ABC):
    operator_id: str

    @abstractmethod
    def place_order(self, order: BetOrder, *, live: bool = False) -> ExecutionResult:
        raise NotImplementedError

    def cancel_order(self, bet_id: str, *, live: bool = False) -> ExecutionResult:
        return ExecutionResult(
            operator_id=self.operator_id,
            status=ExecutionStatus.UNSUPPORTED,
            message="Cancellation is not implemented for this connector.",
        )
