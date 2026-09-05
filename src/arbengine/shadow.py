from __future__ import annotations

import time
from decimal import Decimal
from pathlib import Path

from .costs import CostBook
from .engine import find_arbitrage
from .liquidity import LiquidityBook
from .providers.base import OddsProvider
from .scan_history import ScanHistory
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
    history = ScanHistory(store)
    completed = 0
    provider_name = provider.__class__.__name__
    try:
        while iterations is None or completed < iterations:
            session = history.start(provider_name)
            opportunities = []
            try:
                quotes = provider.fetch_quotes()
                # Persist the normalized snapshot immediately. If later arbitrage
                # processing fails, the quote history is still retained for replay.
                session.save_quotes(quotes)
                opportunities = find_arbitrage(
                    quotes,
                    bankroll=bankroll,
                    min_net_roi=min_net_roi,
                    cost_book=cost_book,
                    liquidity_book=liquidity_book,
                )
                session.save_opportunities(opportunities)
                receipt = session.complete()
            except Exception as exc:
                receipt = session.fail(exc)
                print(
                    f"[SCAN ERROR] id={receipt.scan_id} provider={provider_name} "
                    f"quotes_saved={receipt.quote_count} {receipt.error_type}: "
                    f"{receipt.error_message}"
                )
                raise

            if opportunities:
                best = opportunities[0]
                print(
                    f"[ARB] scan={receipt.scan_id} {best.event} | NET ROI {best.net_roi:.4%} | "
                    f"gross {best.gross_roi:.4%} | net profit {best.guaranteed_profit} "
                    f"on {best.capital_used}"
                    + (
                        f" | liquidity-limited: {', '.join(best.limiting_bookmakers)}"
                        if best.liquidity_limited
                        else ""
                    )
                )
            else:
                print(
                    f"[SCAN] id={receipt.scan_id} {receipt.quote_count} quotes | "
                    f"no net opportunity >= {min_net_roi:.2%} | "
                    f"{receipt.duration_ms:.0f} ms"
                )

            completed += 1
            if iterations is None or completed < iterations:
                time.sleep(interval_seconds)
    finally:
        store.close()
