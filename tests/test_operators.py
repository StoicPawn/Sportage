from arbengine.operators import (
    ExecutionAccess,
    MarketDataAccess,
    canonical_operator_id,
    operators_by_tier,
)


def test_tier_one_and_two_registry_is_complete():
    specs = operators_by_tier(1, 2)
    ids = {spec.operator_id for spec in specs}
    assert len(specs) == 14
    assert ids == {
        "bet365", "betfair", "snai", "sisal", "eurobet", "goldbet", "lottomatica",
        "planetwin365", "betsson", "codere", "betflag", "bwin", "william_hill", "winamax",
    }
    assert all(spec.adm_concession.startswith("160") for spec in specs)


def test_operator_aliases_are_canonicalized():
    assert canonical_operator_id("Betfair Exchange EU") == "betfair"
    assert canonical_operator_id("BetFlag Exchange") == "betflag"
    assert canonical_operator_id("Sisal Matchpoint") == "sisal"
    assert canonical_operator_id("WilliamHill") == "william_hill"
    assert canonical_operator_id("Planet Win 365") == "planetwin365"


def test_verified_public_transactional_apis_are_betfair_and_betflag():
    by_id = {spec.operator_id: spec for spec in operators_by_tier(1, 2)}
    for operator_id in ("betfair", "betflag"):
        assert by_id[operator_id].market_data_access == MarketDataAccess.OFFICIAL_PUBLIC_API
        assert by_id[operator_id].execution_access == ExecutionAccess.OFFICIAL_API
    assert by_id["bwin"].market_data_access == MarketDataAccess.OFFICIAL_PARTNER_API
    assert all(
        spec.execution_access == ExecutionAccess.MANUAL_ONLY
        for operator_id, spec in by_id.items()
        if operator_id not in {"betfair", "betflag"}
    )
