from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from arbengine.models import Quote


@dataclass(frozen=True)
class SourceHealth:
    source: str
    status: str
    started_at: datetime
    completed_at: datetime
    duration_ms: float
    raw_quote_count: int = 0
    normalized_quote_count: int = 0
    operator_count: int = 0
    error_type: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class OperatorCoverage:
    operator_id: str
    quote_count: int
    event_count: int
    market_count: int
    source_count: int
    freshest_observed_at: datetime | None
    oldest_quote_age_seconds: float


@dataclass
class ProviderFetchReport:
    quotes: list[Quote]
    source_health: list[SourceHealth]
    operator_coverage: list[OperatorCoverage]
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def successful_source_count(self) -> int:
        """Sources freshly fetched successfully during this tick."""
        return sum(1 for item in self.source_health if item.status == "ok")

    @property
    def cached_source_count(self) -> int:
        return sum(
            1
            for item in self.source_health
            if item.status in {"cached", "budget_exhausted", "disabled", "unconfigured"}
            and item.normalized_quote_count > 0
        )

    @property
    def usable_source_count(self) -> int:
        """Fresh or cached sources that currently contribute normalized quotes."""
        return sum(1 for item in self.source_health if item.normalized_quote_count > 0)

    @property
    def failed_source_count(self) -> int:
        return sum(1 for item in self.source_health if item.status == "error")

    @property
    def covered_operator_ids(self) -> set[str]:
        return {item.operator_id for item in self.operator_coverage if item.quote_count > 0}

    @property
    def partial_failure(self) -> bool:
        return self.usable_source_count > 0 and self.failed_source_count > 0
