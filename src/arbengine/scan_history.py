from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter

from .models import ArbitrageOpportunity, Quote
from .storage import SQLiteStore


@dataclass(frozen=True)
class ScanReceipt:
    scan_id: int
    provider: str
    started_at: datetime
    completed_at: datetime
    status: str
    quote_count: int
    opportunity_count: int
    duration_ms: float
    error_type: str | None = None
    error_message: str | None = None


class ScanHistorySession:
    """Durably records one attempted scanner cycle.

    The scan row is committed immediately on start, so even a provider/network
    failure leaves an auditable attempt in SQL. Quotes are committed as soon as
    they are fetched. Repeated identical snapshots are intentionally retained:
    their persistence through time is useful for latency/lifetime backtests.
    """

    def __init__(self, store: SQLiteStore, provider: str) -> None:
        self.store = store
        self.provider = provider
        self.started_at = datetime.now(timezone.utc)
        self._started_perf = perf_counter()
        self.scan_id = self.store.begin_scan(provider, started_at=self.started_at)
        self.quote_count = 0
        self.opportunity_count = 0
        self._closed = False

    def save_quotes(self, quotes: list[Quote]) -> None:
        if self._closed:
            raise RuntimeError("scan session is already closed")
        self.store.save_quotes(quotes, scan_id=self.scan_id)
        self.quote_count += len(quotes)

    def save_opportunities(self, opportunities: list[ArbitrageOpportunity]) -> None:
        if self._closed:
            raise RuntimeError("scan session is already closed")
        self.store.save_opportunities(opportunities, scan_id=self.scan_id)
        self.opportunity_count += len(opportunities)

    def complete(self) -> ScanReceipt:
        return self._finish(status="ok")

    def fail(self, exc: BaseException) -> ScanReceipt:
        return self._finish(
            status="error",
            error_type=type(exc).__name__,
            error_message=str(exc)[:2000],
        )

    def _finish(
        self,
        *,
        status: str,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> ScanReceipt:
        if self._closed:
            row = self.store.get_scan(self.scan_id)
            if row is None:
                raise RuntimeError(f"scan {self.scan_id} disappeared from storage")
            return ScanReceipt(
                scan_id=self.scan_id,
                provider=self.provider,
                started_at=datetime.fromisoformat(row["started_at"]),
                completed_at=datetime.fromisoformat(row["completed_at"]),
                status=row["status"],
                quote_count=int(row["quote_count"]),
                opportunity_count=int(row["opportunity_count"]),
                duration_ms=float(row["duration_ms"] or 0.0),
                error_type=row["error_type"],
                error_message=row["error_message"],
            )

        completed_at = datetime.now(timezone.utc)
        duration_ms = (perf_counter() - self._started_perf) * 1000.0
        self.store.finish_scan(
            self.scan_id,
            self.quote_count,
            self.opportunity_count,
            status=status,
            completed_at=completed_at,
            duration_ms=duration_ms,
            error_type=error_type,
            error_message=error_message,
        )
        self._closed = True
        return ScanReceipt(
            scan_id=self.scan_id,
            provider=self.provider,
            started_at=self.started_at,
            completed_at=completed_at,
            status=status,
            quote_count=self.quote_count,
            opportunity_count=self.opportunity_count,
            duration_ms=duration_ms,
            error_type=error_type,
            error_message=error_message,
        )


class ScanHistory:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def start(self, provider: str) -> ScanHistorySession:
        return ScanHistorySession(self.store, provider)
