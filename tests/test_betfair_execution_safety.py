from __future__ import annotations

from decimal import Decimal

from arbengine.connectors.base import BetOrder, ExecutionStatus, TimeInForce
from arbengine.connectors.betfair import BetfairExchangeExecutionConnector


class FakeClient:
    def __init__(self):
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "placeOrders":
            return {
                "result": {
                    "status": "SUCCESS",
                    "instructionReports": [
                        {
                            "status": "SUCCESS",
                            "betId": "12345",
                            "sizeMatched": 100.0,
                            "averagePriceMatched": 2.1,
                            "orderStatus": "EXECUTION_COMPLETE",
                        }
                    ],
                },
                "raw": {"result": "ok"},
            }
        raise AssertionError(method)


def test_betfair_live_fok_uses_market_version_and_persistent_order_ref(monkeypatch):
    monkeypatch.setenv("SPORTAGE_LIVE_EXECUTION", "true")
    connector = BetfairExchangeExecutionConnector(app_key="key", session_token="token")
    fake = FakeClient()
    connector.client = fake
    order = BetOrder(
        operator_id="betfair",
        market_id="1.123",
        selection_id="456",
        stake=Decimal("100"),
        limit_odds=Decimal("2.10"),
        customer_ref="sportage-request-1",
        customer_order_ref="sportage-order-1",
        market_version="77",
        time_in_force=TimeInForce.FILL_OR_KILL,
        min_fill_size=Decimal("100"),
    )

    result = connector.place_order(order, live=True)
    assert result.status == ExecutionStatus.ACCEPTED
    assert result.fully_matched

    method, params = fake.calls[0]
    assert method == "placeOrders"
    assert params["marketVersion"] == {"version": 77}
    assert params["customerRef"] == "sportage-request-1"
    instruction = params["instructions"][0]
    assert instruction["customerOrderRef"] == "sportage-order-1"
    assert instruction["limitOrder"]["timeInForce"] == "FILL_OR_KILL"
    assert instruction["limitOrder"]["minFillSize"] == 100.0
