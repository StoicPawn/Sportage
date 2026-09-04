from __future__ import annotations

import re
import unicodedata


_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def canonical_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    normalized = normalized.lower().strip()
    return _NON_ALNUM.sub(" ", normalized).strip()


def canonical_event_key(sport: str, home: str, away: str, commence_iso: str) -> str:
    return "|".join(
        [canonical_name(sport), canonical_name(home), canonical_name(away), commence_iso]
    )
