from __future__ import annotations

import time
from decimal import Decimal
from pathlib import Path

from .costs import CostBook
from .engine import find_arbitrage
from .liquidity import LiquidityBook
from .market_signals import MarketSignalStore, build_market_signals
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
    near_arb_gap: Decimal = Decimal("0.02"),
    max_quote_age_seconds: float = 30.0,
) -> None:
    store = SQLiteStore(db_path)
    health_store = ProviderHealthStore(store.conn)
    signal_store = MarketSignalStore(store.conn)
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
            signal_counts: dict[str, int] = {}
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
                    max_quote_age_seconds=max_quote_age_seconds,
                    cost_book=cost_book,
                    liquidity_book=liquidity_book,
                )
                session.save_opportunities(opportunities)

                fresh_signal_quotes = [
                    quote
                    for quote in quotes
                    if -5.0 <= (session.started_at - quote.observed_at).total_seconds() <= max_quote_age_seconds
                ]
                signals = build_market_signals(
                    fresh_signal_quotes,
                    opportunities,
                    observed_at=session.started_at,
                    near_gap=near_arb_gap,
                )
                signal_store.save(session.scan_id, signals)
                for signal in signals:
                    signal_counts[signal.status] = signal_counts.get(signal.status, 0) + 1

                receipt = session.complete()
            except Exception as exc:
                receipt = session.fail(exc)
                print(
                    f"[SCAN ERROR] id={receipt.scan_id} provider={provider_name} "
                    f"quotes_saved={receipt.quote_count} {receipt.error_type}: "
                    f"{receipt.error_message}"
                )
                raise

            signal_text = (
                f" | signals=net:{signal_counts.get('net_arbitrage', 0)}"
                f" gross:{signal_counts.get('gross_arbitrage', 0)}"
                f" near:{signal_counts.get('near_arbitrage', 0)}"
            )
            if opportunities:
                best = opportunities[0]
                print(
                    f"[ARB] scan={receipt.scan_id} {best.event} | NET ROI {best.net_roi:.4%} | "
                    f"gross {best.gross_roi:.4%} | net profit {best.guaranteed_profit} "
                    f"on {best.capital_used}{coverage_text}{signal_text}"
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
                    f"{receipt.duration_ms:.0f} ms{coverage_text}{signal_text}"
                )

            completed += 1
            if iterations is None or completed < iterations:
                time.sleep(interval_seconds)
    finally:
        store.close()
