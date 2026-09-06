from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, Field, field_validator


Money = Annotated[Decimal, Field(gt=0)]
Odds = Annotated[Decimal, Field(gt=1)]


class MarketType(str, Enum):
    H2H = "h2h"
    MONEYLINE = "moneyline"
    ONE_X_TWO = "1x2"
    TOTALS = "totals"
    SPREADS = "spreads"


class Quote(BaseModel):
    event_id: str
    source_event_id: str | None = None
    operator_id: str | None = None
    sport: str
    commence_time: datetime
    home: str
    away: str
    market: MarketType
    outcome: str
    bookmaker: str
    odds: Odds
    expected_outcomes: int = Field(default=2, ge=2, le=20)
    period: str = "full_time"
    market_line: Decimal | None = None
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source: str = "unknown"
    deep_link: str | None = None
    # Provider-native execution references. They survive normalization and SQL persistence.
    source_market_id: str | None = None
    source_selection_id: str | None = None
    source_market_version: str | None = None
    available_size: Decimal | None = Field(default=None, ge=0)

    @field_validator("commence_time", "observed_at")
    @classmethod
    def tz_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    @property
    def market_signature(self) -> str:
        line = "" if self.market_line is None else str(self.market_line.normalize())
        return f"{self.market.value}:{self.period}:{line}"


class SettlementResult(BaseModel):
    """Observed settlement for one exact event+market signature."""

    event_id: str
    market_signature: str
    winning_outcome: str
    settled_at: datetime
    source: str = "manual"
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("settled_at", "observed_at")
    @classmethod
    def result_tz_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    @property
    def event_market_key(self) -> str:
        return f"{self.event_id}|{self.market_signature}"


class CostProfile(BaseModel):
    """Conservative execution-cost assumptions for one bookmaker/exchange."""

    bookmaker: str = "*"
    commission_on_winnings_pct: Decimal = Field(default=Decimal("0"), ge=0, lt=1)
    stake_fee_pct: Decimal = Field(default=Decimal("0"), ge=0, lt=1)
    fixed_cost_per_bet: Decimal = Field(default=Decimal("0"), ge=0)
    slippage_bps: Decimal = Field(default=Decimal("0"), ge=0, le=5000)
    min_stake: Decimal = Field(default=Decimal("0.01"), gt=0)
    max_stake: Decimal | None = Field(default=None, gt=0)


class Leg(BaseModel):
    outcome: str
    bookmaker: str
    operator_id: str | None = None
    odds: Odds
    effective_odds: Decimal
    stake: Decimal
    cash_outlay: Decimal
    net_return_if_win: Decimal
    quote_age_seconds: float
    estimated_placement_cost: Decimal = Decimal("0")
    estimated_win_commission: Decimal = Decimal("0")
    deep_link: str | None = None
    source_market_id: str | None = None
    source_selection_id: str | None = None
    source_market_version: str | None = None
    available_size: Decimal | None = None


class ArbitrageOpportunity(BaseModel):
    event_id: str
    sport: str
    event: str
    commence_time: datetime
    market: MarketType
    period: str = "full_time"
    market_line: Decimal | None = None
    gross_implied_sum: Decimal
    gross_roi: Decimal
    net_roi: Decimal
    capital_available: Decimal
    capital_used: Decimal
    unallocated_cash: Decimal
    guaranteed_payout: Decimal
    gross_guaranteed_profit: Decimal
    guaranteed_profit: Decimal
    estimated_costs: Decimal
    liquidity_limited: bool = False
    limiting_bookmakers: list[str] = Field(default_factory=list)
    legs: list[Leg]
    detected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def roi(self) -> Decimal:
        return self.net_roi

    @property
    def market_signature(self) -> str:
        line = "" if self.market_line is None else str(self.market_line.normalize())
        return f"{self.market.value}:{self.period}:{line}"

    @property
    def event_market_key(self) -> str:
        return f"{self.event_id}|{self.market_signature}"

    @property
    def fingerprint(self) -> str:
        legs = "|".join(
            f"{leg.outcome}:{leg.bookmaker}:{leg.odds}" for leg in sorted(self.legs, key=lambda x: x.outcome)
        )
        return f"{self.event_market_key}|{legs}"
