import pytest


@pytest.fixture(autouse=True)
def _legacy_tests_do_not_consume_live_safety_gates(monkeypatch):
    """Dedicated safety tests opt back in; historical execution tests keep fake live flows isolated."""
    monkeypatch.setenv("SPORTAGE_CANARY_MODE", "false")
    monkeypatch.setenv("SPORTAGE_REQUIRE_COMMISSIONING", "false")
