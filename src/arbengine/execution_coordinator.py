from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_UP
from enum import Enum
from typing import Iterable

from pydantic import BaseModel, Field

from .connectors.base import BetOrder, ExecutionResult, ExecutionStatus, TimeInForce
from .connectors.execution import build_execution_connector
from .costs import CostBook, net_return_factor
from .execution_storage import ExecutionStore
from .models import ArbitrageOpportunity, Quote
from .storage import SQLiteStore


CENT = Decimal("0.01")


def _ceil_cent(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_UP)


class RunStatus(str, Enum):
    PREPARED = "prepared"
    WAITING_MANUAL = "waiting_manual"
    EXECUTING = "executing"
    RESCUING = "rescuing"
    RESCUED = "rescued"
    COMPLETED = "completed"
    ABORTED = "aborted"
    EMERGENCY = "emergency"


class LegRole(str, Enum):
    PRIMARY = "primary"
    HEDGE = "hedge"
    RESCUE = "rescue"


class ExecutionPolicy(BaseModel):
    min_net_roi: Decimal = Field(default=Decimal("0.015"), ge=0)
    max_quote_age_seconds: float = Field(default=10.0, gt=0)
    max_rescue_loss: Decimal = Field(default=Decimal("5"), ge=0)
    max_rescue_slippage_bps: Decimal = Field(default=Decimal("100"), ge=0)
    require_rescue_venue: bool = True
    require_full_fill_exchange: bool = True
    max_reconcile_attempts: int = Field(default=2, ge=1, le=10)


class PlannedLeg(BaseModel):
    leg_id: str
    role: LegRole
    outcome: str
    bookmaker: str
    operator_id: str
    automatic: bool
    order: BetOrder


class ExecutionPlan(BaseModel):
    execution_id: str
    event_market_key: str
    event_id: str
    market_signature: str
    fingerprint: str
    outcomes: list[str]
    live: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    legs: list[PlannedLeg]


class CoordinatorResult(BaseModel):
    execution_id: str
    status: RunStatus
    message: str
    rescue_loss: Decimal | None = None


