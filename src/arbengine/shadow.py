from __future__ import annotations

import time
from decimal import Decimal
from pathlib import Path

from .costs import CostBook
from .engine import find_arbitrage
from .liquidity import LiquidityBook
from .operators import OPERATORS
from .provider_health_storage import ProviderHealthStore
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
    health_store = ProviderHealthStore(store.conn)
    history = ScanHistory(store)
    completed = 0
    provider_name = provider.__class__.__name__

    attach_budget = getattr(provider, "attach_budget_connection", None)
    if callable(attach_budget):
        attach_budget(store.conn)

    try:
        while iterations is None or completed < iterations:
            session = history.start(provider_name)
            opportunities = []
            coverage_text = ""
            try:
                fetch_report = getattr(provider, "fetch_report", None)
                if callable(fetch_report):
                    report = fetch_report()
                    health_store.save_report(session.scan_id, report)
                    if report.source_health and all(item.status == "error" for item in report.source_health):
                        details = "; ".join(
                            f"{item.source}: {item.error_type or 'error'} {item.error_message or ''}".strip()
                            for item in report.source_health
                        )
                        raise RuntimeError(f"All configured market-data sources failed. {details}")
                    quotes = report.quotes
                    coverage_text = (
                        f" | coverage={len(report.covered_operator_ids)}/{len(OPERATORS)}"
                        f" | fresh={report.successful_source_count}"
                        f" cached={report.cached_source_count}"
                        f" failed={report.failed_source_count}"
                    )
                    snapshot = getattr(provider, "last_snapshot", None)
                    if snapshot is not None:
                        gap = "n/a" if snapshot.best_implied_gap is None else f"{snapshot.best_implied_gap:.3%}"
                        coverage_text += f" | mode={snapshot.mode} near-gap={gap}"
                else:
                    quotes = provider.fetch_quotes()

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
                    f"on {best.capital_used}{coverage_text}"
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
                    f"{receipt.duration_ms:.0f} ms{coverage_text}"
                )

            completed += 1
            if iterations is None or completed < iterations:
                time.sleep(interval_seconds)
    finally:
        store.close()
