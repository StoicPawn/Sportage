from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .normalizer import canonical_name


class MarketDataAccess(str, Enum):
    OFFICIAL_PUBLIC_API = "official_public_api"
    OFFICIAL_PARTNER_API = "official_partner_api"
    AGGREGATOR = "aggregator"
    NONE = "none"


class ExecutionAccess(str, Enum):
    OFFICIAL_API = "official_api"
    MANUAL_ONLY = "manual_only"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class OperatorSpec:
    operator_id: str
    display_name: str
    tier: int
    adm_concession: str
    domains: tuple[str, ...]
    aliases: tuple[str, ...]
    market_data_access: MarketDataAccess
    execution_access: ExecutionAccess
    notes: str = ""


# Current Italy-focused Sportage universe. ADM concession codes were verified
# against the public remote-gambling concessionaire register on 2026-09-05.
OPERATORS: dict[str, OperatorSpec] = {
    "bet365": OperatorSpec(
        "bet365", "Bet365", 1, "16030", ("bet365.it",),
        ("bet365", "bet 365"), MarketDataAccess.AGGREGATOR, ExecutionAccess.MANUAL_ONLY,
        "No public retail bet-placement API is assumed.",
    ),
    "betfair": OperatorSpec(
        "betfair", "Betfair Exchange", 1, "16028", ("betfair.it",),
        ("betfair", "betfair exchange", "betfair exchange eu"),
        MarketDataAccess.OFFICIAL_PUBLIC_API, ExecutionAccess.OFFICIAL_API,
        "Official Exchange API-NG supports market data and placeOrders with an App Key/session token.",
    ),
    "snai": OperatorSpec(
        "snai", "SNAI", 1, "16032", ("snai.it",),
        ("snai", "snaitech"), MarketDataAccess.AGGREGATOR, ExecutionAccess.MANUAL_ONLY,
    ),
    "sisal": OperatorSpec(
        "sisal", "Sisal", 1, "16020", ("sisal.it",),
        ("sisal", "sisal matchpoint"), MarketDataAccess.AGGREGATOR, ExecutionAccess.MANUAL_ONLY,
    ),
    "eurobet": OperatorSpec(
        "eurobet", "Eurobet", 1, "16012", ("eurobet.it",),
        ("eurobet",), MarketDataAccess.AGGREGATOR, ExecutionAccess.MANUAL_ONLY,
    ),
    "goldbet": OperatorSpec(
        "goldbet", "Goldbet", 1, "16009", ("goldbet.it",),
        ("goldbet", "gold bet"), MarketDataAccess.AGGREGATOR, ExecutionAccess.MANUAL_ONLY,
    ),
    "lottomatica": OperatorSpec(
        "lottomatica", "Lottomatica", 1, "16010", ("lottomatica.it",),
        ("lottomatica", "better lottomatica"), MarketDataAccess.AGGREGATOR,
        ExecutionAccess.MANUAL_ONLY,
    ),
    "planetwin365": OperatorSpec(
        "planetwin365", "Planetwin365", 2, "16007", ("planetwin365.it",),
        ("planetwin365", "planet win 365", "planetwin 365"), MarketDataAccess.AGGREGATOR,
        ExecutionAccess.MANUAL_ONLY,
    ),
    "betsson": OperatorSpec(
        "betsson", "Betsson", 2, "16027", ("betsson.it",),
        ("betsson", "betsson it"), MarketDataAccess.AGGREGATOR, ExecutionAccess.MANUAL_ONLY,
    ),
    "codere": OperatorSpec(
        "codere", "Codere", 2, "16018", ("codere.it",),
        ("codere", "codere it"), MarketDataAccess.AGGREGATOR, ExecutionAccess.MANUAL_ONLY,
    ),
    "betflag": OperatorSpec(
        "betflag", "BetFlag", 2, "16008", ("betflag.it",),
        ("betflag", "bet flag", "betflag exchange"), MarketDataAccess.AGGREGATOR,
        ExecutionAccess.MANUAL_ONLY,
        "BetFlag offers an exchange product, but Sportage does not assume a public transactional API.",
    ),
    "bwin": OperatorSpec(
        "bwin", "bwin", 2, "16013", ("bwin.it",),
        ("bwin", "bwin it"), MarketDataAccess.OFFICIAL_PARTNER_API, ExecutionAccess.MANUAL_ONLY,
        "bwin publishes a partner Sports API; access requires business/legal approval and credentials.",
    ),
    "william_hill": OperatorSpec(
        "william_hill", "William Hill", 2, "16044", ("williamhill.it",),
        ("william hill", "williamhill"), MarketDataAccess.AGGREGATOR, ExecutionAccess.MANUAL_ONLY,
    ),
    "winamax": OperatorSpec(
        "winamax", "Winamax", 2, "16042", ("winamax.it",),
        ("winamax", "winamax it"), MarketDataAccess.AGGREGATOR, ExecutionAccess.MANUAL_ONLY,
    ),
}

_ALIAS_TO_ID: dict[str, str] = {}
for _operator_id, _spec in OPERATORS.items():
    for _alias in (_operator_id, _spec.display_name, *_spec.aliases, *_spec.domains):
        _ALIAS_TO_ID[canonical_name(_alias)] = _operator_id


def canonical_operator_id(value: str | None) -> str | None:
    if not value:
        return None
    key = canonical_name(value)
    if key in _ALIAS_TO_ID:
        return _ALIAS_TO_ID[key]
    # Feed titles often append geography/exchange qualifiers.
    for alias, operator_id in _ALIAS_TO_ID.items():
        if len(alias) >= 4 and (key.startswith(alias + " ") or key.endswith(" " + alias)):
            return operator_id
    return None


def operator_spec(value: str) -> OperatorSpec:
    operator_id = value if value in OPERATORS else canonical_operator_id(value)
    if operator_id is None:
        raise KeyError(f"Unknown Sportage operator: {value}")
    return OPERATORS[operator_id]


def operators_by_tier(*tiers: int) -> list[OperatorSpec]:
    wanted = set(tiers or (1, 2))
    return sorted(
        (spec for spec in OPERATORS.values() if spec.tier in wanted),
        key=lambda spec: (spec.tier, spec.display_name.lower()),
    )
