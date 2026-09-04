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
    min_signal_persistence_seconds: float = Field(default=0.0, ge=0)
    settlement_hours: float = Field(default=3.0, ge=0)
    start: datetime | None = None
    end: datetime | None = None
    one_trade_per_event_market: bool = True


@dataclass
class BacktestTrade:
    first_seen_at: datetime
    detected_at: datetime
    settle_at: datetime
    event: str
    event_market_key: str
    market: str
    persistence_seconds: float
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
    signals_rejected_for_persistence: int = 0

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
    """Replay stored scans with conservative execution constraints.

    If ``min_signal_persistence_seconds`` is positive, an event+market has to remain a
    qualifying net arbitrage across successive scans for at least that long. A scan
    where the signal disappears resets the persistence clock. This deliberately
    rejects one-snapshot opportunities that may be impossible to execute in practice.
    """
    cost_book = cost_book or CostBook()
    scans = store.list_scans(config.start, config.end)
    cash = config.initial_bankroll
    active: list[_Position] = []
    trades: list[BacktestTrade] = []
    traded_keys: set[str] = set()
    qualifying_since: dict[str, datetime] = {}
    signals_seen = 0
    signals_rejected_for_persistence = 0
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
            position = heapq.heappop(active)
            cash += position.guaranteed_return

        quotes = store.load_quotes_for_scan(int(scan["id"]))
        if not quotes:
            qualifying_since.clear()
            continue

        # Detection is independent of current free cash. We evaluate at the configured
        # per-opportunity size so signal continuity does not disappear merely because
        # bankroll is temporarily locked in another position.
        opportunities = find_arbitrage(
            quotes,
            bankroll=config.stake_per_opportunity,
            min_net_roi=config.min_net_roi,
            max_quote_age_seconds=config.max_quote_age_seconds,
            now=scan_time,
            cost_book=cost_book,
        )
        signals_seen += len(opportunities)

        current_keys = {opp.event_market_key for opp in opportunities}
        for missing_key in set(qualifying_since) - current_keys:
            qualifying_since.pop(missing_key, None)

        for opp in opportunities:
            key = opp.event_market_key
            first_seen = qualifying_since.setdefault(key, scan_time)
            persistence_seconds = max(0.0, (scan_time - first_seen).total_seconds())

            if persistence_seconds < config.min_signal_persistence_seconds:
                signals_rejected_for_persistence += 1
                continue
            if config.one_trade_per_event_market and key in traded_keys:
                continue
            if opp.commence_time <= scan_time:
                continue
            if cash < Decimal("0.01"):
                continue

            allocation = min(config.stake_per_opportunity, cash)
            if allocation != config.stake_per_opportunity:
                # Recompute stakes/profit for the capital actually available at execution.
                resized = find_arbitrage(
                    quotes,
                    bankroll=allocation,
                    min_net_roi=config.min_net_roi,
                    max_quote_age_seconds=config.max_quote_age_seconds,
                    now=scan_time,
                    cost_book=cost_book,
                )
                resized_by_key = {candidate.event_market_key: candidate for candidate in resized}
                opp = resized_by_key.get(key)
                if opp is None:
                    continue

            if opp.capital_used > cash:
                continue

            settle_at = opp.commence_time + timedelta(hours=config.settlement_hours)
            guaranteed_return = opp.capital_used + opp.guaranteed_profit
            cash -= opp.capital_used
            seq += 1
            heapq.heappush(
                active,
                _Position(
                    settle_at=settle_at,
                    seq=seq,
                    capital=opp.capital_used,
                    guaranteed_return=guaranteed_return,
                ),
            )
            traded_keys.add(key)
            trades.append(
                BacktestTrade(
                    first_seen_at=first_seen,
                    detected_at=scan_time,
                    settle_at=settle_at,
                    event=opp.event,
                    event_market_key=key,
                    market=opp.market_signature,
                    persistence_seconds=persistence_seconds,
                    net_roi=opp.net_roi,
                    capital_used=opp.capital_used,
                    guaranteed_profit=opp.guaranteed_profit,
                    guaranteed_return=guaranteed_return,
                )
            )

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
        signals_rejected_for_persistence=signals_rejected_for_persistence,
    )
