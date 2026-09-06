from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any
from zoneinfo import ZoneInfo

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


CENT = Decimal("0.01")


class BetFlagAPIError(RuntimeError):
    pass


def _cents(value: Decimal) -> int:
    rounded = value.quantize(CENT, rounding=ROUND_DOWN)
    return int(rounded * 100)


def _decimal_odds(raw: Any) -> Decimal:
    return Decimal(str(raw)) / Decimal("100")


def _split_event(name: str) -> tuple[str, str]:
    for sep in (" - ", " vs ", " v ", " @ "):
        if sep in name:
            left, right = name.split(sep, 1)
            return left.strip(), right.strip()
    return name, "Opponent"


def _market_type(name: str, outcomes: list[dict[str, Any]]) -> MarketType | None:
    key = name.strip().lower()
    if "under" in key and "over" in key:
        return MarketType.TOTALS
    if "1x2" in key or len(outcomes) == 3:
        return MarketType.ONE_X_TWO
    if "testa a testa" in key or len(outcomes) == 2:
        return MarketType.H2H
    return None


def _outcome_name(raw: str, market: MarketType, home: str, away: str) -> str:
    key = raw.strip().lower()
    if market == MarketType.ONE_X_TWO:
        if key == "1":
            return home
        if key in {"x", "draw", "pareggio"}:
            return "Draw"
        if key == "2":
            return away
    if market == MarketType.H2H:
        if key == "1":
            return home
        if key == "2":
            return away
    if market == MarketType.TOTALS:
        if "under" in key:
            return "Under"
        if "over" in key:
            return "Over"
    return raw.strip()


class _BetFlagClient:
    PRODUCTION_BASE = "https://exchange-api-proxy.mediasystemtechnologies.it:4453/api/rest"
    STAGING_BASE = "https://exchange-api-proxy-staging.mstxchange.com/api/rest"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_key_name: str | None = None,
        api_key_location: str | None = None,
        username: str | None = None,
        password: str | None = None,
        session_token: str | None = None,
        environment: str | None = None,
        base_url: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.getenv("BETFLAG_API_KEY")
        self.api_key_name = api_key_name if api_key_name is not None else os.getenv("BETFLAG_API_KEY_NAME")
        self.api_key_location = (
            api_key_location if api_key_location is not None else os.getenv("BETFLAG_API_KEY_LOCATION", "header")
        ).lower()
        self.username = username if username is not None else os.getenv("BETFLAG_USERNAME")
        self.password = password if password is not None else os.getenv("BETFLAG_PASSWORD")
        self.session_token = session_token if session_token is not None else os.getenv("BETFLAG_SESSION_TOKEN")
        self.environment = (environment or os.getenv("BETFLAG_ENVIRONMENT", "staging")).lower()
        if base_url:
            self.base_url = base_url.rstrip("/")
        elif os.getenv("BETFLAG_BASE_URL"):
            self.base_url = os.environ["BETFLAG_BASE_URL"].rstrip("/")
        elif self.environment == "production":
            self.base_url = self.PRODUCTION_BASE
        else:
            self.base_url = self.STAGING_BASE
        self.timeout = timeout

    def _auth(self, session: bool) -> tuple[dict[str, str], dict[str, str]]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        params: dict[str, str] = {}
        if self.api_key:
            if not self.api_key_name:
                raise BetFlagAPIError(
                    "BETFLAG_API_KEY is set but BETFLAG_API_KEY_NAME is missing. "
                    "BetFlag's public OpenAPI labels the scheme api_key without exposing its concrete name."
                )
            if self.api_key_location == "query":
                params[self.api_key_name] = self.api_key
            elif self.api_key_location == "header":
                headers[self.api_key_name] = self.api_key
            else:
                raise BetFlagAPIError("BETFLAG_API_KEY_LOCATION must be 'header' or 'query'")
        if session:
            headers["X-BF-Session-Token"] = self.ensure_session()
        return headers, params

    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        session: bool = False,
    ) -> Any:
        headers, params = self._auth(session)
        with httpx.Client(timeout=self.timeout) as client:
            response = client.request(
                method,
                f"{self.base_url}/{path.lstrip('/')}",
                headers=headers,
                params=params,
                json=json,
            )
            response.raise_for_status()
            data = response.json()
        if isinstance(data, dict) and data.get("errore"):
            raise BetFlagAPIError(str(data["errore"]))
        return data

    def login(self) -> str:
        if not self.username or not self.password:
            raise BetFlagAPIError(
                "BETFLAG_SESSION_TOKEN or BETFLAG_USERNAME + BETFLAG_PASSWORD are required for trading calls"
            )
        data = self.request(
            "POST",
            "/security/session",
            json={"Username": self.username, "Pwd": self.password},
            session=False,
        )
        token = data.get("SessionToken") if isinstance(data, dict) else None
        if not token:
            raise BetFlagAPIError("BetFlag login response contains no SessionToken")
        self.session_token = str(token)
        return self.session_token

    def ensure_session(self) -> str:
        return self.session_token or self.login()


