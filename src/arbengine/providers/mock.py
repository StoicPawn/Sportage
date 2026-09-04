from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arbengine.models import MarketType, Quote
from arbengine.providers.base import OddsProvider


class MockProvider(OddsProvider):
    def fetch_quotes(self) -> list[Quote]:
        now = datetime.now(timezone.utc)
        tennis = dict(
            event_id="demo-tennis-001",
            sport="tennis",
            commence_time=now + timedelta(hours=8),
            home="Player A",
            away="Player B",
            market=MarketType.H2H,
            expected_outcomes=2,
            observed_at=now,
            source="mock",
        )
        soccer = dict(
            event_id="demo-soccer-001",
            sport="soccer",
            commence_time=now + timedelta(hours=10),
            home="Home FC",
            away="Away FC",
            market=MarketType.ONE_X_TWO,
            expected_outcomes=3,
            observed_at=now,
            source="mock",
        )
        return [
            Quote(**tennis, outcome="Player A", bookmaker="Book A", odds=Decimal("2.10")),
            Quote(**tennis, outcome="Player A", bookmaker="Book B", odds=Decimal("1.92")),
            Quote(**tennis, outcome="Player B", bookmaker="Book A", odds=Decimal("1.85")),
            Quote(**tennis, outcome="Player B", bookmaker="Book B", odds=Decimal("2.05")),
            Quote(**soccer, outcome="Home", bookmaker="Book A", odds=Decimal("3.60")),
            Quote(**soccer, outcome="Draw", bookmaker="Book B", odds=Decimal("3.75")),
            Quote(**soccer, outcome="Away", bookmaker="Book C", odds=Decimal("3.55")),
        ]
