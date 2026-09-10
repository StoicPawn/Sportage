from pathlib import Path

from arbengine.providers.scheduler import load_scheduler_config
from arbengine.providers.the_odds_api import TheOddsAPIProvider
from arbengine.shadow import _coverage_target


def test_default_production_coverage_targets_ten_operators(monkeypatch):
    monkeypatch.delenv("SPORTAGE_TARGET_OPERATORS", raising=False)
    monkeypatch.delenv("SPORTAGE_MIN_OPERATOR_COVERAGE", raising=False)
    target, minimum = _coverage_target()
    assert minimum == 10
    assert len(target) == 10
    assert {"bet365", "betfair", "sisal", "snai"}.issubset(target)


def test_the_odds_api_reads_redundancy_settings_from_env(monkeypatch):
    monkeypatch.setenv("THE_ODDS_API_KEY", "test-key")
    monkeypatch.setenv("THE_ODDS_API_REGIONS", "eu,uk")
    monkeypatch.setenv("THE_ODDS_API_MARKETS", "h2h")
    provider = TheOddsAPIProvider()
    assert provider.regions == "eu,uk"
    assert provider.markets == "h2h"


def test_acepc_scheduler_reserves_free_aggregator_budget():
    cfg = load_scheduler_config(Path("config/provider_scheduler.acepc.json"))
    odds = cfg.sources["odds_api_io"]
    assert odds.enabled
    assert odds.daily_call_limit == 400
    assert odds.base_interval_seconds >= 180
    assert cfg.max_workers <= 2