class BetFlagExchangeMarketDataConnector(OddsProvider, MarketDataConnector):
    """Official BetFlag Exchange best executable BACK prices."""

    operator_id = "betflag"
    source_name = "betflag_exchange_api"

    def __init__(
        self,
        *,
        client: _BetFlagClient | None = None,
        horizon_hours: float = 24.0,
        max_events: int = 40,
        max_markets_per_event: int = 8,
        timezone_name: str | None = None,
    ) -> None:
        self.client = client or _BetFlagClient()
        self.horizon_hours = horizon_hours
        self.max_events = max_events
        self.max_markets_per_event = max_markets_per_event
        self.local_tz = ZoneInfo(timezone_name or os.getenv("BETFLAG_TIMEZONE", "Europe/Rome"))

    def _parse_time(self, raw: str) -> datetime:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=self.local_tz)
        return value.astimezone(timezone.utc)

    def _events(self) -> list[dict[str, Any]]:
        data = self.client.request("GET", "/navigation/menu-ex")
        roots = data.get("d", []) if isinstance(data, dict) else []
        events: list[dict[str, Any]] = []
        for sport in roots:
            sport_name = str(sport.get("ds") or "unknown")
            for nation in sport.get("n") or []:
                for competition in nation.get("m") or []:
                    for event in competition.get("a") or []:
                        item = dict(event)
                        item["_sport"] = sport_name
                        events.append(item)
        return events

    def fetch_quotes(self) -> list[Quote]:
        now = datetime.now(timezone.utc)
        horizon = now + timedelta(hours=self.horizon_hours)
        events = []
        for event in self._events():
            raw_time = str(event.get("dt") or "")
            if not raw_time:
                continue
            try:
                start = self._parse_time(raw_time)
            except ValueError:
                continue
            if now - timedelta(minutes=5) <= start <= horizon:
                event["_start"] = start
                events.append(event)
        events.sort(key=lambda e: e["_start"])
        quotes: list[Quote] = []

        for event in events[: self.max_events]:
            name = str(event.get("da") or event.get("des") or event.get("id"))
            home, away = _split_event(name)
            start = event["_start"]
            market_specs = event.get("s") or []
            for market_spec in market_specs[: self.max_markets_per_event]:
                outcomes = market_spec.get("e") or market_spec.get("esi") or []
                market_name = str(market_spec.get("ds") or market_spec.get("des") or "")
                market_type = _market_type(market_name, outcomes)
                if market_type is None:
                    continue
                market_id = str(market_spec.get("id") or market_spec.get("id_mercato") or "")
                if not market_id:
                    continue
                book = self.client.request("GET", f"/offers/market/{market_id},3")
                offers_root = (book.get("offerte") or {}) if isinstance(book, dict) else {}
                # BACK/PUNTA orders consume the opposite BANCA side of the book.
                back_available = offers_root.get("banca") or []
                selection_offers: dict[str, list[dict[str, Any]]] = {}
                for block in back_available:
                    for selection in block.get("esi") or []:
                        selection_offers[str(selection.get("c"))] = selection.get("off") or []
                version = str(
                    offers_root.get("versione")
                    or (book.get("versione") if isinstance(book, dict) else "")
                    or ""
                ) or None
                line_raw = str(market_spec.get("iad") or "").strip()
                line = Decimal(line_raw) if line_raw else None
                expected = len(outcomes)
                if expected < 2:
                    continue
                for outcome in outcomes:
                    selection_id = str(outcome.get("id") or outcome.get("cod") or "")
                    levels = selection_offers.get(selection_id) or []
                    if not levels:
                        continue
                    best = min(levels, key=lambda x: int(x.get("p", 999)))
                    odds = _decimal_odds(best.get("q"))
                    if odds <= 1:
                        continue
                    size = Decimal(str(best.get("i", 0))) / Decimal("100")
                    raw_label = str(outcome.get("de") or outcome.get("des") or selection_id)
                    quotes.append(
                        Quote(
                            event_id=str(event.get("id")),
                            source_event_id=str(event.get("id")),
                            operator_id=self.operator_id,
                            sport=str(event.get("_sport") or "unknown"),
                            commence_time=start,
                            home=home,
                            away=away,
                            market=market_type,
                            outcome=_outcome_name(raw_label, market_type, home, away),
                            bookmaker="BetFlag",
                            odds=odds,
                            expected_outcomes=expected,
                            market_line=line,
                            observed_at=now,
                            source=self.source_name,
                            source_market_id=market_id,
                            source_selection_id=selection_id,
                            source_market_version=version,
                            available_size=size,
                        )
                    )
        return quotes


