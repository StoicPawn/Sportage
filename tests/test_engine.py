from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arbengine.costs import CostBook, CostConfig
from arbengine.engine import find_arbitrage
from arbengine.models import CostProfile, MarketType, Quote


def quote(outcome: str, bookmaker: str, odds: str, *, expected_outcomes: int = 2, market: MarketType = MarketType.H2H) -> Quote:
    now = datetime.now(timezone.utc)
    return Quote(event_id="e1", sport="tennis", commence_time=now + timedelta(hours=2), home="A", away="B", market=market, outcome=outcome, bookmaker=bookmaker, odds=Decimal(odds), expected_outcomes=expected_outcomes, observed_at=now)


def test_detects_two_way_arbitrage():
    opportunities = find_arbitrage([
        quote("A", "book1", "2.10"), quote("A", "book2", "1.95"),
        quote("B", "book1", "1.90"), quote("B", "book2", "2.05"),
    ], bankroll=Decimal("1000"))
    assert len(opportunities) == 1
    opp = opportunities[0]
    assert opp.gross_implied_sum < 1
    assert opp.net_roi > Decimal("0.03")
    assert opp.capital_used <= Decimal("1000")
    assert opp.guaranteed_profit > Decimal("37")


def test_rejects_non_arbitrage():
    assert find_arbitrage([quote("A", "book1", "1.90"), quote("B", "book2", "1.90")]) == []


def test_respects_net_roi():
    assert find_arbitrage([quote("A", "book1", "2.10"), quote("B", "book2", "2.05")], min_net_roi=Decimal("0.05")) == []


def test_rejects_incomplete_three_way_market():
    assert find_arbitrage([
        quote("Home", "book1", "3.00", expected_outcomes=3, market=MarketType.ONE_X_TWO),
        quote("Away", "book2", "3.00", expected_outcomes=3, market=MarketType.ONE_X_TWO),
    ]) == []


def test_costs_can_remove_gross_arbitrage():
    cost_book = CostBook(CostConfig(default=CostProfile(bookmaker="*", slippage_bps=Decimal("250"))))
    assert find_arbitrage([quote("A", "book1", "2.02"), quote("B", "book2", "2.02")], cost_book=cost_book) == []


def test_bookmaker_commission_is_reflected_in_net_roi():
    cost_book = CostBook(CostConfig(bookmakers=[CostProfile(bookmaker="book2", commission_on_winnings_pct=Decimal("0.10"))]))
    opp = find_arbitrage([quote("A", "book1", "2.10"), quote("B", "book2", "2.05")], cost_book=cost_book)[0]
    assert opp.net_roi < opp.gross_roi
    assert opp.estimated_costs > 0
