from __future__ import annotations

import heapq
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from .costs import CostBook
from .engine import find_arbitrage
from .liquidity import LiquidityBook, LiquidityConfig
from .normalizer import canonical_name
from .storage import SQLiteStore


class BacktestConfig(BaseModel):
    initial_bankroll: Decimal = Field(default=Decimal("5000"), gt=0)
    stake_per_opportunity: Decimal = Field(default=Decimal("500"), gt=0)
    min_net_roi: Decimal = Field(default=Decimal("0.015"), ge=0)
    max_quote_age_seconds: float = Field(default=30.0, gt=0)
    min_signal_persistence_seconds: float = Field(default=0.0, ge=0)
    execution_latency_seconds: float = Field(default=0.0, ge=0)
    settlement_hours: float = Field(default=3.0, ge=0)
    settlement_mode: Literal["guaranteed", "results"] = "guaranteed"
    enforce_bookmaker_liquidity: bool = False
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
    execution_latency_seconds: float
    net_roi: Decimal
    capital_used: Decimal
    guaranteed_profit: Decimal
    guaranteed_return: Decimal
    settlement_return: Decimal
    realized_profit: Decimal
    winning_outcome: str | None = None
    settlement_mode: str = "guaranteed"
    outlay_by_bookmaker: dict[str, Decimal] = field(default_factory=dict)


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
    signals_rejected_for_latency: int = 0
    signals_rejected_for_liquidity: int = 0
    signals_rejected_for_missing_result: int = 0
    turnover_by_bookmaker: dict[str, Decimal] = field(default_factory=dict)
    peak_locked_outlay_by_bookmaker: dict[str, Decimal] = field(default_factory=dict)
    starting_balance_by_bookmaker: dict[str, Decimal] = field(default_factory=dict)
    ending_balance_by_bookmaker: dict[str, Decimal] = field(default_factory=dict)
    balance_change_by_bookmaker: dict[str, Decimal] = field(default_factory=dict)

    @property
    def projected_return_pct(self) -> Decimal:
        return self.projected_profit / self.initial_bankroll if self.initial_bankroll else Decimal("0")


@dataclass(order=True)
class _Position:
    settle_at: datetime
    seq: int
    capital: Decimal = field(compare=False)
    settlement_return: Decimal = field(compare=False)
    outlay_by_bookmaker: dict[str, Decimal] = field(compare=False, default_factory=dict)
    payout_bookmaker: str | None = field(compare=False, default=None)


