from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from .execution_storage import ExecutionStore


class AbortSafetyError(RuntimeError):
    pass


@dataclass(frozen=True)
class AbortResult:
    execution_id: str
    aborted: bool
    message: str


_ABORTABLE_RUN_STATUSES = {"waiting_manual", "prepared"}
_TERMINAL_RUN_STATUSES = {"completed", "rescued", "aborted"}
_UNSAFE_LEG_STATUSES = {"accepted", "partially_matched", "pending", "unknown"}
_SAFE_BET_ID_STATUSES = {"rejected", "cancelled"}


def _matched_stake(result: dict) -> Decimal:
    raw = result.get("matched_stake")
    if raw in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise AbortSafetyError("Cannot prove zero exposure: invalid matched_stake in execution ledger") from exc


def abort_if_flat(conn: sqlite3.Connection, execution_id: str, *, reason: str = "Operator aborted before exposure") -> AbortResult:
    """Abort only when the durable ledger proves no order can be live or matched.

    The inspection, run transition and lock release are one IMMEDIATE SQLite
    transaction. This prevents another local process from changing execution state
    between the zero-exposure check and the abort commit.
    """

    ExecutionStore(conn)  # Ensure schema before opening the explicit transaction.
    try:
        conn.execute("BEGIN IMMEDIATE")
        run = conn.execute(
            "SELECT execution_id, event_market_key, status FROM execution_runs WHERE execution_id=?",
            (execution_id,),
        ).fetchone()
        if run is None:
            raise AbortSafetyError(f"Unknown execution id: {execution_id}")

        status = str(run["status"])
        if status == "aborted":
            conn.rollback()
            return AbortResult(execution_id, False, "Execution is already aborted.")
        if status in _TERMINAL_RUN_STATUSES:
            raise AbortSafetyError(f"Execution is already terminal with status={status}; abort is not applicable")
        if status not in _ABORTABLE_RUN_STATUSES:
            raise AbortSafetyError(
                f"Cannot prove zero exposure while run status={status}. Only waiting_manual/prepared may be aborted."
            )

        legs = list(
            conn.execute(
                "SELECT leg_id, status, result_json FROM execution_legs WHERE execution_id=? ORDER BY leg_id",
                (execution_id,),
            )
        )
        if not legs:
            raise AbortSafetyError("Cannot prove zero exposure: execution has no durable leg ledger")

        for leg in legs:
            leg_status = str(leg["status"] or "").lower()
            if leg_status in _UNSAFE_LEG_STATUSES:
                raise AbortSafetyError(
                    f"Cannot abort: leg {leg['leg_id']} has exposure-sensitive status={leg_status}"
                )
            if not leg["result_json"]:
                continue
            try:
                result = json.loads(leg["result_json"])
            except Exception as exc:
                raise AbortSafetyError(
                    f"Cannot prove zero exposure: invalid result ledger for leg {leg['leg_id']}"
                ) from exc
            matched = _matched_stake(result)
            if matched > 0:
                raise AbortSafetyError(
                    f"Cannot abort: leg {leg['leg_id']} has matched exposure {matched}"
                )
            bet_id = result.get("bet_id")
            if bet_id and leg_status not in _SAFE_BET_ID_STATUSES:
                raise AbortSafetyError(
                    f"Cannot abort: leg {leg['leg_id']} has operator bet_id={bet_id} without a proven rejected/cancelled state"
                )

        lock = conn.execute(
            "SELECT execution_id FROM execution_locks WHERE event_market_key=?",
            (run["event_market_key"],),
        ).fetchone()
        if lock is not None and str(lock["execution_id"]) != execution_id:
            raise AbortSafetyError(
                f"Execution lock is held by {lock['execution_id']}, not {execution_id}; refusing inconsistent abort"
            )

        now = datetime.now(timezone.utc).isoformat()
        safe_reason = reason.strip()[:2000] or "Operator aborted before exposure"
        conn.execute(
            "UPDATE execution_runs SET status='aborted', reason=?, updated_at=? WHERE execution_id=?",
            (safe_reason, now, execution_id),
        )
        conn.execute(
            "DELETE FROM execution_locks WHERE event_market_key=? AND execution_id=?",
            (run["event_market_key"], execution_id),
        )
        conn.execute(
            "INSERT INTO execution_events(execution_id, created_at, event_type, payload_json) VALUES (?, ?, ?, ?)",
            (
                execution_id,
                now,
                "ABORTED_ZERO_EXPOSURE",
                json.dumps({"reason": safe_reason, "verified_legs": len(legs)}),
            ),
        )
        conn.commit()
        return AbortResult(execution_id, True, "Execution aborted with durable zero-exposure proof; lock released.")
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
