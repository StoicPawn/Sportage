from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from itertools import product
from typing import Iterable

from .costs import CostBook, effective_odds, net_return_factor
from .liquidity import LiquidityBook
from .normalizer import canonical_name
from .models import ArbitrageOpportunity, Leg, Quote


CENT = Decimal("0.01")


def _qcent(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_DOWN)


def _evaluate_combination(
    selected: dict[str, Quote],
    bankroll: Decimal,
    cost_book: CostBook,
    now: datetime,
    liquidity_book: LiquidityBook,
) -> ArbitrageOpportunity | None:
    profiles = {outcome: cost_book.for_bookmaker(q.bookmaker) for outcome, q in selected.items()}
    factors = {
        outcome: net_return_factor(q.odds, profiles[outcome]) for outcome, q in selected.items()
    }

    fixed_total = sum((p.fixed_cost_per_bet for p in profiles.values()), Decimal("0"))
    if fixed_total >= bankroll:
        return None

    # Equalise net return across every outcome. Placement costs are part of the
    # cash budget, so the denominator includes per-stake fees.
    denom = sum(
        (
            (Decimal("1") + profiles[outcome].stake_fee_pct) / factors[outcome]
            for outcome in selected
        ),
        Decimal("0"),
    )
    target_return = (bankroll - fixed_total) / denom

    # Respect bookmaker max stakes by scaling the whole surebet down, keeping
    # outcome returns equal. Unused cash remains unexposed.
    max_return = target_return
    limiting_bookmakers: set[str] = set()
    for outcome, profile in profiles.items():
        if profile.max_stake is not None:
            stake_cap_return = profile.max_stake * factors[outcome]
            if stake_cap_return < max_return:
                max_return = stake_cap_return

    # A global bankroll is not enough in practice: cash must already sit at the
    # bookmaker used by each leg. Because every stake is proportional to the
    # common guaranteed return R, aggregate bookmaker cash outlay is linear in R.
    # This lets us resize the whole surebet exactly while preserving equal returns.
    outcomes_by_bookmaker: dict[str, list[str]] = defaultdict(list)
    display_name_by_key: dict[str, str] = {}
    for outcome, quote in selected.items():
        book_key = canonical_name(quote.bookmaker)
        outcomes_by_bookmaker[book_key].append(outcome)
        display_name_by_key.setdefault(book_key, quote.bookmaker)

    for book_key, book_outcomes in outcomes_by_bookmaker.items():
        display_name = display_name_by_key[book_key]
        balance = liquidity_book.available(display_name)
        if balance is None:
            continue
        fixed_for_book = sum(
            (profiles[outcome].fixed_cost_per_bet for outcome in book_outcomes), Decimal("0")
        )
        if balance <= fixed_for_book:
            return None
        variable_coeff = sum(
            (
                (Decimal("1") + profiles[outcome].stake_fee_pct) / factors[outcome]
                for outcome in book_outcomes
            ),
            Decimal("0"),
        )
        liquidity_cap_return = (balance - fixed_for_book) / variable_coeff
        if liquidity_cap_return < max_return:
            max_return = liquidity_cap_return
            limiting_bookmakers = {display_name}
        elif liquidity_cap_return == max_return:
            limiting_bookmakers.add(display_name)

    raw_stakes = {outcome: max_return / factors[outcome] for outcome in selected}
    stakes = {outcome: _qcent(stake) for outcome, stake in raw_stakes.items()}
    if any(stakes[o] < profiles[o].min_stake for o in stakes):
        return None

    legs: list[Leg] = []
    net_returns: dict[str, Decimal] = {}
    cash_used = Decimal("0")
    gross_payouts: dict[str, Decimal] = {}

    for outcome, quote in sorted(selected.items()):
        profile = profiles[outcome]
        stake = stakes[outcome]
        adjusted_odds = effective_odds(quote.odds, profile)
        placement_cost = stake * profile.stake_fee_pct + profile.fixed_cost_per_bet
        cash_outlay = stake + placement_cost
        gross_payout = stake * quote.odds
        adjusted_winnings = stake * (adjusted_odds - Decimal("1"))
        win_commission = adjusted_winnings * profile.commission_on_winnings_pct
        net_return = stake * adjusted_odds - win_commission

        cash_used += cash_outlay
        gross_payouts[outcome] = gross_payout
        net_returns[outcome] = net_return
        legs.append(
            Leg(
                outcome=outcome,
                bookmaker=quote.bookmaker,
                odds=quote.odds,
                effective_odds=adjusted_odds,
                stake=stake,
                cash_outlay=_qcent(cash_outlay),
                net_return_if_win=_qcent(net_return),
                quote_age_seconds=max(0.0, (now - quote.observed_at).total_seconds()),
                estimated_placement_cost=_qcent(placement_cost),
                estimated_win_commission=_qcent(win_commission),
                deep_link=quote.deep_link,
            )
        )

    if cash_used <= 0 or cash_used > bankroll:
        return None

    # Rounding should only reduce outlay, nevertheless enforce the aggregate
    # per-book constraint explicitly as a final safety check.
    outlay_by_bookmaker: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for leg in legs:
        outlay_by_bookmaker[canonical_name(leg.bookmaker)] += leg.cash_outlay
    for book_key, outlay in outlay_by_bookmaker.items():
        display_name = display_name_by_key[book_key]
        balance = liquidity_book.available(display_name)
        if balance is not None and outlay > balance:
            return None

    guaranteed_return = min(net_returns.values())
    guaranteed_profit = guaranteed_return - cash_used
    net_roi = guaranteed_profit / cash_used

    gross_implied_sum = sum((Decimal("1") / q.odds for q in selected.values()), Decimal("0"))
    gross_roi = Decimal("1") / gross_implied_sum - Decimal("1")
    gross_guaranteed_payout = min(gross_payouts.values())
    gross_profit = gross_guaranteed_payout - sum(stakes.values(), Decimal("0"))
    estimated_costs = gross_profit - guaranteed_profit

    first = next(iter(selected.values()))
    return ArbitrageOpportunity(
        event_id=first.event_id,
        sport=first.sport,
        event=f"{first.home} vs {first.away}",
        commence_time=first.commence_time,
        market=first.market,
        period=first.period,
        market_line=first.market_line,
        gross_implied_sum=gross_implied_sum,
        gross_roi=gross_roi,
        net_roi=net_roi,
        capital_available=bankroll,
        capital_used=_qcent(cash_used),
        unallocated_cash=_qcent(bankroll - cash_used),
        guaranteed_payout=_qcent(guaranteed_return),
        gross_guaranteed_profit=_qcent(gross_profit),
        guaranteed_profit=_qcent(guaranteed_profit),
        estimated_costs=_qcent(max(Decimal("0"), estimated_costs)),
        liquidity_limited=bool(limiting_bookmakers),
        limiting_bookmakers=sorted(limiting_bookmakers),
        legs=legs,
    )


