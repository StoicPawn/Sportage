from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arbengine.costs import CostBook, CostConfig
from arbengine.engine import find_arbitrage
from arbengine.models import CostProfile, MarketType, Quote


def quote(
    outcome: str,
    bookmaker: str,
    odds: str,
    *,
    expected_outcomes: int = 2,
    market: MarketType = MarketType.H2H,
) -> Quote:
    now = datetime.now(timezone.utc)
    return Quote(
        event_id="e1",
        sport="tennis",
        commence_time=now + timedelta(hours=2),
        home="A",
        away="B",
        market=market,
        outcome=outcome,
        bookmaker=bookmaker,
        odds=Decimal(odds),
        expected_outcomes=expected_outcomes,
        observed_at=now,
    )


def test_detects_two_way_arbitrage():
    opportunities = find_arbitrage(
        [
            quote("A", "book1", "2.10"),
            quote("A", "book2", "1.95"),
            quote("B", "book1", "1.90"),
            quote("B", "book2", "2.05"),
        ],
        bankroll=Decimal("1000"),
    )
    assert len(opportunities) == 1
    opp = opportunities[0]
    assert opp.gross_implied_sum < 1
    assert opp.net_roi > Decimal("0.03")
    assert opp.capital_used <= Decimal("1000")
    assert opp.guaranteed_profit > Decimal("37")


def test_rejects_non_arbitrage():
    opportunities = find_arbitrage([quote("A", "book1", "1.90"), quote("B", "book2", "1.90")])
    assert opportunities == []


def test_respects_net_roi():
    opportunities = find_arbitrage(
        [quote("A", "book1", "2.10"), quote("B", "book2", "2.05")],
        min_net_roi=Decimal("0.05"),
    )
    assert opportunities == []


def test_rejects_incomplete_three_way_market():
    opportunities = find_arbitrage(
        [
            quote("Home", "book1", "3.00", expected_outcomes=3, market=MarketType.ONE_X_TWO),
            quote("Away", "book2", "3.00", expected_outcomes=3, market=MarketType.ONE_X_TWO),
        ]
    )
    assert opportunities == []


def test_costs_can_remove_gross_arbitrage():
    cost_book = CostBook(
        CostConfig(
            default=CostProfile(bookmaker="*", slippage_bps=Decimal("250")),
        )
    )
    opportunities = find_arbitrage(
        [quote("A", "book1", "2.02"), quote("B", "book2", "2.02")],
        cost_book=cost_book,
    )
    assert opportunities == []


def test_bookmaker_commission_is_reflected_in_net_roi():
    cost_book = CostBook(
        CostConfig(
            bookmakers=[
                CostProfile(bookmaker="book2", commission_on_winnings_pct=Decimal("0.10"))
            ]
        )
    )
    opp = find_arbitrage(
        [quote("A", "book1", "2.10"), quote("B", "book2", "2.05")],
        cost_book=cost_book,
    )[0]
    assert opp.net_roi < opp.gross_roi
    assert opp.estimated_costs > 0


def test_bookmaker_liquidity_resizes_surebet():
    from arbengine.liquidity import LiquidityBook, LiquidityConfig

    liquidity = LiquidityBook(LiquidityConfig(bookmakers={"book1": Decimal("100"), "book2": Decimal("1000")}))
    opp = find_arbitrage(
        [quote("A", "book1", "2.10"), quote("B", "book2", "2.05")],
        bankroll=Decimal("1000"),
        liquidity_book=liquidity,
    )[0]
    book1_outlay = sum(leg.cash_outlay for leg in opp.legs if leg.bookmaker == "book1")
    assert book1_outlay <= Decimal("100")
    assert opp.capital_used < Decimal("300")
    assert opp.unallocated_cash > Decimal("700")
    assert opp.liquidity_limited is True
    assert "book1" in opp.limiting_bookmakers


def test_liquidity_allowlist_can_make_arb_unexecutable():
    from arbengine.liquidity import LiquidityBook, LiquidityConfig

    liquidity = LiquidityBook(
        LiquidityConfig(default_balance=Decimal("0"), bookmakers={"book1": Decimal("100")})
    )
    opportunities = find_arbitrage(
        [quote("A", "book1", "2.10"), quote("B", "book2", "2.05")],
        liquidity_book=liquidity,
    )
    assert opportunities == []
