from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ItalyPolicy(str, Enum):
    ADM_ELIGIBLE = "adm_eligible"
    OPERATOR_BLOCKS_ITALY = "operator_blocks_italy"
    NOT_ADM_AUTHORISED = "not_adm_authorised"
    APPROVAL_ONLY_NOT_ADM = "approval_only_not_adm"
    BUSINESS_PARTNER_ONLY = "business_partner_only"
    UNVERIFIED = "unverified"


class VenueKind(str, Enum):
    EXCHANGE = "exchange"
    SPORTSBOOK = "sportsbook"
    BROKER = "broker"
    DATA_PARTNER = "data_partner"


@dataclass(frozen=True)
class DirectVenueSpec:
    venue_id: str
    display_name: str
    kind: VenueKind
    personal_account_api: bool
    market_data_api: bool
    order_api: bool
    self_service_api: bool
    italy_policy: ItalyPolicy
    adm_concession: str | None
    credential_env: tuple[str, ...]
    docs_url: str
    notes: str = ""

    @property
    def can_be_enabled_on_italian_acepc(self) -> bool:
        return self.italy_policy == ItalyPolicy.ADM_ELIGIBLE and self.personal_account_api


# Audited 2026-09-10 against operator/developer documentation. This is a
# capability and jurisdiction registry, not an endorsement. Only ADM_ELIGIBLE
# venues are allowed to become automatic execution venues on the Italian ACEPC.
DIRECT_VENUES: dict[str, DirectVenueSpec] = {
    "betfair": DirectVenueSpec(
        "betfair", "Betfair Italia Exchange", VenueKind.EXCHANGE,
        True, True, True, False, ItalyPolicy.ADM_ELIGIBLE, "16028",
        ("BETFAIR_APP_KEY", "BETFAIR_USERNAME", "BETFAIR_PASSWORD", "BETFAIR_CERT_FILE", "BETFAIR_KEY_FILE"),
        "https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687808",
        "Italian Exchange API-NG; personal betting Live App Key requires approval.",
    ),
    "betflag": DirectVenueSpec(
        "betflag", "BetFlag Exchange", VenueKind.EXCHANGE,
        True, True, True, False, ItalyPolicy.ADM_ELIGIBLE, "16008",
        ("BETFLAG_API_KEY",),
        "https://api-doc-exchange.mediasystemtechnologies.it/",
        "Official Exchange API 2.0.7; production API access/key must be obtained from BetFlag.",
    ),
    "matchbook": DirectVenueSpec(
        "matchbook", "Matchbook Exchange", VenueKind.EXCHANGE,
        True, True, True, True, ItalyPolicy.NOT_ADM_AUTHORISED, None,
        ("MATCHBOOK_USERNAME", "MATCHBOOK_PASSWORD"),
        "https://developers.matchbook.com/",
        "Retail API is available to registered customers, but Matchbook is not an ADM remote-gaming concessionaire.",
    ),
    "betdaq": DirectVenueSpec(
        "betdaq", "BETDAQ Exchange", VenueKind.EXCHANGE,
        True, True, True, False, ItalyPolicy.OPERATOR_BLOCKS_ITALY, None,
        ("BETDAQ_USERNAME", "BETDAQ_PASSWORD", "BETDAQ_APPLICATION_ID"),
        "https://api.betdaq.com/v2.0/Docs/Intro.aspx",
        "Customer API exists after approval; BETDAQ's accepted-country list does not include Italy.",
    ),
    "smarkets": DirectVenueSpec(
        "smarkets", "Smarkets", VenueKind.EXCHANGE,
        True, True, True, False, ItalyPolicy.OPERATOR_BLOCKS_ITALY, None,
        ("SMARKETS_API_KEY",),
        "https://help.smarkets.com/hc/en-gb/articles/34697834941085-Smarkets-API-Access-Integration-T-Cs",
        "Approved account/API can place bets, but Smarkets explicitly prohibits Italian residents/locations.",
    ),
    "pinnacle": DirectVenueSpec(
        "pinnacle", "Pinnacle", VenueKind.SPORTSBOOK,
        True, True, True, False, ItalyPolicy.APPROVAL_ONLY_NOT_ADM, None,
        ("PINNACLE_USERNAME", "PINNACLE_PASSWORD"),
        "https://github.com/pinnacleapi/pinnacleapi-documentation",
        "General-public API access closed in 2025; bespoke access is application-only and Pinnacle is not ADM-authorised.",
    ),
    "ps3838": DirectVenueSpec(
        "ps3838", "PS3838", VenueKind.SPORTSBOOK,
        True, True, True, False, ItalyPolicy.NOT_ADM_AUTHORISED, None,
        ("PS3838_USERNAME", "PS3838_PASSWORD"),
        "https://ps3838api.github.io/docs/",
        "Documented betting API, but not an ADM-authorised Italian remote-gaming operator.",
    ),
    "cloudbet": DirectVenueSpec(
        "cloudbet", "Cloudbet", VenueKind.SPORTSBOOK,
        True, True, True, True, ItalyPolicy.NOT_ADM_AUTHORISED, None,
        ("CLOUDBET_API_KEY",),
        "https://www.cloudbet.com/api/",
        "Player accounts can generate a Trading API key, but Cloudbet is not an ADM-authorised Italian operator.",
    ),
    "betconnect": DirectVenueSpec(
        "betconnect", "BetConnect", VenueKind.EXCHANGE,
        True, True, True, False, ItalyPolicy.NOT_ADM_AUTHORISED, None,
        ("BETCONNECT_API_KEY",),
        "https://developer.betconnect.com/",
        "Developer API exists; it is a UK betting intermediary and is not an ADM concessionaire.",
    ),
    "bwin_partner": DirectVenueSpec(
        "bwin_partner", "bwin Sports API", VenueKind.DATA_PARTNER,
        False, True, False, False, ItalyPolicy.BUSINESS_PARTNER_ONLY, "16013",
        ("BWIN_ACCESS_ID", "BWIN_TOKEN"),
        "https://beta.m.bwin.it/index.html",
        "Official Sports API is for business partners/website owners, not a bettor's personal execution account.",
    ),
    "mollybet": DirectVenueSpec(
        "mollybet", "MollyBet", VenueKind.BROKER,
        False, True, True, False, ItalyPolicy.UNVERIFIED, None,
        ("MOLLYBET_USERNAME", "MOLLYBET_PASSWORD"),
        "https://api.mollybet.com/docs/",
        "Execution/liquidity broker API; deliberately not counted as a direct personal bookmaker account.",
    ),
    "sportmarket": DirectVenueSpec(
        "sportmarket", "Sportmarket", VenueKind.BROKER,
        False, True, True, False, ItalyPolicy.UNVERIFIED, None,
        ("SPORTMARKET_USERNAME", "SPORTMARKET_PASSWORD"),
        "https://api.sportmarket.com/docs/",
        "Execution broker API; deliberately not counted as a direct personal bookmaker account.",
    ),
}


def italian_automatic_venue_ids() -> tuple[str, ...]:
    return tuple(sorted(k for k, v in DIRECT_VENUES.items() if v.can_be_enabled_on_italian_acepc and v.order_api))