class BetFlagExchangeExecutionConnector(ExecutionConnector):
    operator_id = "betflag"
    automatic_execution = True

    def __init__(self, *, client: _BetFlagClient | None = None) -> None:
        self.client = client or _BetFlagClient()

    @staticmethod
    def _live_enabled() -> bool:
        return os.getenv("SPORTAGE_LIVE_EXECUTION", "false").strip().lower() in {
            "1", "true", "yes", "on"
        }

    @staticmethod
    def _side_code(side: BetSide) -> int:
        return 1 if side == BetSide.BACK else 0

    def _book_side(self, book: dict[str, Any], side: BetSide) -> list[dict[str, Any]]:
        offers = book.get("offerte") or {}
        # A BACK order consumes BANCA; a LAY order consumes PUNTA.
        return offers.get("banca" if side == BetSide.BACK else "punta") or []

    def _levels(self, book: dict[str, Any], selection_id: str, side: BetSide) -> list[dict[str, Any]]:
        for block in self._book_side(book, side):
            for selection in block.get("esi") or []:
                if str(selection.get("c")) == str(selection_id):
                    return selection.get("off") or []
        return []

    def preflight(self, order: BetOrder) -> ExecutionPreflight:
        try:
            book = self.client.request("GET", f"/offers/market/{order.market_id},3")
        except Exception as exc:
            return ExecutionPreflight(
                operator_id=self.operator_id,
                ok=False,
                message=f"BetFlag market refresh failed: {type(exc).__name__}: {exc}",
            )
        offers = book.get("offerte") or {}
        levels = self._levels(book, order.selection_id, order.side)
        version = str(offers.get("versione") or book.get("versione") or "") or None
        if not levels:
            return ExecutionPreflight(
                operator_id=self.operator_id,
                ok=False,
                message="No executable BetFlag depth for this selection.",
                market_open=False,
                market_version=version,
                raw=book,
            )
        acceptable: list[dict[str, Any]] = []
        for level in levels:
            odds = _decimal_odds(level.get("q"))
            if order.side == BetSide.BACK and odds >= order.limit_odds:
                acceptable.append(level)
            elif order.side == BetSide.LAY and odds <= order.limit_odds:
                acceptable.append(level)
        available = sum((Decimal(str(x.get("i", 0))) / Decimal("100") for x in acceptable), Decimal("0"))
        best = _decimal_odds(levels[0].get("q"))
        ok = available >= order.stake
        return ExecutionPreflight(
            operator_id=self.operator_id,
            ok=ok,
            message=("BetFlag price/depth validated." if ok else f"Insufficient depth: {available} < {order.stake}"),
            market_open=bool(levels),
            current_odds=best,
            available_size=available,
            market_version=version,
            raw=book,
        )

    def _payload(self, order: BetOrder) -> dict[str, Any]:
        stake_cents = _cents(order.stake)
        odds_x100 = int((order.limit_odds * 100).quantize(Decimal("1"), rounding=ROUND_DOWN))
        if stake_cents <= 0 or odds_x100 <= 100:
            raise BetFlagAPIError("Invalid stake or odds after BetFlag cent encoding")
        if order.side == BetSide.BACK:
            exposure = stake_cents
        else:
            exposure = int(
                (Decimal(stake_cents) * (Decimal(odds_x100) / Decimal("100") - Decimal("1")))
                .quantize(Decimal("1"), rounding=ROUND_DOWN)
            )
        return {
            "id_mercato": int(order.market_id),
            "versione_mercato": int(order.market_version or 0),
            "puntata": stake_cents,
            "ritira": 1,
            "mantieni": 0,
            "offerta": [
                {
                    "tipo": self._side_code(order.side),
                    "esito": int(order.selection_id),
                    "quota": odds_x100,
                    "importo": stake_cents,
                    "max_esposizione": exposure,
                }
            ],
        }

    def place_order(self, order: BetOrder, *, live: bool = False) -> ExecutionResult:
        if order.operator_id != self.operator_id:
            return ExecutionResult(
                operator_id=self.operator_id,
                status=ExecutionStatus.REJECTED,
                message=f"Order targets {order.operator_id}, not betflag",
            )
        if not live:
            return ExecutionResult(
                operator_id=self.operator_id,
                status=ExecutionStatus.DRY_RUN,
                message="BetFlag order validated in dry-run mode; no offer was sent.",
                requested_stake=order.stake,
                requested_odds=order.limit_odds,
                customer_order_ref=order.customer_order_ref,
            )
        if not self._live_enabled():
            return ExecutionResult(
                operator_id=self.operator_id,
                status=ExecutionStatus.REJECTED,
                message="Live execution is disabled. Set SPORTAGE_LIVE_EXECUTION=true explicitly.",
                requested_stake=order.stake,
                requested_odds=order.limit_odds,
                customer_order_ref=order.customer_order_ref,
            )
        payload = self._payload(order)
        response = self.client.request("POST", "/offers", json=payload, session=True)
        rows = response.get("ordine") or []
        if len(rows) != 1:
            return ExecutionResult(
                operator_id=self.operator_id,
                status=ExecutionStatus.UNKNOWN,
                message="BetFlag placement response did not contain exactly one order.",
                requested_stake=order.stake,
                requested_odds=order.limit_odds,
                customer_order_ref=order.customer_order_ref,
                raw=response,
            )
        row = rows[0]
        bet_id = str(row.get("offerta") or "") or None
        matched = Decimal(str(row.get("abbinato", 0))) / Decimal("100")
        requested = Decimal(str(row.get("importo", _cents(order.stake)))) / Decimal("100")
        remaining = max(Decimal("0"), requested - matched)
        if matched >= requested:
            return ExecutionResult(
                operator_id=self.operator_id,
                status=ExecutionStatus.ACCEPTED,
                message="BetFlag offer fully matched.",
                bet_id=bet_id,
                customer_order_ref=order.customer_order_ref,
                requested_stake=requested,
                requested_odds=order.limit_odds,
                matched_stake=matched,
                average_price_matched=order.limit_odds,
                remaining_stake=Decimal("0"),
                raw=response,
            )

        # BetFlag has no documented native FOK. Emulate immediate-or-cancel: remove
        # any unmatched remainder and report partial/zero fill honestly to the coordinator.
        if bet_id:
            try:
                self.cancel_order(bet_id, market_id=order.market_id, live=True)
            except Exception:
                pass
        status = ExecutionStatus.PARTIALLY_MATCHED if matched > 0 else ExecutionStatus.CANCELLED
        return ExecutionResult(
            operator_id=self.operator_id,
            status=status,
            message=(
                "BetFlag partially matched; unmatched remainder was cancelled."
                if matched > 0
                else "BetFlag did not match immediately; unmatched offer was cancelled."
            ),
            bet_id=bet_id,
            customer_order_ref=order.customer_order_ref,
            requested_stake=requested,
            requested_odds=order.limit_odds,
            matched_stake=matched,
            average_price_matched=order.limit_odds if matched > 0 else None,
            remaining_stake=remaining,
            raw=response,
        )

    def reconcile_order(
        self,
        *,
        bet_id: str | None = None,
        customer_order_ref: str | None = None,
        market_id: str | None = None,
        order: BetOrder | None = None,
    ) -> ExecutionResult:
        market = market_id or (order.market_id if order is not None else None)
        if not market:
            return ExecutionResult(
                operator_id=self.operator_id,
                status=ExecutionStatus.UNKNOWN,
                message="BetFlag reconciliation requires market_id or the original BetOrder.",
                bet_id=bet_id,
                customer_order_ref=customer_order_ref,
            )
        data = self.client.request("GET", f"/offers/all/{market}", session=True)
        rows = ((data.get("offerte") or {}).get("o") or []) if isinstance(data, dict) else []
        candidates = [row for row in rows if bet_id and str(row.get("id")) == str(bet_id)]
        if not candidates and order is not None:
            expected_type = self._side_code(order.side)
            expected_odds = int((order.limit_odds * 100).quantize(Decimal("1"), rounding=ROUND_DOWN))
            expected_stake = _cents(order.stake)
            candidates = [
                row for row in rows
                if int(row.get("tipo", -1)) == expected_type
                and str(row.get("esi")) == str(order.selection_id)
                and int(row.get("quo", -1)) == expected_odds
                and int(row.get("imp", -1)) == expected_stake
            ]
        if len(candidates) != 1:
            return ExecutionResult(
                operator_id=self.operator_id,
                status=ExecutionStatus.UNKNOWN,
                message=f"BetFlag reconciliation found {len(candidates)} matching offers; manual verification required.",
                bet_id=bet_id,
                customer_order_ref=customer_order_ref,
                raw=data,
            )
        row = candidates[0]
        requested = Decimal(str(row.get("imp", 0))) / Decimal("100")
        matched = Decimal(str(row.get("abb", 0))) / Decimal("100")
        remaining = max(Decimal("0"), requested - matched)
        if matched >= requested and requested > 0:
            status = ExecutionStatus.ACCEPTED
        elif matched > 0:
            status = ExecutionStatus.PARTIALLY_MATCHED
        elif int(row.get("st", 0)) in {1, 2}:
            status = ExecutionStatus.PENDING
        else:
            status = ExecutionStatus.CANCELLED
        return ExecutionResult(
            operator_id=self.operator_id,
            status=status,
            message="BetFlag order reconciled from account offers.",
            bet_id=str(row.get("id")),
            customer_order_ref=customer_order_ref,
            requested_stake=requested,
            requested_odds=_decimal_odds(row.get("quo")),
            matched_stake=matched,
            average_price_matched=_decimal_odds(row.get("quo")) if matched > 0 else None,
            remaining_stake=remaining,
            order_status=str(row.get("st")),
            raw=data,
        )

    def cancel_order(
        self,
        bet_id: str,
        *,
        market_id: str | None = None,
        live: bool = False,
    ) -> ExecutionResult:
        if not market_id:
            return ExecutionResult(
                operator_id=self.operator_id,
                status=ExecutionStatus.REJECTED,
                message="BetFlag cancellation requires market_id.",
                bet_id=bet_id,
            )
        if not live:
            return ExecutionResult(
                operator_id=self.operator_id,
                status=ExecutionStatus.DRY_RUN,
                message="BetFlag cancellation validated in dry-run mode.",
                bet_id=bet_id,
            )
        if not self._live_enabled():
            return ExecutionResult(
                operator_id=self.operator_id,
                status=ExecutionStatus.REJECTED,
                message="Live execution is disabled.",
                bet_id=bet_id,
            )
        response = self.client.request(
            "DELETE",
            "/offers",
            json={"id_mercato": int(market_id), "offerte": [{"id": bet_id}]},
            session=True,
        )
        return ExecutionResult(
            operator_id=self.operator_id,
            status=ExecutionStatus.CANCELLED,
            message="BetFlag unmatched remainder cancellation submitted.",
            bet_id=bet_id,
            raw=response,
        )
