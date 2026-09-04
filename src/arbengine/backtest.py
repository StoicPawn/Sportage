from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from pydantic import BaseModel, Field

from .costs import CostBook
from .engine import find_arbitrage
from .storage import SQLiteStore


class BacktestConfig(BaseModel):
    initial_bankroll: Decimal = Field(default=Decimal("5000"), gt=0)
    stake_per_opportunity: Decimal = Field(default=Decimal("500"), gt=0)
    min_net_roi: Decimal = Field(default=Decimal("0.015"), ge=0)
    max_quote_age_seconds: float = Field(default=30.0, gt=0)
    settlement_hours: float = Field(default=3.0, ge=0)
    start: datetime | None = None
    end: datetime | None = None
    one_trade_per_event_market: bool = True


@dataclass
class BacktestTrade:
    detected_at: datetime
    settle_at: datetime
    event: str
    event_market_key: str
    market: str
    net_roi: Decimal
    capital_used: Decimal
    guaranteed_profit: Decimal
    guaranteed_return: Decimal


@dataclass
class BacktestResult:
    start: datetime | None
    end: datetime | None
    initial_bankroll: Decimal
    ending_cash: Decimal
    locked_capital: Decimal
    pending_guaranteed_profit: Decimal
    projected_equity: Decimal
    realized_profit: Decimal
    projected_profit: Decimal
    trades: list[BacktestTrade] = field(default_factory=list)
    scans: int = 0
    signals_seen: int = 0

    @property
    def projected_return_pct(self) -> Decimal:
        return self.projected_profit / self.initial_bankroll if self.initial_bankroll else Decimal("0")


@dataclass(order=True)
class _Position:
    settle_at: datetime
    seq: int
    capital: Decimal = field(compare=False)
    guaranteed_return: Decimal = field(compare=False)


def run_backtest(store: SQLiteStore, config: BacktestConfig, cost_book: CostBook | None = None) -> BacktestResult:
    cost_book = cost_book or CostBook()
    scans = store.list_scans(config.start, config.end)
    cash = config.initial_bankroll
    active: list[_Position] = []
    trades: list[BacktestTrade] = []
    traded_keys: set[str] = set()
    signals_seen = 0
    seq = 0
    first_time: datetime | None = None
    last_time: datetime | None = None

    for scan in scans:
        scan_time = datetime.fromisoformat(scan["started_at"])
        if scan_time.tzinfo is None:
            scan_time = scan_time.replace(tzinfo=timezone.utc)
        first_time = first_time or scan_time
        last_time = scan_time

        while active and active[0].settle_at <= scan_time:
            cash += heapq.heappop(active).guaranteed_return

        quotes = store.load_quotes_for_scan(int(scan["id"]))
        if not quotes or cash < Decimal("0.01"):
            continue

        allocation = min(config.stake_per_opportunity, cash)
        opportunities = find_arbitrage(
            quotes,
            bankroll=allocation,
            min_net_roi=config.min_net_roi,
            max_quote_age_seconds=config.max_quote_age_seconds,
            now=scan_time,
            cost_book=cost_book,
        )
        signals_seen += len(opportunities)

        for opp in opportunities:
            key = opp.event_market_key
            if config.one_trade_per_event_market and key in traded_keys:
                continue
            if opp.commence_time <= scan_time or opp.capital_used > cash:
                continue

            settle_at = opp.commence_time + timedelta(hours=config.settlement_hours)
            guaranteed_return = opp.capital_used + opp.guaranteed_profit
            cash -= opp.capital_used
            seq += 1
            heapq.heappush(active, _Position(settle_at, seq, opp.capital_used, guaranteed_return))
            traded_keys.add(key)
            trades.append(BacktestTrade(
                detected_at=scan_time,
                settle_at=settle_at,
                event=opp.event,
                event_market_key=key,
                market=opp.market_signature,
                net_roi=opp.net_roi,
                capital_used=opp.capital_used,
                guaranteed_profit=opp.guaranteed_profit,
                guaranteed_return=guaranteed_return,
            ))
            if cash < Decimal("0.01"):
                break

    locked_capital = sum((p.capital for p in active), Decimal("0"))
    pending_profit = sum((p.guaranteed_return - p.capital for p in active), Decimal("0"))
    projected_equity = cash + sum((p.guaranteed_return for p in active), Decimal("0"))
    realized_profit = cash + locked_capital - config.initial_bankroll
    projected_profit = projected_equity - config.initial_bankroll

    return BacktestResult(
        start=first_time or config.start,
        end=last_time or config.end,
        initial_bankroll=config.initial_bankroll,
        ending_cash=cash,
        locked_capital=locked_capital,
        pending_guaranteed_profit=pending_profit,
        projected_equity=projected_equity,
        realized_profit=realized_profit,
        projected_profit=projected_profit,
        trades=trades,
        scans=len(scans),
        signals_seen=signals_seen,
    )
