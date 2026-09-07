import pytest


@pytest.fixture(autouse=True)
def _legacy_tests_do_not_consume_live_canary(monkeypatch):
    """Existing execution tests use large fake stakes; dedicated canary tests opt back in."""
    monkeypatch.setenv("SPORTAGE_CANARY_MODE", "false")
