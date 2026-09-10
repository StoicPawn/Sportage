from arbengine.direct_venues import DIRECT_VENUES, ItalyPolicy, italian_automatic_venue_ids


def test_only_verified_adm_personal_order_apis_are_italian_automatic_targets():
    assert italian_automatic_venue_ids() == ("betfair", "betflag")


def test_foreign_or_partner_apis_are_never_silently_promoted():
    for venue_id in ("matchbook", "betdaq", "smarkets", "pinnacle", "ps3838", "cloudbet", "betconnect"):
        assert DIRECT_VENUES[venue_id].italy_policy != ItalyPolicy.ADM_ELIGIBLE
        assert not DIRECT_VENUES[venue_id].can_be_enabled_on_italian_acepc

    bwin = DIRECT_VENUES["bwin_partner"]
    assert not bwin.personal_account_api
    assert not bwin.order_api
    assert not bwin.can_be_enabled_on_italian_acepc


def test_brokers_are_not_counted_as_direct_personal_bookmaker_accounts():
    for venue_id in ("mollybet", "sportmarket"):
        assert not DIRECT_VENUES[venue_id].personal_account_api
        assert not DIRECT_VENUES[venue_id].can_be_enabled_on_italian_acepc
