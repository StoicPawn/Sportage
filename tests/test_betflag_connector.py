from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from arbengine.connectors.base import BetOrder, BetSide, ExecutionStatus, TimeInForce
from arbengine.connectors.betflag import (
    BetFlagExchangeExecutionConnector,
    BetFlagExchangeMarketDataConnector,
)
from arbengine.connectors.execution import build_execution_connector
from arbengine.models import MarketType
from arbengine.operators import ExecutionAccess, MarketDataAccess, operator_spec


class FakeBetFlagClient:
    def __init__(self, *, match_cents: int = 1000):
        self.match_cents = match_cents
        self.calls = []

    def request(self, method, path, *, json=None, session=False):
        self.calls.append((method, path, json, session))
        if path == "/navigation/menu-ex":
            local = datetime.now(ZoneInfo("Europe/Rome")) + timedelta(hours=1)
            return {
                "d": [
                    {
                        "ds": "CALCIO",
                        "n": [{"m": [{"a": [{
                            "id": "411222",
                            "da": "Lazio - Hellas Verona",
                            "dt": local.replace(tzinfo=None).isoformat(timespec="seconds"),
                            "s": [{
                                "id": "19901289",
                                "ds": "Esito Finale 1x2",
                                "e": [
                                    {"id": "1", "de": "1"},
                                    {"id": "2", "de": "X"},
                                    {"id": "3", "de": "2"},
                                ],
                                "iad": "",
                            }],
                        }]}]}],
                    }
                ]
            }
        if path == "/offers/market/19901289,3":
            return {
                "offerte": {
                    "banca": [{"esi": [
                        {"c": 1, "d": "1", "off": [{"i": 500, "p": 1, "q": 152}]},
                        {"c": 2, "d": "X", "off": [{"i": 600, "p": 1, "q": 325}]},
                        {"c": 3, "d": "2", "off": [{"i": 700, "p": 1, "q": 600}]},
                    ]}],
                    "punta": [],
                    "stato_betflag": 2,
                    "stato_sogei": 2,
                    "versione": 7,
                }
            }
        if method == "POST" and path == "/offers":
            amount = json["offerta"][0]["importo"]
            return {
                "errore": "",
                "esito": 1024,
                "ordine": [{
                    "abbinato": min(self.match_cents, amount),
                    "importo": amount,
                    "offerta": "BC0001",
                    "tipo": json["offerta"][0]["tipo"],
                }],
            }
        if method == "DELETE" and path == "/offers":
            return {"errore": "", "esito": 1024, "offerta": [{"id": "BC0001"}]}
        if path.startswith("/offers/all/"):
            return {"errore": "", "esito": 1024, "offerte": {"o": []}}
        raise AssertionError(f"Unexpected fake call: {method} {path}")


def test_market_data_maps_official_book_to_native_quote():
    client = FakeBetFlagClient()
    connector = BetFlagExchangeMarketDataConnector(client=client, max_events=5)
    quotes = connector.fetch_quotes()
    assert len(quotes) == 3
    home = next(q for q in quotes if q.outcome == "Lazio")
    draw = next(q for q in quotes if q.outcome == "Draw")
    assert home.market == MarketType.ONE_X_TWO
    assert home.odds == Decimal("1.52")
    assert home.available_size == Decimal("5")
    assert home.source_market_id == "19901289"
    assert home.source_selection_id == "1"
    assert home.source_market_version == "7"
    assert home.operator_id == "betflag"
    assert home.source == "betflag_exchange_api"
    assert draw.odds == Decimal("3.25")


def test_back_payload_uses_documented_cents_and_type_one():
    connector = BetFlagExchangeExecutionConnector(client=FakeBetFlagClient())
    order = BetOrder(
        operator_id="betflag", market_id="813016", selection_id="1",
        stake=Decimal("141.00"), limit_odds=Decimal("1.79"), side=BetSide.BACK,
        market_version="4",
    )
    payload = connector._payload(order)
    offer = payload["offerta"][0]
    assert payload["puntata"] == 14100
    assert payload["versione_mercato"] == 4
    assert offer == {
        "tipo": 1, "esito": 1, "quota": 179,
        "importo": 14100, "max_esposizione": 14100,
    }


def test_lay_payload_uses_documented_liability():
    connector = BetFlagExchangeExecutionConnector(client=FakeBetFlagClient())
    order = BetOrder(
        operator_id="betflag", market_id="813016", selection_id="1",
        stake=Decimal("115.00"), limit_odds=Decimal("1.76"), side=BetSide.LAY,
    )
    offer = connector._payload(order)["offerta"][0]
    assert offer["tipo"] == 0
    assert offer["importo"] == 11500
    assert offer["max_esposizione"] == 8740


def test_full_match_is_accepted(monkeypatch):
    monkeypatch.setenv("SPORTAGE_LIVE_EXECUTION", "true")
    client = FakeBetFlagClient(match_cents=1000)
    connector = BetFlagExchangeExecutionConnector(client=client)
    result = connector.place_order(
        BetOrder(
            operator_id="betflag", market_id="813016", selection_id="1",
            stake=Decimal("10"), limit_odds=Decimal("1.79"),
            time_in_force=TimeInForce.FILL_OR_KILL,
        ),
        live=True,
    )
    assert result.status == ExecutionStatus.ACCEPTED
    assert result.matched_stake == Decimal("10")
    assert not any(method == "DELETE" for method, *_ in client.calls)


def test_partial_match_cancels_unmatched_and_reports_partial(monkeypatch):
    monkeypatch.setenv("SPORTAGE_LIVE_EXECUTION", "true")
    client = FakeBetFlagClient(match_cents=400)
    connector = BetFlagExchangeExecutionConnector(client=client)
    result = connector.place_order(
        BetOrder(
            operator_id="betflag", market_id="813016", selection_id="1",
            stake=Decimal("10"), limit_odds=Decimal("1.79"),
            time_in_force=TimeInForce.FILL_OR_KILL,
        ),
        live=True,
    )
    assert result.status == ExecutionStatus.PARTIALLY_MATCHED
    assert result.matched_stake == Decimal("4")
    assert result.remaining_stake == Decimal("6")
    deletes = [call for call in client.calls if call[0] == "DELETE" and call[1] == "/offers"]
    assert len(deletes) == 1
    assert deletes[0][2]["offerte"][0]["id"] == "BC0001"


def test_betflag_registry_is_official_and_automatic():
    spec = operator_spec("betflag")
    assert spec.market_data_access == MarketDataAccess.OFFICIAL_PUBLIC_API
    assert spec.execution_access == ExecutionAccess.OFFICIAL_API
    assert build_execution_connector("betflag").automatic_execution is True
