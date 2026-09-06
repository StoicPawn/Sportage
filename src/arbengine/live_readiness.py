from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Iterable

from .connectors.execution import build_execution_connector
from .models import ArbitrageOpportunity, Quote
from .venue_certification import VenueCertificationStore, execution_environment


class LiveReadinessError(ValueError):
    pass


def _automatic(operator_id: str) -> bool:
    connector = build_execution_connector(operator_id)
    return bool(getattr(connector, "automatic_execution", False))


def assert_live_readiness(
    opportunity: ArbitrageOpportunity,
    quotes: Iterable[Quote],
    certifications: VenueCertificationStore,
    *,
    max_quote_age_seconds: float = 10.0,
) -> dict[str, list[str]]:
    """Require certified primary automatic venues and an independent rescue venue.

    For every automatic venue used by the opportunity, at least one *different*
    certified automatic venue must currently expose fresh native execution references
    for every outcome in the same event+market. This is intentionally conservative:
    a live plan is armed only when Sportage can route around the failure of any one
    automatic venue before exposure is opened.
    """

    planned_automatic = {
        leg.operator_id
        for leg in opportunity.legs
        if leg.operator_id and _automatic(leg.operator_id)
    }
    if not planned_automatic:
        raise LiveReadinessError("Live plan has no certified automatic hedge/execution venue.")

    for operator_id in sorted(planned_automatic):
        environment = execution_environment(operator_id)
        if not certifications.valid(operator_id, environment):
            raise LiveReadinessError(
                f"Automatic venue {operator_id}/{environment} has no current successful certification."
            )

    now = datetime.now(timezone.utc)
    required_outcomes = {leg.outcome for leg in opportunity.legs}
    coverage: dict[str, set[str]] = defaultdict(set)

    for quote in quotes:
        if quote.event_id != opportunity.event_id:
            continue
        if quote.market_signature != opportunity.market_signature:
            continue
        if quote.outcome not in required_outcomes or not quote.operator_id:
            continue
        age = (now - quote.observed_at).total_seconds()
        if age < -5 or age > max_quote_age_seconds:
            continue
        if not quote.source_market_id or not quote.source_selection_id:
            continue
        if not _automatic(quote.operator_id):
            continue
        environment = execution_environment(quote.operator_id)
        if not certifications.valid(quote.operator_id, environment):
            continue
        coverage[quote.operator_id].add(quote.outcome)

    rescue_map: dict[str, list[str]] = {}
    for failed_operator in sorted(planned_automatic):
        alternatives = sorted(
            operator_id
            for operator_id, outcomes in coverage.items()
            if operator_id != failed_operator and required_outcomes.issubset(outcomes)
        )
        if not alternatives:
            raise LiveReadinessError(
                f"No independent certified rescue venue can replace {failed_operator} "
                f"for all outcomes of {opportunity.event_market_key}."
            )
        rescue_map[failed_operator] = alternatives

    return rescue_map