def find_arbitrage(
    quotes: Iterable[Quote],
    bankroll: Decimal = Decimal("1000"),
    min_net_roi: Decimal | None = None,
    max_quote_age_seconds: float = 30.0,
    now: datetime | None = None,
    cost_book: CostBook | None = None,
    liquidity_book: LiquidityBook | None = None,
    min_roi: Decimal | None = None,
) -> list[ArbitrageOpportunity]:
    """Find cross-bookmaker surebets using *net* return after configured costs.

    The scanner is strict about market completeness. A group is considered only
    when the number of distinct outcomes equals the provider-declared
    `expected_outcomes`, preventing false positives from incomplete 1X2 markets.
    """
    now = now or datetime.now(timezone.utc)
    if min_net_roi is None:
        min_net_roi = min_roi if min_roi is not None else Decimal("0")
    cost_book = cost_book or CostBook()
    liquidity_book = liquidity_book or LiquidityBook()

    groups: dict[tuple[str, str], list[Quote]] = defaultdict(list)
    for quote in quotes:
        age = (now - quote.observed_at).total_seconds()
        if age < -5 or age > max_quote_age_seconds:
            continue
        groups[(quote.event_id, quote.market_signature)].append(quote)

    opportunities: list[ArbitrageOpportunity] = []

    for group in groups.values():
        expected = max(q.expected_outcomes for q in group)
        outcomes = sorted({q.outcome for q in group})
        if len(outcomes) != expected:
            continue

        candidates = {outcome: [q for q in group if q.outcome == outcome] for outcome in outcomes}
        if any(not items for items in candidates.values()):
            continue

        best_net: ArbitrageOpportunity | None = None
        # Enumerating combinations gives the correct choice when commission,
        # fixed fees or stake limits make the highest raw odd suboptimal.
        for combo in product(*(candidates[outcome] for outcome in outcomes)):
            selected = dict(zip(outcomes, combo, strict=True))
            opportunity = _evaluate_combination(selected, bankroll, cost_book, now, liquidity_book)
            if opportunity is None or opportunity.net_roi < min_net_roi:
                continue
            if best_net is None or opportunity.guaranteed_profit > best_net.guaranteed_profit:
                best_net = opportunity

        if best_net is not None:
            opportunities.append(best_net)

    return sorted(opportunities, key=lambda x: (x.net_roi, x.guaranteed_profit), reverse=True)
