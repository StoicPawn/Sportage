from decimal import Decimal

from arbengine.connectors.base import BetOrder, ExecutionStatus
from arbengine.connectors.execution import build_execution_connector, execution_connector_ids


def _order(operator_id: str) -> BetOrder:
    return BetOrder(
        operator_id=operator_id,
        market_id="1.234",
        selection_id="123",
        stake=Decimal("10"),
        limit_odds=Decimal("2.10"),
    )


def test_all_tier_one_two_execution_connectors_exist():
    assert set(execution_connector_ids()) == {
        "bet365", "betfair", "snai", "sisal", "eurobet", "goldbet", "lottomatica",
        "planetwin365", "betsson", "codere", "betflag", "bwin", "william_hill", "winamax",
    }


def test_official_exchange_connectors_default_to_dry_run():
    for operator_id in ("betfair", "betflag"):
        connector = build_execution_connector(operator_id)
        result = connector.place_order(_order(operator_id), live=False)
        assert result.status == ExecutionStatus.DRY_RUN
        assert result.requested_stake == Decimal("10")
        assert connector.automatic_execution is True


def test_non_public_retail_api_connectors_require_manual_placement():
    for operator_id in (
        "bet365", "snai", "sisal", "eurobet", "goldbet", "lottomatica",
        "planetwin365", "betsson", "codere", "bwin", "william_hill", "winamax",
    ):
        connector = build_execution_connector(operator_id)
        result = connector.place_order(_order(operator_id), live=False)
        assert result.status == ExecutionStatus.MANUAL_REQUIRED
