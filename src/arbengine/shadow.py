from __future__ import annotations

import time
from decimal import Decimal
from pathlib import Path

from .costs import CostBook
from .engine import find_arbitrage
from .liquidity import LiquidityBook
from .providers.base import OddsProvider
from .storage import SQLiteStore


def run_shadow_loop(
    provider: OddsProvider,
    db_path: str | Path,
    bankroll: Decimal,
    min_net_roi: Decimal,
    cost_book: CostBook | None = None,
    liquidity_book: LiquidityBook | None = None,
    interval_seconds: float = 30.0,
    iterations: int | None = None,
) -> None:
    store = SQLiteStore(db_path)
    completed = 0
    provider_name = provider.__class__.__name__
    try:
        while iterations is None or completed < iterations:
            scan_id = store.begin_scan(provider_name)
            try:
                quotes = provider.fetch_quotes()
                store.save_quotes(quotes, scan_id=scan_id)
                opportunities = find_arbitrage(
                    quotes,
                    bankroll=bankroll,
                    min_net_roi=min_net_roi,
                    cost_book=cost_book,
                    liquidity_book=liquidity_book,
                )
                store.save_opportunities(opportunities, scan_id=scan_id)
                store.finish_scan(scan_id, len(quotes), len(opportunities))
            except Exception:
                store.finish_scan(scan_id, 0, 0, status="error")
                raise

            if opportunities:
                best = opportunities[0]
                print(
                    f"[ARB] {best.event} | NET ROI {best.net_roi:.4%} | "
                    f"gross {best.gross_roi:.4%} | net profit {best.guaranteed_profit} "
                    f"on {best.capital_used}"
                    + (f" | liquidity-limited: {', '.join(best.limiting_bookmakers)}" if best.liquidity_limited else "")
                )
            else:
                print(f"[SCAN] {len(quotes)} quotes | no net opportunity >= {min_net_roi:.2%}")

            completed += 1
            if iterations is None or completed < iterations:
                time.sleep(interval_seconds)
    finally:
        store.close()
