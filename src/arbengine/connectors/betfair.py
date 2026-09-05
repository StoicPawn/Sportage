from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx

from arbengine.models import MarketType, Quote
from arbengine.providers.base import OddsProvider

from .base import BetOrder, ExecutionConnector, ExecutionResult, ExecutionStatus, MarketDataConnector


class BetfairAPIError(RuntimeError):
    pass


class _BetfairClient:
    ENDPOINT = "https://api.betfair.com/exchange/betting/json-rpc/v1"

    def __init__(
        self,
        app_key: str | None = None,
        session_token: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.app_key = app_key or os.getenv("BETFAIR_APP_KEY")
        self.session_token = session_token or os.getenv("BETFAIR_SESSION_TOKEN")
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        if not self.app_key or not self.session_token:
            raise BetfairAPIError(
                "BETFAIR_APP_KEY and BETFAIR_SESSION_TOKEN are required for Betfair API calls"
            )
        return {
            "X-Application": self.app_key,
            "X-Authentication": self.session_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "jsonrpc": "2.0",
            "method": f"SportsAPING/v1.0/{method}",
            "params": params,
            "id": 1,
        }
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(self.ENDPOINT, headers=self._headers(), json=payload)
            response.raise_for_status()
            data = response.json()
        if not isinstance(data, dict):
            raise BetfairAPIError("Unexpected Betfair API response")
        if data.get("error"):
            raise BetfairAPIError(str(data["error"]))
        result = data.get("result")
        if result is None:
            raise BetfairAPIError("Betfair API response has no result")
        return {"result": result, "raw": data}


class BetfairExchangeMarketDataConnector(OddsProvider, MarketDataConnector):
    """Direct Betfair Exchange best-back market data via official API-NG.

    The initial connector intentionally focuses on MATCH_ODDS markets because they
    map cleanly into Sportage H2H/1X2. Other Betfair market types can be added without
    changing the normalized Quote contract.
    """

    operator_id = "betfair"

    def __init__(
        self,
        app_key: str | None = None,
        session_token: str | None = None,
        horizon_hours: float = 24.0,
        max_results: int = 100,
        timeout: float = 15.0,
    ) -> None:
        self.client = _BetfairClient(app_key, session_token, timeout)
        self.horizon_hours = horizon_hours
        self.max_results = max_results

    @staticmethod
    def _participants(event_name: str, runner_names: list[str]) -> tuple[str, str]:
        for separator in (" v ", " vs ", " @ "):
            if separator in event_name:
                left, right = event_name.split(separator, 1)
                return left.strip(), right.strip()
        non_draw = [name for name in runner_names if name.strip().lower() not in {"the draw", "draw", "x"}]
        if len(non_draw) >= 2:
            return non_draw[0], non_draw[1]
        if len(runner_names) >= 2:
            return runner_names[0], runner_names[1]
        return event_name or "Home", "Away"

    def fetch_quotes(self) -> list[Quote]:
        now = datetime.now(timezone.utc)
        to_time = now + timedelta(hours=self.horizon_hours)
        catalogue_result = self.client.call(
            "listMarketCatalogue",
            {
                "filter": {
                    "marketTypeCodes": ["MATCH_ODDS"],
                    "marketStartTime": {"from": now.isoformat(), "to": to_time.isoformat()},
                },
                "marketProjection": [
                    "EVENT",
                    "EVENT_TYPE",
                    "MARKET_START_TIME",
                    "RUNNER_DESCRIPTION",
                ],
                "sort": "FIRST_TO_START",
                "maxResults": str(self.max_results),
            },
        )["result"]
        if not catalogue_result:
            return []

        market_ids = [str(item["marketId"]) for item in catalogue_result]
        books = self.client.call(
            "listMarketBook",
            {
                "marketIds": market_ids,
                "priceProjection": {"priceData": ["EX_BEST_OFFERS"]},
            },
        )["result"]
        books_by_id = {str(book["marketId"]): book for book in books}
        quotes: list[Quote] = []

        for catalogue in catalogue_result:
            market_id = str(catalogue["marketId"])
            book = books_by_id.get(market_id)
            if not book or book.get("status") not in {None, "OPEN"}:
                continue
            runners_catalogue = catalogue.get("runners") or []
            name_by_selection = {
                str(runner["selectionId"]): str(runner.get("runnerName", runner["selectionId"]))
                for runner in runners_catalogue
            }
            runner_names = list(name_by_selection.values())
            event = catalogue.get("event") or {}
            event_name = str(event.get("name") or catalogue.get("marketName") or market_id)
            home, away = self._participants(event_name, runner_names)
            expected = len(runners_catalogue)
            market_type = MarketType.ONE_X_TWO if expected == 3 else MarketType.H2H
            start_value = catalogue.get("marketStartTime")
            commence = (
                datetime.fromisoformat(str(start_value).replace("Z", "+00:00"))
                if start_value
                else now
            )
            event_type = catalogue.get("eventType") or {}
            sport = str(event_type.get("name") or "betfair_exchange")
            source_event_id = str(event.get("id") or market_id)

            for runner in book.get("runners") or []:
                selection_id = str(runner.get("selectionId"))
                available = ((runner.get("ex") or {}).get("availableToBack") or [])
                if not available:
                    continue
                best = available[0]
                price = Decimal(str(best.get("price")))
                if price <= 1:
                    continue
                outcome = name_by_selection.get(selection_id, selection_id)
                quotes.append(
                    Quote(
                        event_id=source_event_id,
                        source_event_id=source_event_id,
                        operator_id=self.operator_id,
                        sport=sport,
                        commence_time=commence,
                        home=home,
                        away=away,
                        market=market_type,
                        outcome=outcome,
                        bookmaker="Betfair Exchange",
                        odds=price,
                        expected_outcomes=expected,
                        observed_at=now,
                        source="betfair_api_ng",
                    )
                )
        return quotes


class BetfairExchangeExecutionConnector(ExecutionConnector):
    operator_id = "betfair"

    def __init__(
        self,
        app_key: str | None = None,
        session_token: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.client = _BetfairClient(app_key, session_token, timeout)

    @staticmethod
    def _live_enabled() -> bool:
        return os.getenv("SPORTAGE_LIVE_EXECUTION", "false").strip().lower() in {
            "1", "true", "yes", "on"
        }

    def place_order(self, order: BetOrder, *, live: bool = False) -> ExecutionResult:
        if order.operator_id != self.operator_id:
            return ExecutionResult(
                operator_id=self.operator_id,
                status=ExecutionStatus.REJECTED,
                message=f"Order targets {order.operator_id}, not betfair",
            )
        if not live:
            return ExecutionResult(
                operator_id=self.operator_id,
                status=ExecutionStatus.DRY_RUN,
                message="Betfair order validated in dry-run mode; no bet was sent.",
                requested_stake=order.stake,
                requested_odds=order.limit_odds,
            )
        if not self._live_enabled():
            return ExecutionResult(
                operator_id=self.operator_id,
                status=ExecutionStatus.REJECTED,
                message="Live execution is disabled. Set SPORTAGE_LIVE_EXECUTION=true explicitly.",
                requested_stake=order.stake,
                requested_odds=order.limit_odds,
            )

        customer_ref = order.customer_ref or f"sportage-{uuid.uuid4().hex[:20]}"
        response = self.client.call(
            "placeOrders",
            {
                "marketId": order.market_id,
                "instructions": [
                    {
                        "selectionId": int(order.selection_id),
                        "handicap": 0.0,
                        "side": order.side.value,
                        "orderType": "LIMIT",
                        "limitOrder": {
                            "size": float(order.stake),
                            "price": float(order.limit_odds),
                            "persistenceType": "LAPSE",
                        },
                    }
                ],
                "customerRef": customer_ref,
            },
        )
        result = response["result"]
        reports = result.get("instructionReports") or []
        report = reports[0] if reports else {}
        success = result.get("status") == "SUCCESS" and report.get("status") in {None, "SUCCESS"}
        bet_id = report.get("betId")
        size_matched = report.get("sizeMatched")
        average_price = report.get("averagePriceMatched")
        error = report.get("errorCode") or result.get("errorCode")
        return ExecutionResult(
            operator_id=self.operator_id,
            status=ExecutionStatus.ACCEPTED if success else ExecutionStatus.REJECTED,
            message="Betfair accepted the order." if success else f"Betfair rejected the order: {error or 'unknown error'}",
            bet_id=None if bet_id is None else str(bet_id),
            requested_stake=order.stake,
            requested_odds=order.limit_odds,
            matched_stake=None if size_matched is None else Decimal(str(size_matched)),
            average_price_matched=None if average_price is None else Decimal(str(average_price)),
            raw=response["raw"],
        )
