from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from pydantic import BaseModel, Field, field_validator

from .normalizer import canonical_name


class LiquidityConfig(BaseModel):
    """Static per-bookmaker cash available for opening a surebet.

    ``default_balance=None`` means bookmakers omitted from the mapping are
    unconstrained. Set it to ``0`` to make the mapping an allow-list of funded
    accounts.
    """

    default_balance: Decimal | None = Field(default=None, ge=0)
    bookmakers: dict[str, Decimal] = Field(default_factory=dict)

    @field_validator("bookmakers")
    @classmethod
    def non_negative_balances(cls, value: dict[str, Decimal]) -> dict[str, Decimal]:
        if any(balance < 0 for balance in value.values()):
            raise ValueError("bookmaker balances must be >= 0")
        return value


class LiquidityBook:
    def __init__(self, config: LiquidityConfig | None = None) -> None:
        self.config = config or LiquidityConfig()
        self._balances = {
            canonical_name(bookmaker): Decimal(balance)
            for bookmaker, balance in self.config.bookmakers.items()
        }

    def available(self, bookmaker: str) -> Decimal | None:
        return self._balances.get(canonical_name(bookmaker), self.config.default_balance)

    def explicit_balances(self) -> dict[str, Decimal]:
        return dict(self.config.bookmakers)

    def after_exposure(
        self,
        exposure_by_canonical_bookmaker: dict[str, Decimal],
        bookmaker_names: Iterable[str],
    ) -> "LiquidityBook":
        adjusted: dict[str, Decimal] = {}
        for bookmaker in bookmaker_names:
            base = self.available(bookmaker)
            if base is None:
                continue
            used = exposure_by_canonical_bookmaker.get(canonical_name(bookmaker), Decimal("0"))
            adjusted[bookmaker] = max(Decimal("0"), base - used)
        return LiquidityBook(
            LiquidityConfig(
                default_balance=self.config.default_balance,
                bookmakers=adjusted,
            )
        )


def load_liquidity_config(path: str | Path | None) -> LiquidityBook:
    if path is None:
        return LiquidityBook()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return LiquidityBook(LiquidityConfig.model_validate(data))
