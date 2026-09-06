from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .backtest import BacktestConfig, BacktestResult, run_backtest
from .costs import CostBook
from .liquidity import LiquidityBook
from .provider_health_storage import ProviderHealthStore
from .storage import SQLiteStore


@dataclass(frozen=True)
class CoverageFilterStats:
    total_scans: int
    eligible_scans: int
    rejected_scans: int
    min_covered_operators: int


class CoverageFilteredStore:
    """Read-through SQLiteStore view that filters scans by measured operator coverage."""

    def __init__(self, store: SQLiteStore, min_covered_operators: int) -> None:
        if min_covered_operators < 0:
            raise ValueError("min_covered_operators must be >= 0")
        self.store = store
        self.min_covered_operators = min_covered_operators
        self.health = ProviderHealthStore(store.conn)
        self.stats = CoverageFilterStats(0, 0, 0, min_covered_operators)

    def list_scans(self, start: datetime | None = None, end: datetime | None = None):
        scans = self.store.list_scans(start, end)
        if self.min_covered_operators == 0:
            self.stats = CoverageFilterStats(len(scans), len(scans), 0, 0)
            return scans

        eligible = []
        for scan in scans:
            coverage = self.health.operator_coverage_for_scan(int(scan["id"]))
            if len(coverage) >= self.min_covered_operators:
                eligible.append(scan)
        self.stats = CoverageFilterStats(
            total_scans=len(scans),
            eligible_scans=len(eligible),
            rejected_scans=len(scans) - len(eligible),
            min_covered_operators=self.min_covered_operators,
        )
        return eligible

    def __getattr__(self, name: str):
        return getattr(self.store, name)


def run_coverage_aware_backtest(
    store: SQLiteStore,
    config: BacktestConfig,
    *,
    min_covered_operators: int = 0,
    cost_book: CostBook | None = None,
    liquidity_book: LiquidityBook | None = None,
) -> tuple[BacktestResult, CoverageFilterStats]:
    """Run the existing backtest only on scans with sufficient measured coverage.

    Scans without provider-health coverage are rejected when the threshold is > 0.
    This prevents low-coverage periods from being silently interpreted as genuine
    no-arbitrage periods in profitability estimates.
    """
    filtered = CoverageFilteredStore(store, min_covered_operators)
    result = run_backtest(
        filtered,  # type: ignore[arg-type]
        config,
        cost_book=cost_book,
        liquidity_book=liquidity_book,
    )
    return result, filtered.stats
