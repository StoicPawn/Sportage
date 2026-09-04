from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from itertools import product
from typing import Iterable

from .costs import CostBook, effective_odds, net_return_factor
from .models import ArbitrageOpportunity, Leg, Quote

CENT = Decimal("0.01")


def _qcent(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_DOWN)


def _evaluate_combination(selected: dict[str, Quote], bankroll: Decimal, cost_book: CostBook, now: datetime) -> ArbitrageOpportunity | None:
    profiles = {outcome: cost_book.for_bookmaker(q.bookmaker) for outcome, q in selected.items()}
    factors = {outcome: net_return_factor(q.odds, profiles[outcome]) for outcome, q in selected.items()}
    fixed_total = sum((p.fixed_cost_per_bet for p in profiles.values()), Decimal("0"))
    if fixed_total >= bankroll:
        return None

    denom = sum((((Decimal("1") + profiles[o].stake_fee_pct) / factors[o]) for o in selected), Decimal("0"))
    target_return = (bankroll - fixed_total) / denom

    max_return = target_return
    for outcome, profile in profiles.items():
        if profile.max_stake is not None:
            max_return = min(max_return, profile.max_stake * factors[outcome])

    stakes = {outcome: _qcent(max_return / factors[outcome]) for outcome in selected}
    if any(stakes[o] < profiles[o].min_stake for o in stakes):
        return None

    legs: list[Leg] = []
    net_returns: dict[str, Decimal] = {}
    gross_payouts: dict[str, Decimal] = {}
    cash_used = Decimal("0")

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
        legs.append(Leg(
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
        ))

    if cash_used <= 0 or cash_used > bankroll:
        return None

    guaranteed_return = min(net_returns.values())
    guaranteed_profit = guaranteed_return - cash_used
    net_roi = guaranteed_profit / cash_used
    gross_implied_sum = sum((Decimal("1") / q.odds for q in selected.values()), Decimal("0"))
    gross_roi = Decimal("1") / gross_implied_sum - Decimal("1")
    gross_payout = min(gross_payouts.values())
    gross_profit = gross_payout - sum(stakes.values(), Decimal("0"))
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
        legs=legs,
    )


def find_arbitrage(
    quotes: Iterable[Quote],
    bankroll: Decimal = Decimal("1000"),
    min_net_roi: Decimal | None = None,
    max_quote_age_seconds: float = 30.0,
    now: datetime | None = None,
    cost_book: CostBook | None = None,
    min_roi: Decimal | None = None,
) -> list[ArbitrageOpportunity]:
    """Find complete cross-bookmaker surebets and filter on NET ROI after costs."""
    now = now or datetime.now(timezone.utc)
    if min_net_roi is None:
        min_net_roi = min_roi if min_roi is not None else Decimal("0")
    cost_book = cost_book or CostBook()
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
        best_net: ArbitrageOpportunity | None = None
        for combo in product(*(candidates[outcome] for outcome in outcomes)):
            selected = dict(zip(outcomes, combo, strict=True))
            opportunity = _evaluate_combination(selected, bankroll, cost_book, now)
            if opportunity is None or opportunity.net_roi < min_net_roi:
                continue
            if best_net is None or opportunity.guaranteed_profit > best_net.guaranteed_profit:
                best_net = opportunity

        if best_net is not None:
            opportunities.append(best_net)

    return sorted(opportunities, key=lambda x: (x.net_roi, x.guaranteed_profit), reverse=True)
