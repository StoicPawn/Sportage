from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx

from arbengine.models import MarketType, Quote
from arbengine.providers.base import OddsProvider

from .base import (
    BetOrder,
    BetSide,
    ExecutionConnector,
    ExecutionPreflight,
    ExecutionResult,
    ExecutionStatus,
    MarketDataConnector,
    TimeInForce,
)


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
    """Direct Betfair Exchange best-back market data via official API-NG."""

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
                "marketProjection": ["EVENT", "EVENT_TYPE", "MARKET_START_TIME", "RUNNER_DESCRIPTION"],
                "sort": "FIRST_TO_START",
                "maxResults": str(self.max_results),
            },
        )["result"]
        if not catalogue_result:
            return []

        market_ids = [str(item["marketId"]) for item in catalogue_result]
        books = self.client.call(
            "listMarketBook",
            {"marketIds": market_ids, "priceProjection": {"priceData": ["EX_BEST_OFFERS"]}},
        )["result"]
        books_by_id = {str(book["marketId"]): book for book in books}
        quotes: list[Quote] = []

        for catalogue in catalogue_result:
            market_id = str(catalogue["marketId"])
            book = books_by_id.get(market_id)
            if not book or book.get("status") not in {None, "OPEN"}:
                continue
            market_version = None if book.get("version") is None else str(book.get("version"))
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
            commence = datetime.fromisoformat(str(start_value).replace("Z", "+00:00")) if start_value else now
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
                size = None if best.get("size") is None else Decimal(str(best.get("size")))
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
                        source_market_id=market_id,
                        source_selection_id=selection_id,
                        source_market_version=market_version,
                        available_size=size,
                    )
                )
        return quotes


