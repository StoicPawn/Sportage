from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, ROUND_UP
from typing import Any

import httpx
from pydantic import BaseModel, Field

from .connectors.base import AccountSnapshot, BetOrder, required_account_funds
from .connectors.betflag import _BetFlagClient
from .costs import CostBook, net_return_factor
from .models import ArbitrageOpportunity, Quote
from .venue_certification import execution_environment


CENT = Decimal("0.01")
ACCOUNT_SCHEMA = """
CREATE TABLE IF NOT EXISTS account_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operator_id TEXT NOT NULL,
    environment TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    available_balance TEXT NOT NULL,
    total_balance TEXT,
    locked_balance TEXT,
    exposure TEXT,
    exposure_limit TEXT,
    retained_commission TEXT,
    raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_account_snapshots_operator
ON account_snapshots(operator_id, environment, observed_at DESC);
"""


class LiveFundingError(ValueError):
    pass


class FundingVenue(BaseModel):
    operator_id: str
    environment: str
    available_balance: Decimal
    planned_requirement: Decimal = Decimal("0")
    rescue_requirement: Decimal = Decimal("0")
    free_after_planned: Decimal = Decimal("0")


class FundingPlan(BaseModel):
    checked_at: datetime
    venues: list[FundingVenue]
    rescue_routes: dict[str, str] = Field(default_factory=dict)


class AccountSnapshotStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn.executescript(ACCOUNT_SCHEMA)
        self.conn.commit()

    def save(self, snapshot: AccountSnapshot) -> None:
        self.conn.execute(
            """INSERT INTO account_snapshots
            (operator_id, environment, observed_at, available_balance, total_balance,
             locked_balance, exposure, exposure_limit, retained_commission, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                snapshot.operator_id,
                snapshot.environment,
                snapshot.observed_at.isoformat(),
                str(snapshot.available_balance),
                None if snapshot.total_balance is None else str(snapshot.total_balance),
                None if snapshot.locked_balance is None else str(snapshot.locked_balance),
                None if snapshot.exposure is None else str(snapshot.exposure),
                None if snapshot.exposure_limit is None else str(snapshot.exposure_limit),
                None if snapshot.retained_commission is None else str(snapshot.retained_commission),
                json.dumps(snapshot.raw or {}, default=str),
            ),
        )
        self.conn.commit()

    def latest(self, operator_id: str, environment: str) -> AccountSnapshot | None:
        row = self.conn.execute(
            """SELECT * FROM account_snapshots
            WHERE operator_id=? AND environment=? ORDER BY id DESC LIMIT 1""",
            (operator_id, environment),
        ).fetchone()
        if row is None:
            return None
        return AccountSnapshot(
            operator_id=row["operator_id"],
            environment=row["environment"],
            observed_at=datetime.fromisoformat(row["observed_at"]),
            available_balance=Decimal(row["available_balance"]),
            total_balance=None if row["total_balance"] is None else Decimal(row["total_balance"]),
            locked_balance=None if row["locked_balance"] is None else Decimal(row["locked_balance"]),
            exposure=None if row["exposure"] is None else Decimal(row["exposure"]),
            exposure_limit=None if row["exposure_limit"] is None else Decimal(row["exposure_limit"]),
            retained_commission=(
                None if row["retained_commission"] is None else Decimal(row["retained_commission"])
            ),
            raw=json.loads(row["raw_json"]),
        )


def _betfair_account_snapshot() -> AccountSnapshot:
    app_key = os.getenv("BETFAIR_APP_KEY")
    session = os.getenv("BETFAIR_SESSION_TOKEN")
    if not app_key or not session:
        raise RuntimeError("BETFAIR_APP_KEY and BETFAIR_SESSION_TOKEN are required for account funds")
    payload = {
        "jsonrpc": "2.0",
        "method": "AccountAPING/v1.0/getAccountFunds",
        "params": {},
        "id": 1,
    }
    headers = {
        "X-Application": app_key,
        "X-Authentication": session,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    with httpx.Client(timeout=15.0) as client:
        response = client.post(
            "https://api.betfair.com/exchange/account/json-rpc/v1",
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
    if not isinstance(data, dict) or data.get("error") or not isinstance(data.get("result"), dict):
        raise RuntimeError(f"Unexpected Betfair getAccountFunds response: {data}")
    result = data["result"]
    available = Decimal(str(result.get("availableToBetBalance", 0) or 0))
    return AccountSnapshot(
        operator_id="betfair",
        environment="production",
        available_balance=max(Decimal("0"), available),
        exposure=None if result.get("exposure") is None else Decimal(str(result.get("exposure"))),
        exposure_limit=(
            None if result.get("exposureLimit") is None else Decimal(str(result.get("exposureLimit")))
        ),
        retained_commission=(
            None
            if result.get("retainedCommission") is None
            else Decimal(str(result.get("retainedCommission")))
        ),
        raw=data,
    )


def _betflag_account_snapshot() -> AccountSnapshot:
    environment = execution_environment("betflag")
    client = _BetFlagClient(environment=environment)
    data = client.request("GET", "/account/balance", session=True)
    if not isinstance(data, dict):
        raise RuntimeError("Unexpected BetFlag account/balance response")
    divisor = Decimal("100")
    available = Decimal(str(data.get("FreeBalance", 0) or 0)) / divisor
    total = Decimal(str(data.get("Balance", 0) or 0)) / divisor
    locked = Decimal(str(data.get("LockedBalance", 0) or 0)) / divisor
    return AccountSnapshot(
        operator_id="betflag",
        environment=environment,
        available_balance=max(Decimal("0"), available),
        total_balance=max(Decimal("0"), total),
        locked_balance=max(Decimal("0"), locked),
        exposure=-max(Decimal("0"), locked),
        raw=data,
    )


def fetch_account_snapshot(operator_id: str) -> AccountSnapshot:
    if operator_id == "betfair":
        return _betfair_account_snapshot()
    if operator_id == "betflag":
        return _betflag_account_snapshot()
    raise LiveFundingError(f"No official account-funds connector for {operator_id}")


def refresh_account_snapshot(
    operator_id: str,
    store: AccountSnapshotStore | None = None,
) -> AccountSnapshot:
    snapshot = fetch_account_snapshot(operator_id)
    if store is not None:
        store.save(snapshot)
    return snapshot


def _ceil_cent(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_UP)


def _rescue_buffer_pct() -> Decimal:
    try:
        value = Decimal(os.getenv("SPORTAGE_RESCUE_BALANCE_BUFFER_PCT", "0.10"))
    except Exception:
        value = Decimal("0.10")
    return max(Decimal("0"), min(value, Decimal("1")))


def assert_order_funded(order: BetOrder, snapshot: AccountSnapshot) -> Decimal:
    required = _ceil_cent(required_account_funds(order))
    if snapshot.available_balance < required:
        raise LiveFundingError(
            f"{order.operator_id} available balance {snapshot.available_balance:.2f} "
            f"is below order liability {required:.2f}"
        )
    return required


def assert_live_funding(
    opportunity: ArbitrageOpportunity,
    quotes: list[Quote],
    rescue_map: dict[str, list[str]],
    *,
    cost_book: CostBook,
    max_quote_age_seconds: float,
    max_rescue_slippage_bps: Decimal,
    snapshot_store: AccountSnapshotStore | None = None,
) -> FundingPlan:
    """Refresh balances and reserve enough cash for planned legs plus one venue failure.

    Rescue scenarios are evaluated independently because the design assumes a single
    automatic venue failure at a time. If an alternative venue is also used by the
    planned trade, its planned requirement is deducted before rescue reserve is tested.
    """
    operators = set(rescue_map)
    for alternatives in rescue_map.values():
        operators.update(alternatives)
    snapshots: dict[str, AccountSnapshot] = {}
    for operator_id in sorted(operators):
        try:
            snapshots[operator_id] = refresh_account_snapshot(operator_id, snapshot_store)
        except Exception as exc:
            raise LiveFundingError(
                f"Cannot refresh {operator_id} account funds: {type(exc).__name__}: {exc}"
            ) from exc

    planned: dict[str, Decimal] = {operator_id: Decimal("0") for operator_id in operators}
    for leg in opportunity.legs:
        if leg.operator_id in planned:
            # Current scanner execution legs are BACK orders. Explicit LAY orders are
            # checked again with exact liability immediately before placement.
            planned[leg.operator_id] += leg.stake

    for operator_id, required in planned.items():
        available = snapshots[operator_id].available_balance
        if available < required:
            raise LiveFundingError(
                f"{operator_id} needs {required:.2f} for planned legs but only {available:.2f} is free"
            )

    now = datetime.now(timezone.utc)
    buffer = Decimal("1") + _rescue_buffer_pct()
    stress_haircut = Decimal("1") - max_rescue_slippage_bps / Decimal("10000")
    selected_routes: dict[str, str] = {}
    rescue_requirement_by_venue: dict[str, Decimal] = {operator_id: Decimal("0") for operator_id in operators}

    for failed_operator, alternatives in rescue_map.items():
        failed_outcomes = {
            leg.outcome for leg in opportunity.legs if leg.operator_id == failed_operator
        }
        if not failed_outcomes:
            continue
        viable: list[tuple[Decimal, str, Decimal]] = []
        for alternative in alternatives:
            scenario_required = Decimal("0")
            valid = True
            for outcome in failed_outcomes:
                candidates = [
                    quote
                    for quote in quotes
                    if quote.event_id == opportunity.event_id
                    and quote.market_signature == opportunity.market_signature
                    and quote.operator_id == alternative
                    and quote.outcome == outcome
                    and quote.source_market_id
                    and quote.source_selection_id
                    and -5 <= (now - quote.observed_at).total_seconds() <= max_quote_age_seconds
                ]
                if not candidates:
                    valid = False
                    break
                quote = max(candidates, key=lambda item: item.odds)
                stressed_odds = max(Decimal("1.000001"), quote.odds * stress_haircut)
                factor = net_return_factor(stressed_odds, cost_book.for_bookmaker(quote.bookmaker))
                stake = _ceil_cent(opportunity.guaranteed_payout / factor)
                if quote.available_size is not None and quote.available_size < stake:
                    valid = False
                    break
                scenario_required += stake
            if not valid:
                continue
            reserve = _ceil_cent(scenario_required * buffer)
            free_after_planned = snapshots[alternative].available_balance - planned.get(alternative, Decimal("0"))
            if free_after_planned >= reserve:
                viable.append((free_after_planned - reserve, alternative, reserve))
        if not viable:
            raise LiveFundingError(
                f"No rescue venue has enough free balance/depth to replace {failed_operator} "
                f"with {_rescue_buffer_pct():.0%} balance buffer"
            )
        _, selected, reserve = max(viable, key=lambda item: item[0])
        selected_routes[failed_operator] = selected
        rescue_requirement_by_venue[selected] = max(rescue_requirement_by_venue[selected], reserve)

    venue_rows: list[FundingVenue] = []
    for operator_id in sorted(operators):
        available = snapshots[operator_id].available_balance
        planned_requirement = planned.get(operator_id, Decimal("0"))
        venue_rows.append(
            FundingVenue(
                operator_id=operator_id,
                environment=snapshots[operator_id].environment,
                available_balance=available,
                planned_requirement=planned_requirement,
                rescue_requirement=rescue_requirement_by_venue.get(operator_id, Decimal("0")),
                free_after_planned=available - planned_requirement,
            )
        )

    return FundingPlan(
        checked_at=datetime.now(timezone.utc),
        venues=venue_rows,
        rescue_routes=selected_routes,
    )