class ExecutionCoordinator:
    """Fail-closed coordinator for multi-venue arbitrage execution.

    It deliberately does not pretend that independent bookmakers support distributed
    transactions. Instead it serializes the difficult/manual leg first, uses strict
    exchange hedges, reconciles unknown outcomes before retrying, and routes any orphan
    exposure to a fresh automatic rescue venue. Unknown exposure or rescue-policy
    breach triggers a global execution halt.
    """

    def __init__(
        self,
        store: SQLiteStore,
        *,
        policy: ExecutionPolicy | None = None,
        cost_book: CostBook | None = None,
    ) -> None:
        self.store = store
        self.exec_store = ExecutionStore(store.conn)
        self.policy = policy or ExecutionPolicy()
        self.cost_book = cost_book or CostBook()

    @staticmethod
    def _connector_automatic(operator_id: str) -> bool:
        connector = build_execution_connector(operator_id)
        return bool(getattr(connector, "automatic_execution", False))

    def _native_order(self, execution_id: str, index: int, leg, automatic: bool) -> BetOrder:
        market_id = leg.source_market_id or f"manual:{execution_id}:{index}"
        selection_id = leg.source_selection_id or leg.outcome
        tif = TimeInForce.FILL_OR_KILL if automatic and self.policy.require_full_fill_exchange else TimeInForce.DEFAULT
        customer_order_ref = f"sp-{execution_id[-12:]}-{index}"[:32]
        return BetOrder(
            operator_id=leg.operator_id or "",
            market_id=market_id,
            selection_id=selection_id,
            stake=leg.stake,
            limit_odds=leg.odds,
            customer_ref=f"sp-{execution_id[-20:]}"[:32],
            customer_order_ref=customer_order_ref,
            event_id=None,
            outcome=leg.outcome,
            deep_link=leg.deep_link,
            time_in_force=tif,
            min_fill_size=leg.stake if tif == TimeInForce.FILL_OR_KILL else None,
            market_version=leg.source_market_version,
        )

    def _rescue_exists(self, opportunity: ArbitrageOpportunity, quotes: Iterable[Quote]) -> bool:
        now = datetime.now(timezone.utc)
        outcomes = {leg.outcome for leg in opportunity.legs}
        covered: set[str] = set()
        for quote in quotes:
            if quote.event_id != opportunity.event_id or quote.market_signature != opportunity.market_signature:
                continue
            if quote.outcome not in outcomes or not quote.operator_id:
                continue
            age = (now - quote.observed_at).total_seconds()
            if age < -5 or age > self.policy.max_quote_age_seconds:
                continue
            if not quote.source_market_id or not quote.source_selection_id:
                continue
            if self._connector_automatic(quote.operator_id):
                covered.add(quote.outcome)
        # A single accepted outcome needs all alternatives hedgeable. Requiring all
        # outcomes here also guarantees the rescue venue can protect whichever primary
        # leg becomes orphaned.
        return outcomes.issubset(covered)

    def prepare(
        self,
        opportunity: ArbitrageOpportunity,
        quotes: Iterable[Quote],
        *,
        live: bool = False,
    ) -> ExecutionPlan:
        halted, reason = self.exec_store.halt_state()
        if halted:
            raise RuntimeError(f"Execution is globally halted: {reason}")
        if opportunity.net_roi < self.policy.min_net_roi:
            raise ValueError(
                f"NET ROI {opportunity.net_roi:.3%} is below execution floor {self.policy.min_net_roi:.3%}"
            )
        if any(leg.quote_age_seconds > self.policy.max_quote_age_seconds for leg in opportunity.legs):
            raise ValueError("At least one arbitrage leg is too stale for execution")
        if self.policy.require_rescue_venue and not self._rescue_exists(opportunity, quotes):
            raise ValueError("No complete fresh automatic rescue venue is available for this market")

        execution_id = f"exe_{uuid.uuid4().hex}"
        self.exec_store.acquire_lock(opportunity.event_market_key, execution_id)
        try:
            staged: list[tuple[bool, object]] = []
            for leg in opportunity.legs:
                if not leg.operator_id:
                    raise ValueError(f"Leg {leg.outcome} has no canonical operator id")
                automatic = self._connector_automatic(leg.operator_id)
                if automatic and (not leg.source_market_id or not leg.source_selection_id):
                    raise ValueError(f"Automatic leg {leg.outcome} lacks native market/selection ids")
                staged.append((automatic, leg))

            # Difficult/manual venues first; automatic exchange legs are the hedge.
            staged.sort(key=lambda item: item[0])
            planned: list[PlannedLeg] = []
            for index, (automatic, leg) in enumerate(staged):
                role = LegRole.HEDGE if automatic and any(not a for a, _ in staged) else LegRole.PRIMARY
                order = self._native_order(execution_id, index, leg, automatic)
                planned.append(
                    PlannedLeg(
                        leg_id=f"L{index + 1}", role=role, outcome=leg.outcome,
                        bookmaker=leg.bookmaker, operator_id=leg.operator_id or "", automatic=automatic, order=order,
                    )
                )

            plan = ExecutionPlan(
                execution_id=execution_id,
                event_market_key=opportunity.event_market_key,
                event_id=opportunity.event_id,
                market_signature=opportunity.market_signature,
                fingerprint=opportunity.fingerprint,
                outcomes=[leg.outcome for leg in opportunity.legs],
                live=live,
                legs=planned,
            )
            initial = RunStatus.WAITING_MANUAL if any(not leg.automatic for leg in planned) else RunStatus.PREPARED
            self.exec_store.create_run(
                execution_id, opportunity.event_market_key, initial.value, live, plan.model_dump(mode="json")
            )
            for leg in planned:
                self.exec_store.save_leg(
                    execution_id, leg.leg_id, leg.role.value, leg.outcome, leg.operator_id,
                    ExecutionStatus.MANUAL_REQUIRED.value if not leg.automatic else "prepared",
                    leg.order.model_dump(mode="json"),
                )
            self.exec_store.event(execution_id, "PREPARED", {"status": initial.value})
            return plan
        except Exception:
            self.exec_store.release_lock(opportunity.event_market_key, execution_id)
            raise

    def _load_plan(self, execution_id: str) -> ExecutionPlan:
        row = self.exec_store.get_run(execution_id)
        if row is None:
            raise KeyError(f"Unknown execution {execution_id}")
        return ExecutionPlan.model_validate(json.loads(row["plan_json"]))

    def confirm_manual_leg(
        self,
        execution_id: str,
        leg_id: str,
        *,
        accepted: bool,
        matched_stake: Decimal | None = None,
        average_odds: Decimal | None = None,
        bet_id: str | None = None,
    ) -> CoordinatorResult:
        plan = self._load_plan(execution_id)
        leg = next((item for item in plan.legs if item.leg_id == leg_id), None)
        if leg is None or leg.automatic:
            raise ValueError("leg_id is not a manual execution leg")
        if not accepted:
            result = ExecutionResult(
                operator_id=leg.operator_id, status=ExecutionStatus.REJECTED,
                message="Manual leg reported rejected by operator.", requested_stake=leg.order.stake,
                requested_odds=leg.order.limit_odds,
            )
            self.exec_store.save_leg(
                execution_id, leg.leg_id, leg.role.value, leg.outcome, leg.operator_id,
                result.status.value, leg.order.model_dump(mode="json"), result.model_dump(mode="json"),
            )
            self.exec_store.update_run(execution_id, RunStatus.ABORTED.value, "Manual primary rejected")
            self.exec_store.event(execution_id, "MANUAL_REJECTED", {"leg_id": leg_id})
            self.exec_store.release_lock(plan.event_market_key, execution_id)
            return CoordinatorResult(execution_id=execution_id, status=RunStatus.ABORTED, message="No exposure opened.")

        matched = matched_stake or leg.order.stake
        odds = average_odds or leg.order.limit_odds
        status = ExecutionStatus.ACCEPTED if matched >= leg.order.stake else ExecutionStatus.PARTIALLY_MATCHED
        result = ExecutionResult(
            operator_id=leg.operator_id, status=status, message="Manual placement confirmed.", bet_id=bet_id,
            customer_order_ref=leg.order.customer_order_ref, requested_stake=leg.order.stake,
            requested_odds=leg.order.limit_odds, matched_stake=matched, average_price_matched=odds,
            remaining_stake=max(Decimal("0"), leg.order.stake - matched),
        )
        self.exec_store.save_leg(
            execution_id, leg.leg_id, leg.role.value, leg.outcome, leg.operator_id,
            status.value, leg.order.model_dump(mode="json"), result.model_dump(mode="json"),
        )
        self.exec_store.event(execution_id, "MANUAL_CONFIRMED", {"leg_id": leg_id, "matched": str(matched), "odds": str(odds)})
        if status == ExecutionStatus.PARTIALLY_MATCHED:
            self.exec_store.update_run(execution_id, RunStatus.RESCUING.value, "Manual leg partially accepted")
            return CoordinatorResult(execution_id=execution_id, status=RunStatus.RESCUING, message="Partial exposure requires rescue.")

        remaining_manual = [
            row for row in self.exec_store.get_legs(execution_id)
            if row["status"] == ExecutionStatus.MANUAL_REQUIRED.value
        ]
        next_status = RunStatus.WAITING_MANUAL if remaining_manual else RunStatus.PREPARED
        self.exec_store.update_run(execution_id, next_status.value)
        return CoordinatorResult(execution_id=execution_id, status=next_status, message="Manual leg recorded.")

    def _safe_submit(self, leg: PlannedLeg, *, live: bool) -> ExecutionResult:
        connector = build_execution_connector(leg.operator_id)
        preflight = connector.preflight(leg.order)
        if not preflight.ok:
            return ExecutionResult(
                operator_id=leg.operator_id, status=ExecutionStatus.REJECTED,
                message=f"Preflight failed: {preflight.message}", requested_stake=leg.order.stake,
                requested_odds=leg.order.limit_odds, customer_order_ref=leg.order.customer_order_ref,
            )
        order = leg.order.model_copy(
            update={"market_version": preflight.market_version or leg.order.market_version}
        )
        try:
            result = connector.place_order(order, live=live)
        except Exception as exc:
            # Never retry a placement blindly. Reconcile by persistent order reference first.
            result = ExecutionResult(
                operator_id=leg.operator_id, status=ExecutionStatus.UNKNOWN,
                message=f"Placement raised {type(exc).__name__}; reconciling before any retry.",
                customer_order_ref=order.customer_order_ref, requested_stake=order.stake,
                requested_odds=order.limit_odds,
            )
            for _ in range(self.policy.max_reconcile_attempts):
                reconciled = connector.reconcile_order(customer_order_ref=order.customer_order_ref)
                if reconciled.status != ExecutionStatus.UNKNOWN:
                    result = reconciled
                    break
        if result.status == ExecutionStatus.PENDING and result.bet_id:
            connector.cancel_order(result.bet_id, market_id=order.market_id, live=live)
            result = connector.reconcile_order(bet_id=result.bet_id, customer_order_ref=order.customer_order_ref)
        if result.status == ExecutionStatus.PARTIALLY_MATCHED and result.bet_id:
            connector.cancel_order(result.bet_id, market_id=order.market_id, live=live)
        return result

    def resume(self, execution_id: str, fresh_quotes: Iterable[Quote], *, live: bool | None = None) -> CoordinatorResult:
        plan = self._load_plan(execution_id)
        row = self.exec_store.get_run(execution_id)
        assert row is not None
        halted, reason = self.exec_store.halt_state()
        if halted:
            return CoordinatorResult(execution_id=execution_id, status=RunStatus.EMERGENCY, message=f"Global halt: {reason}")
        if row["status"] == RunStatus.WAITING_MANUAL.value:
            return CoordinatorResult(execution_id=execution_id, status=RunStatus.WAITING_MANUAL, message="Manual primary confirmation required.")
        if row["status"] in {RunStatus.COMPLETED.value, RunStatus.RESCUED.value, RunStatus.ABORTED.value}:
            return CoordinatorResult(execution_id=execution_id, status=RunStatus(row["status"]), message="Execution is already terminal.")

        use_live = plan.live if live is None else live
        self.exec_store.update_run(execution_id, RunStatus.EXECUTING.value)
        for leg in plan.legs:
            stored = next(r for r in self.exec_store.get_legs(execution_id) if r["leg_id"] == leg.leg_id)
            if stored["status"] == ExecutionStatus.ACCEPTED.value:
                continue
            if not leg.automatic:
                continue
            result = self._safe_submit(leg, live=use_live)
            self.exec_store.save_leg(
                execution_id, leg.leg_id, leg.role.value, leg.outcome, leg.operator_id,
                result.status.value, leg.order.model_dump(mode="json"), result.model_dump(mode="json"),
            )
            self.exec_store.event(execution_id, "ORDER_RESULT", {"leg_id": leg.leg_id, "status": result.status.value})
            if result.status == ExecutionStatus.UNKNOWN:
                return self._emergency(plan, "Order state UNKNOWN after reconciliation; duplicate/exposure risk")
            if result.status != ExecutionStatus.ACCEPTED or not result.fully_matched:
                return self._rescue(plan, list(fresh_quotes), use_live, f"Leg {leg.leg_id} not fully matched")

        accepted = [r for r in self.exec_store.get_legs(execution_id) if r["status"] == ExecutionStatus.ACCEPTED.value]
        if len(accepted) == len(plan.legs):
            self.exec_store.update_run(execution_id, RunStatus.COMPLETED.value)
            self.exec_store.event(execution_id, "FULLY_HEDGED")
            self.exec_store.release_lock(plan.event_market_key, execution_id)
            return CoordinatorResult(execution_id=execution_id, status=RunStatus.COMPLETED, message="All legs fully matched.")
        return self._rescue(plan, list(fresh_quotes), use_live, "Execution ended with orphan exposure")

    def _accepted_exposure(self, plan: ExecutionPlan) -> tuple[dict[str, Decimal], Decimal]:
        returns = {outcome: Decimal("0") for outcome in plan.outcomes}
        outlay = Decimal("0")
        leg_by_id = {leg.leg_id: leg for leg in plan.legs}
        for row in self.exec_store.get_legs(plan.execution_id):
            if not row["result_json"]:
                continue
            result = ExecutionResult.model_validate(json.loads(row["result_json"]))
            matched = result.matched_stake or Decimal("0")
            if matched <= 0:
                continue
            leg = leg_by_id[row["leg_id"]]
            odds = result.average_price_matched or leg.order.limit_odds
            profile = self.cost_book.for_bookmaker(leg.bookmaker)
            returns[leg.outcome] += matched * net_return_factor(odds, profile)
            outlay += matched * (Decimal("1") + profile.stake_fee_pct) + profile.fixed_cost_per_bet
        return returns, outlay

    def _rescue(self, plan: ExecutionPlan, quotes: list[Quote], live: bool, reason: str) -> CoordinatorResult:
        self.exec_store.update_run(plan.execution_id, RunStatus.RESCUING.value, reason)
        self.exec_store.event(plan.execution_id, "ORPHAN_DETECTED", {"reason": reason})
        base_returns, existing_outlay = self._accepted_exposure(plan)
        positive = [value for value in base_returns.values() if value > 0]
        if not positive:
            self.exec_store.update_run(plan.execution_id, RunStatus.ABORTED.value, "No matched exposure after failed hedge")
            self.exec_store.release_lock(plan.event_market_key, plan.execution_id)
            return CoordinatorResult(plan.execution_id, RunStatus.ABORTED, "No matched exposure remained.")
        target_return = min(positive)
        now = datetime.now(timezone.utc)
        original_odds = {leg.outcome: leg.order.limit_odds for leg in plan.legs}
        rescue_legs: list[PlannedLeg] = []
        rescue_outlay = Decimal("0")

        for outcome in plan.outcomes:
            needed = target_return - base_returns[outcome]
            if needed <= 0:
                continue
            candidates: list[Quote] = []
            for quote in quotes:
                if quote.event_id != plan.event_id or quote.market_signature != plan.market_signature:
                    continue
                if quote.outcome != outcome or not quote.operator_id:
                    continue
                age = (now - quote.observed_at).total_seconds()
                if age < -5 or age > self.policy.max_quote_age_seconds:
                    continue
                if not quote.source_market_id or not quote.source_selection_id:
                    continue
                if not self._connector_automatic(quote.operator_id):
                    continue
                reference = original_odds.get(outcome, quote.odds)
                slippage_bps = max(Decimal("0"), (reference - quote.odds) / reference * Decimal("10000"))
                if slippage_bps > self.policy.max_rescue_slippage_bps:
                    continue
                candidates.append(quote)
            if not candidates:
                return self._emergency(plan, f"No rescue quote within policy for outcome {outcome}")
            quote = max(candidates, key=lambda q: q.odds)
            profile = self.cost_book.for_bookmaker(quote.bookmaker)
            factor = net_return_factor(quote.odds, profile)
            stake = _ceil_cent(needed / factor)
            if quote.available_size is not None and quote.available_size < stake:
                return self._emergency(plan, f"Insufficient rescue depth for {outcome}: need {stake}, have {quote.available_size}")
            cash = stake * (Decimal("1") + profile.stake_fee_pct) + profile.fixed_cost_per_bet
            rescue_outlay += cash
            order = BetOrder(
                operator_id=quote.operator_id,
                market_id=quote.source_market_id,
                selection_id=quote.source_selection_id,
                stake=stake,
                limit_odds=quote.odds,
                customer_ref=f"rs-{plan.execution_id[-20:]}"[:32],
                customer_order_ref=f"rs-{plan.execution_id[-12:]}-{len(rescue_legs)}"[:32],
                event_id=plan.event_id,
                outcome=outcome,
                time_in_force=TimeInForce.FILL_OR_KILL,
                min_fill_size=stake,
                market_version=quote.source_market_version,
            )
            rescue_legs.append(
                PlannedLeg(
                    leg_id=f"R{len(rescue_legs) + 1}", role=LegRole.RESCUE, outcome=outcome,
                    bookmaker=quote.bookmaker, operator_id=quote.operator_id, automatic=True, order=order,
                )
            )

        projected_profit = target_return - existing_outlay - rescue_outlay
        rescue_loss = max(Decimal("0"), -projected_profit)
        if rescue_loss > self.policy.max_rescue_loss:
            return self._emergency(
                plan, f"Projected rescue loss {rescue_loss:.2f} exceeds limit {self.policy.max_rescue_loss:.2f}"
            )
        if len(rescue_legs) > 1:
            # Multi-outcome rescue would itself create sequencing risk unless a connector
            # provides a genuinely atomic batch primitive. Do not compound the orphan.
            return self._emergency(plan, "Multi-leg rescue requires atomic batch execution; intervention required")

        for rescue in rescue_legs:
            result = self._safe_submit(rescue, live=live)
            self.exec_store.save_leg(
                plan.execution_id, rescue.leg_id, rescue.role.value, rescue.outcome, rescue.operator_id,
                result.status.value, rescue.order.model_dump(mode="json"), result.model_dump(mode="json"),
            )
            self.exec_store.event(plan.execution_id, "RESCUE_RESULT", {"leg_id": rescue.leg_id, "status": result.status.value})
            if result.status != ExecutionStatus.ACCEPTED or not result.fully_matched:
                return self._emergency(plan, f"Rescue order {rescue.leg_id} failed to fully match")

        self.exec_store.update_run(plan.execution_id, RunStatus.RESCUED.value, f"Rescued; projected floor {projected_profit:.2f}")
        self.exec_store.event(plan.execution_id, "RESCUED", {"projected_profit": str(projected_profit), "rescue_loss": str(rescue_loss)})
        self.exec_store.release_lock(plan.event_market_key, plan.execution_id)
        return CoordinatorResult(plan.execution_id, RunStatus.RESCUED, "Orphan exposure hedged within policy.", rescue_loss=rescue_loss)

    def _emergency(self, plan: ExecutionPlan, reason: str) -> CoordinatorResult:
        self.exec_store.update_run(plan.execution_id, RunStatus.EMERGENCY.value, reason)
        self.exec_store.event(plan.execution_id, "EMERGENCY", {"reason": reason})
        self.exec_store.set_halt(f"{plan.execution_id}: {reason}")
        # Keep the event lock: unresolved exposure must be consciously reconciled before reset.
        return CoordinatorResult(plan.execution_id, RunStatus.EMERGENCY, reason)
