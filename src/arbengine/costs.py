from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, Field

from .models import CostProfile
from .normalizer import canonical_name


class CostConfig(BaseModel):
    default: CostProfile = Field(default_factory=CostProfile)
    bookmakers: list[CostProfile] = Field(default_factory=list)


class CostBook:
    def __init__(self, config: CostConfig | None = None) -> None:
        self.config = config or CostConfig()
        self._by_name = {
            canonical_name(profile.bookmaker): profile
            for profile in self.config.bookmakers
            if profile.bookmaker != "*"
        }

    def for_bookmaker(self, bookmaker: str) -> CostProfile:
        return self._by_name.get(canonical_name(bookmaker), self.config.default)


def load_cost_config(path: str | Path | None) -> CostBook:
    if path is None:
        return CostBook()
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    return CostBook(CostConfig.model_validate(data))


def effective_odds(odds: Decimal, profile: CostProfile) -> Decimal:
    haircut = Decimal("1") - profile.slippage_bps / Decimal("10000")
    adjusted = odds * haircut
    return max(adjusted, Decimal("1.000001"))


def net_return_factor(odds: Decimal, profile: CostProfile) -> Decimal:
    adjusted = effective_odds(odds, profile)
    winnings = adjusted - Decimal("1")
    return Decimal("1") + winnings * (Decimal("1") - profile.commission_on_winnings_pct)
