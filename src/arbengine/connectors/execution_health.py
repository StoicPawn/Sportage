from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock


@dataclass(frozen=True)
class VenueHealth:
    operator_id: str
    healthy: bool
    disabled_until: datetime | None = None
    reason: str | None = None


_lock = RLock()
_disabled: dict[str, tuple[datetime, str]] = {}


def mark_unhealthy(operator_id: str, reason: str, *, cooldown_seconds: float = 60.0) -> None:
    until = datetime.now(timezone.utc) + timedelta(seconds=max(1.0, cooldown_seconds))
    with _lock:
        current = _disabled.get(operator_id)
        if current is None or current[0] < until:
            _disabled[operator_id] = (until, reason[:500])


def mark_healthy(operator_id: str) -> None:
    with _lock:
        _disabled.pop(operator_id, None)


def venue_health(operator_id: str, *, now: datetime | None = None) -> VenueHealth:
    now = now or datetime.now(timezone.utc)
    with _lock:
        state = _disabled.get(operator_id)
        if state is None:
            return VenueHealth(operator_id, True)
        until, reason = state
        if now >= until:
            _disabled.pop(operator_id, None)
            return VenueHealth(operator_id, True)
        return VenueHealth(operator_id, False, until, reason)


def execution_available(operator_id: str) -> bool:
    return venue_health(operator_id).healthy


def reset_all() -> None:
    with _lock:
        _disabled.clear()
