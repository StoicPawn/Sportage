from __future__ import annotations

from arbengine.models import Quote
from arbengine.operators import OPERATORS, operator_spec
from arbengine.providers.base import OddsProvider
from arbengine.providers.unified import normalize_quote

from .base import MarketDataConnector


class AggregatedOperatorMarketDataConnector(MarketDataConnector):
    """Operator-specific view over a shared upstream feed.

    The shared provider is fetched by the caller/hub; this class is useful when a
    workflow needs an explicit connector object per bookmaker without changing the
    canonical Quote contract.
    """

    def __init__(self, operator: str, provider: OddsProvider) -> None:
        self.spec = operator_spec(operator)
        self.operator_id = self.spec.operator_id
        self.provider = provider

    def fetch_quotes(self) -> list[Quote]:
        result: list[Quote] = []
        for raw in self.provider.fetch_quotes():
            quote = normalize_quote(raw)
            if quote is not None and quote.operator_id == self.operator_id:
                result.append(quote)
        return result


def supported_operator_ids() -> tuple[str, ...]:
    return tuple(sorted(OPERATORS))
