from __future__ import annotations

from dataclasses import dataclass

from arbengine.operators import MarketDataAccess, OperatorSpec, operator_spec
from arbengine.providers.base import OddsProvider

from .base import ExecutionConnector, MarketDataConnector
from .betfair import BetfairExchangeMarketDataConnector
from .execution import build_execution_connector
from .market_data import AggregatedOperatorMarketDataConnector


@dataclass
class OperatorConnectorBundle:
    spec: OperatorSpec
    market_data: MarketDataConnector | None
    execution: ExecutionConnector


def build_operator_bundle(
    operator: str,
    *,
    shared_market_provider: OddsProvider | None = None,
) -> OperatorConnectorBundle:
    spec = operator_spec(operator)
    market_data: MarketDataConnector | None
    if spec.operator_id == "betfair":
        market_data = BetfairExchangeMarketDataConnector()
    elif shared_market_provider is not None:
        market_data = AggregatedOperatorMarketDataConnector(spec.operator_id, shared_market_provider)
    else:
        market_data = None

    return OperatorConnectorBundle(
        spec=spec,
        market_data=market_data,
        execution=build_execution_connector(spec.operator_id),
    )


def direct_market_data_available(operator: str) -> bool:
    spec = operator_spec(operator)
    return spec.market_data_access in {
        MarketDataAccess.OFFICIAL_PUBLIC_API,
        MarketDataAccess.OFFICIAL_PARTNER_API,
    }
