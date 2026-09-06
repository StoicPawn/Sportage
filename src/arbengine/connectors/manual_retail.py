from __future__ import annotations

from .base import BetOrder, ExecutionConnector, ExecutionResult, ExecutionStatus


class ManualRetailExecutionConnector(ExecutionConnector):
    operator_id = "unknown"

    def place_order(self, order: BetOrder, *, live: bool = False) -> ExecutionResult:
        if order.operator_id != self.operator_id:
            return ExecutionResult(
                operator_id=self.operator_id,
                status=ExecutionStatus.REJECTED,
                message=f"Order targets {order.operator_id}, not {self.operator_id}",
            )
        return ExecutionResult(
            operator_id=self.operator_id,
            status=ExecutionStatus.MANUAL_REQUIRED,
            message=(
                "No public official retail placement API is configured for this operator. "
                "Sportage can prepare the order and deep link, but the final placement must be manual."
            ),
            requested_stake=order.stake,
            requested_odds=order.limit_odds,
        )


class Bet365ExecutionConnector(ManualRetailExecutionConnector):
    operator_id = "bet365"


class SNAIExecutionConnector(ManualRetailExecutionConnector):
    operator_id = "snai"


class SisalExecutionConnector(ManualRetailExecutionConnector):
    operator_id = "sisal"


class EurobetExecutionConnector(ManualRetailExecutionConnector):
    operator_id = "eurobet"


class GoldbetExecutionConnector(ManualRetailExecutionConnector):
    operator_id = "goldbet"


class LottomaticaExecutionConnector(ManualRetailExecutionConnector):
    operator_id = "lottomatica"


class Planetwin365ExecutionConnector(ManualRetailExecutionConnector):
    operator_id = "planetwin365"


class BetssonExecutionConnector(ManualRetailExecutionConnector):
    operator_id = "betsson"


class CodereExecutionConnector(ManualRetailExecutionConnector):
    operator_id = "codere"


class BwinExecutionConnector(ManualRetailExecutionConnector):
    operator_id = "bwin"


class WilliamHillExecutionConnector(ManualRetailExecutionConnector):
    operator_id = "william_hill"


class WinamaxExecutionConnector(ManualRetailExecutionConnector):
    operator_id = "winamax"