class BetfairExchangeExecutionConnector(ExecutionConnector):
    operator_id = "betfair"

    def __init__(self, app_key: str | None = None, session_token: str | None = None, timeout: float = 15.0) -> None:
        self.client = _BetfairClient(app_key, session_token, timeout)

    @staticmethod
    def _live_enabled() -> bool:
        return os.getenv("SPORTAGE_LIVE_EXECUTION", "false").strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _price_side(order: BetOrder, runner: dict[str, Any]) -> list[dict[str, Any]]:
        ex = runner.get("ex") or {}
        return (ex.get("availableToBack") or []) if order.side == BetSide.BACK else (ex.get("availableToLay") or [])

    def preflight(self, order: BetOrder) -> ExecutionPreflight:
        if order.operator_id != self.operator_id:
            return ExecutionPreflight(operator_id=self.operator_id, ok=False, message="Order targets another operator.")
        book_result = self.client.call(
            "listMarketBook",
            {"marketIds": [order.market_id], "priceProjection": {"priceData": ["EX_BEST_OFFERS"]}},
        )
        books = book_result["result"]
        if not books:
            return ExecutionPreflight(operator_id=self.operator_id, ok=False, message="Betfair market not found.")
        book = books[0]
        version = None if book.get("version") is None else str(book.get("version"))
        if book.get("status") != "OPEN":
            return ExecutionPreflight(
                operator_id=self.operator_id, ok=False, message=f"Market status is {book.get('status')}",
                market_open=False, market_version=version, raw=book_result["raw"],
            )
        runner = next(
            (r for r in book.get("runners") or [] if str(r.get("selectionId")) == str(order.selection_id)), None
        )
        if runner is None:
            return ExecutionPreflight(
                operator_id=self.operator_id, ok=False, message="Selection not present in market.",
                market_open=True, market_version=version, raw=book_result["raw"],
            )
        available = self._price_side(order, runner)
        if not available:
            return ExecutionPreflight(
                operator_id=self.operator_id, ok=False, message="No executable price available.",
                market_open=True, market_version=version, raw=book_result["raw"],
            )
        best = available[0]
        price = Decimal(str(best["price"]))
        size = Decimal(str(best.get("size", "0")))
        price_ok = price >= order.limit_odds if order.side == BetSide.BACK else price <= order.limit_odds
        required = order.min_fill_size or (order.stake if order.time_in_force == TimeInForce.FILL_OR_KILL else Decimal("0"))
        size_ok = size >= required if required > 0 else True
        return ExecutionPreflight(
            operator_id=self.operator_id,
            ok=price_ok and size_ok,
            message=("Preflight passed." if price_ok and size_ok else f"Price/size outside limits: {price} x {size}."),
            market_open=True,
            current_odds=price,
            available_size=size,
            market_version=version,
            raw=book_result["raw"],
        )

    def place_order(self, order: BetOrder, *, live: bool = False) -> ExecutionResult:
        if order.operator_id != self.operator_id:
            return ExecutionResult(operator_id=self.operator_id, status=ExecutionStatus.REJECTED, message="Wrong operator.")
        if not live:
            return ExecutionResult(
                operator_id=self.operator_id, status=ExecutionStatus.DRY_RUN,
                message="Betfair order validated in dry-run mode; no bet was sent.",
                customer_order_ref=order.customer_order_ref,
                requested_stake=order.stake, requested_odds=order.limit_odds,
            )
        if not self._live_enabled():
            return ExecutionResult(
                operator_id=self.operator_id, status=ExecutionStatus.REJECTED,
                message="Live execution is disabled. Set SPORTAGE_LIVE_EXECUTION=true explicitly.",
                customer_order_ref=order.customer_order_ref,
                requested_stake=order.stake, requested_odds=order.limit_odds,
            )

        customer_ref = (order.customer_ref or f"sportage-{uuid.uuid4().hex[:20]}")[:32]
        customer_order_ref = (order.customer_order_ref or f"sp-{uuid.uuid4().hex[:24]}")[:32]
        limit_order: dict[str, Any] = {
            "size": float(order.stake), "price": float(order.limit_odds), "persistenceType": "LAPSE",
        }
        if order.time_in_force == TimeInForce.FILL_OR_KILL:
            limit_order["timeInForce"] = "FILL_OR_KILL"
            limit_order["minFillSize"] = float(order.min_fill_size or order.stake)
        instruction: dict[str, Any] = {
            "selectionId": int(order.selection_id), "handicap": 0.0, "side": order.side.value,
            "orderType": "LIMIT", "limitOrder": limit_order, "customerOrderRef": customer_order_ref,
        }
        params: dict[str, Any] = {
            "marketId": order.market_id, "instructions": [instruction], "customerRef": customer_ref,
        }
        if order.customer_strategy_ref:
            params["customerStrategyRef"] = order.customer_strategy_ref[:15]
        if order.market_version is not None:
            try:
                version: int | str = int(order.market_version)
            except ValueError:
                version = order.market_version
            params["marketVersion"] = {"version": version}

        response = self.client.call("placeOrders", params)
        result = response["result"]
        reports = result.get("instructionReports") or []
        report = reports[0] if reports else {}
        bet_id = report.get("betId")
        matched = Decimal(str(report.get("sizeMatched", 0) or 0))
        average = report.get("averagePriceMatched")
        order_status = report.get("orderStatus")
        success = result.get("status") == "SUCCESS" and report.get("status") in {None, "SUCCESS"}
        if not success:
            status = ExecutionStatus.REJECTED
        elif matched >= order.stake:
            status = ExecutionStatus.ACCEPTED
        elif matched > 0:
            status = ExecutionStatus.PARTIALLY_MATCHED
        elif order.time_in_force == TimeInForce.FILL_OR_KILL:
            status = ExecutionStatus.CANCELLED
        else:
            status = ExecutionStatus.PENDING
        return ExecutionResult(
            operator_id=self.operator_id,
            status=status,
            message=f"Betfair placeOrders: {status.value}",
            bet_id=None if bet_id is None else str(bet_id),
            customer_order_ref=customer_order_ref,
            requested_stake=order.stake,
            requested_odds=order.limit_odds,
            matched_stake=matched,
            average_price_matched=None if average is None else Decimal(str(average)),
            remaining_stake=max(Decimal("0"), order.stake - matched),
            order_status=None if order_status is None else str(order_status),
            raw=response["raw"],
        )

    def reconcile_order(
        self, *, bet_id: str | None = None, customer_order_ref: str | None = None
    ) -> ExecutionResult:
        if not bet_id and not customer_order_ref:
            return ExecutionResult(
                operator_id=self.operator_id, status=ExecutionStatus.UNKNOWN,
                message="Need bet_id or customer_order_ref to reconcile.",
            )
        params: dict[str, Any] = {"orderProjection": "ALL"}
        if bet_id:
            params["betIds"] = [bet_id]
        if customer_order_ref:
            params["customerOrderRefs"] = [customer_order_ref]
        response = self.client.call("listCurrentOrders", params)
        orders = response["result"].get("currentOrders") or []
        if not orders:
            return ExecutionResult(
                operator_id=self.operator_id, status=ExecutionStatus.UNKNOWN,
                message="Order not present in listCurrentOrders; manual/cleared-order review required.",
                bet_id=bet_id, customer_order_ref=customer_order_ref, raw=response["raw"],
            )
        row = orders[0]
        requested = Decimal(str(row.get("sizeMatched", 0) or 0)) + Decimal(str(row.get("sizeRemaining", 0) or 0))
        matched = Decimal(str(row.get("sizeMatched", 0) or 0))
        remaining = Decimal(str(row.get("sizeRemaining", 0) or 0))
        if matched > 0 and remaining == 0:
            status = ExecutionStatus.ACCEPTED
        elif matched > 0:
            status = ExecutionStatus.PARTIALLY_MATCHED
        elif row.get("status") == "EXECUTABLE":
            status = ExecutionStatus.PENDING
        else:
            status = ExecutionStatus.CANCELLED
        return ExecutionResult(
            operator_id=self.operator_id, status=status,
            message=f"Betfair reconciled order: {status.value}", bet_id=str(row.get("betId") or bet_id or "") or None,
            customer_order_ref=str(row.get("customerOrderRef") or customer_order_ref or "") or None,
            requested_stake=requested if requested > 0 else None,
            requested_odds=None if row.get("priceSize") is None else Decimal(str((row.get("priceSize") or {}).get("price"))),
            matched_stake=matched, average_price_matched=Decimal(str(row.get("averagePriceMatched", 0) or 0)) or None,
            remaining_stake=remaining, order_status=row.get("status"), raw=response["raw"],
        )

    def cancel_order(self, bet_id: str, *, market_id: str | None = None, live: bool = False) -> ExecutionResult:
        if not live:
            return ExecutionResult(
                operator_id=self.operator_id, status=ExecutionStatus.DRY_RUN,
                message="Betfair cancellation validated in dry-run mode.", bet_id=bet_id,
            )
        if not self._live_enabled():
            return ExecutionResult(
                operator_id=self.operator_id, status=ExecutionStatus.REJECTED,
                message="Live execution is disabled.", bet_id=bet_id,
            )
        params: dict[str, Any] = {"instructions": [{"betId": bet_id}]}
        if market_id:
            params["marketId"] = market_id
        response = self.client.call("cancelOrders", params)
        result = response["result"]
        success = result.get("status") == "SUCCESS"
        return ExecutionResult(
            operator_id=self.operator_id,
            status=ExecutionStatus.CANCELLED if success else ExecutionStatus.REJECTED,
            message="Betfair unmatched remainder cancelled." if success else f"Betfair cancellation failed: {result.get('errorCode')}",
            bet_id=bet_id, raw=response["raw"],
        )
