from __future__ import annotations

from arbengine.operators import operator_spec

from .base import ExecutionConnector
from .betfair import BetfairExchangeExecutionConnector
from .manual_retail import (
    Bet365ExecutionConnector,
    BetFlagExecutionConnector,
    BetssonExecutionConnector,
    BwinExecutionConnector,
    CodereExecutionConnector,
    EurobetExecutionConnector,
    GoldbetExecutionConnector,
    LottomaticaExecutionConnector,
    Planetwin365ExecutionConnector,
    SNAIExecutionConnector,
    SisalExecutionConnector,
    WilliamHillExecutionConnector,
    WinamaxExecutionConnector,
)


_EXECUTION_CONNECTORS: dict[str, type[ExecutionConnector]] = {
    "bet365": Bet365ExecutionConnector,
    "betfair": BetfairExchangeExecutionConnector,
    "snai": SNAIExecutionConnector,
    "sisal": SisalExecutionConnector,
    "eurobet": EurobetExecutionConnector,
    "goldbet": GoldbetExecutionConnector,
    "lottomatica": LottomaticaExecutionConnector,
    "planetwin365": Planetwin365ExecutionConnector,
    "betsson": BetssonExecutionConnector,
    "codere": CodereExecutionConnector,
    "betflag": BetFlagExecutionConnector,
    "bwin": BwinExecutionConnector,
    "william_hill": WilliamHillExecutionConnector,
    "winamax": WinamaxExecutionConnector,
}


def build_execution_connector(operator: str) -> ExecutionConnector:
    spec = operator_spec(operator)
    connector_cls = _EXECUTION_CONNECTORS[spec.operator_id]
    return connector_cls()


def execution_connector_ids() -> tuple[str, ...]:
    return tuple(sorted(_EXECUTION_CONNECTORS))