def run_backtest(
    store: SQLiteStore,
    config: BacktestConfig,
    cost_book: CostBook | None = None,
    liquidity_book: LiquidityBook | None = None,
) -> BacktestResult:
    """Replay stored quote snapshots under conservative execution assumptions.

    `guaranteed` mode preserves the working-capital model: every settled surebet
    credits its minimum guaranteed return.

    `results` mode requires an exact stored settlement for each event+market. Cash
    outlays are removed from the corresponding bookmaker wallets and only the winning
    leg's net return is credited back to the winning bookmaker. Future opportunities
    therefore depend on the actual post-settlement distribution of bankroll.

    Execution latency is modeled by requiring the signal to remain qualifying until
    a scan at or after first_seen + persistence + latency. The opportunity is then
    recomputed from that later scan instead of using the original quote snapshot.
    """
    cost_book = cost_book or CostBook()
    liquidity_book = liquidity_book or LiquidityBook()
    scans = store.list_scans(config.start, config.end)
    cash = config.initial_bankroll
    active: list[_Position] = []
    trades: list[BacktestTrade] = []
    traded_keys: set[str] = set()
    qualifying_since: dict[str, datetime] = {}
    signals_seen = 0
    persistence_rejections = 0
    latency_rejections = 0
    liquidity_rejections = 0
    missing_result_rejections = 0
    seq = 0

    current_exposure: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    peak_exposure: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    turnover: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    bookmaker_display: dict[str, str] = {}

    starting_wallets: dict[str, Decimal] = {}
    wallets: dict[str, Decimal] = {}
    for bookmaker, balance in liquidity_book.explicit_balances().items():
        key = canonical_name(bookmaker)
        bookmaker_display.setdefault(key, bookmaker)
        starting_wallets[key] = Decimal(balance)
        wallets[key] = Decimal(balance)

    def ensure_wallet(bookmaker: str) -> Decimal | None:
        key = canonical_name(bookmaker)
        bookmaker_display.setdefault(key, bookmaker)
        if key in wallets:
            return wallets[key]
        base = liquidity_book.available(bookmaker)
        if base is None:
            return None
        wallets[key] = Decimal(base)
        starting_wallets.setdefault(key, Decimal(base))
        return wallets[key]

    def execution_liquidity_for(bookmaker_names: set[str]) -> LiquidityBook | None:
        if not config.enforce_bookmaker_liquidity:
            return None
        if config.settlement_mode == "results":
            adjusted: dict[str, Decimal] = {}
            for bookmaker in bookmaker_names:
                balance = ensure_wallet(bookmaker)
                if balance is not None:
                    adjusted[bookmaker] = balance
            return LiquidityBook(
                LiquidityConfig(
                    default_balance=liquidity_book.config.default_balance,
                    bookmakers=adjusted,
                )
            )
        return liquidity_book.after_exposure(current_exposure, bookmaker_names)

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
            cash += position.settlement_return
            for book_key, outlay in position.outlay_by_bookmaker.items():
                current_exposure[book_key] = max(
                    Decimal("0"), current_exposure[book_key] - outlay
                )
            if config.settlement_mode == "results" and position.payout_bookmaker is not None:
                wallets[position.payout_bookmaker] = (
                    wallets.get(position.payout_bookmaker, Decimal("0"))
                    + position.settlement_return
                )

        quotes = store.load_quotes_for_scan(int(scan["id"]))
        if not quotes:
            qualifying_since.clear()
            continue

        signals = find_arbitrage(
            quotes,
            bankroll=config.stake_per_opportunity,
            min_net_roi=config.min_net_roi,
            max_quote_age_seconds=config.max_quote_age_seconds,
            now=scan_time,
            cost_book=cost_book,
        )
        signals_seen += len(signals)

        current_keys = {opp.event_market_key for opp in signals}
        for missing_key in set(qualifying_since) - current_keys:
            qualifying_since.pop(missing_key, None)

        for signal in signals:
            key = signal.event_market_key
            first_seen = qualifying_since.setdefault(key, scan_time)
            persistence_seconds = max(0.0, (scan_time - first_seen).total_seconds())

            if persistence_seconds < config.min_signal_persistence_seconds:
                persistence_rejections += 1
                continue
            ready_after = config.min_signal_persistence_seconds + config.execution_latency_seconds
            if persistence_seconds < ready_after:
                latency_rejections += 1
                continue
            if config.one_trade_per_event_market and key in traded_keys:
                continue
            if signal.commence_time <= scan_time or cash < Decimal("0.01"):
                continue

            settlement_result = None
            if config.settlement_mode == "results":
                settlement_result = store.get_settlement_result(
                    signal.event_id, signal.market_signature
                )
                if settlement_result is None:
                    missing_result_rejections += 1
                    continue

            allocation = min(config.stake_per_opportunity, cash)
            bookmaker_names = {quote.bookmaker for quote in quotes}
            execution_liquidity = execution_liquidity_for(bookmaker_names)

            executable = find_arbitrage(
                quotes,
                bankroll=allocation,
                min_net_roi=config.min_net_roi,
                max_quote_age_seconds=config.max_quote_age_seconds,
                now=scan_time,
                cost_book=cost_book,
                liquidity_book=execution_liquidity,
            )
            executable_by_key = {
                candidate.event_market_key: candidate for candidate in executable
            }
            opp = executable_by_key.get(key)
            if opp is None:
                if config.enforce_bookmaker_liquidity:
                    liquidity_rejections += 1
                continue
            if opp.capital_used > cash:
                continue

            winning_leg = None
            if settlement_result is not None:
                result_key = canonical_name(settlement_result.winning_outcome)
                winning_leg = next(
                    (
                        leg
                        for leg in opp.legs
                        if canonical_name(leg.outcome) == result_key
                    ),
                    None,
                )
                if winning_leg is None:
                    missing_result_rejections += 1
                    continue

            leg_outlays: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
            trade_display_outlays: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
            for leg in opp.legs:
                book_key = canonical_name(leg.bookmaker)
                bookmaker_display.setdefault(book_key, leg.bookmaker)
                leg_outlays[book_key] += leg.cash_outlay
                trade_display_outlays[leg.bookmaker] += leg.cash_outlay

            if config.settlement_mode == "results" and config.enforce_bookmaker_liquidity:
                if any(
                    book_key in wallets and wallets[book_key] < outlay
                    for book_key, outlay in leg_outlays.items()
                ):
                    liquidity_rejections += 1
                    continue

            base_settle_at = opp.commence_time + timedelta(hours=config.settlement_hours)
            if settlement_result is not None:
                settle_at = max(base_settle_at, settlement_result.settled_at)
                settlement_return = winning_leg.net_return_if_win
                winning_outcome = settlement_result.winning_outcome
                payout_bookmaker = canonical_name(winning_leg.bookmaker)
            else:
                settle_at = base_settle_at
                settlement_return = opp.capital_used + opp.guaranteed_profit
                winning_outcome = None
                payout_bookmaker = None

            cash -= opp.capital_used
            seq += 1
            heapq.heappush(
                active,
                _Position(
                    settle_at=settle_at,
                    seq=seq,
                    capital=opp.capital_used,
                    settlement_return=settlement_return,
                    outlay_by_bookmaker=dict(leg_outlays),
                    payout_bookmaker=payout_bookmaker,
                ),
            )

            for book_key, outlay in leg_outlays.items():
                turnover[book_key] += outlay
                current_exposure[book_key] += outlay
                peak_exposure[book_key] = max(
                    peak_exposure[book_key], current_exposure[book_key]
                )
                if (
                    config.settlement_mode == "results"
                    and config.enforce_bookmaker_liquidity
                    and book_key in wallets
                ):
                    wallets[book_key] -= outlay

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
                    execution_latency_seconds=config.execution_latency_seconds,
                    net_roi=opp.net_roi,
                    capital_used=opp.capital_used,
                    guaranteed_profit=opp.guaranteed_profit,
                    guaranteed_return=opp.capital_used + opp.guaranteed_profit,
                    settlement_return=settlement_return,
                    realized_profit=settlement_return - opp.capital_used,
                    winning_outcome=winning_outcome,
                    settlement_mode=config.settlement_mode,
                    outlay_by_bookmaker=dict(trade_display_outlays),
                )
            )

    locked_capital = sum((p.capital for p in active), Decimal("0"))
    pending_profit = sum(
        (p.settlement_return - p.capital for p in active), Decimal("0")
    )
    projected_equity = cash + sum((p.settlement_return for p in active), Decimal("0"))
    realized_profit = cash + locked_capital - config.initial_bankroll
    projected_profit = projected_equity - config.initial_bankroll

    def display_map(values: dict[str, Decimal]) -> dict[str, Decimal]:
        return {
            bookmaker_display.get(book_key, book_key): value
            for book_key, value in sorted(values.items())
            if value != 0
        }

    ending_wallets = display_map(wallets)
    starting_wallets_display = display_map(starting_wallets)
    wallet_deltas: dict[str, Decimal] = {}
    for name in set(starting_wallets_display) | set(ending_wallets):
        wallet_deltas[name] = ending_wallets.get(name, Decimal("0")) - starting_wallets_display.get(
            name, Decimal("0")
        )

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
        signals_rejected_for_persistence=persistence_rejections,
        signals_rejected_for_latency=latency_rejections,
        signals_rejected_for_liquidity=liquidity_rejections,
        signals_rejected_for_missing_result=missing_result_rejections,
        turnover_by_bookmaker=display_map(turnover),
        peak_locked_outlay_by_bookmaker=display_map(peak_exposure),
        starting_balance_by_bookmaker=starting_wallets_display,
        ending_balance_by_bookmaker=ending_wallets,
        balance_change_by_bookmaker=wallet_deltas,
    )
