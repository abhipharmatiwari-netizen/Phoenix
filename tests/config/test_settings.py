import os
import importlib

import pytest

SETTINGS_PATH = "app.config.settings"


def _reset_settings_cache():
    mod = importlib.import_module(SETTINGS_PATH)
    if hasattr(mod, "get_settings"):
        try:
            mod.get_settings.cache_clear()
        except AttributeError:
            pass


@pytest.fixture(autouse=True)
def reset_settings_between_tests(monkeypatch):
    # Set default profit sweep environment variables
    monkeypatch.setenv("PROFIT_SWEEP_ENABLED", "false")
    monkeypatch.setenv("PROFIT_SWEEP_STRATEGY", "SIMPLE")
    monkeypatch.setenv("PROFIT_SIMPLE_ENABLED", "false")
    monkeypatch.setenv("PROFIT_DAILY_TARGET", "2000")
    monkeypatch.setenv("PROFIT_DAILY_TARGET_PAPER", "2000")
    monkeypatch.setenv("PROFIT_MULTIPLE_ENABLED", "false")
    monkeypatch.setenv("PROFIT_MULTIPLE_TARGET", "500")
    monkeypatch.setenv("PROFIT_SWEEP_COOLDOWN_MINUTES", "30")
    monkeypatch.setenv("PROFIT_MAX_SWEEPS_PER_DAY", "5")
    
    _reset_settings_cache()
    yield
    _reset_settings_cache()


def test_get_settings_returns_singleton(monkeypatch):
    monkeypatch.delenv("GCP_PROJECT_ID", raising=False)
    import app.config.settings as settings_mod

    s1 = settings_mod.get_settings()
    s2 = settings_mod.get_settings()
    assert s1 is s2, "get_settings should be cached and return the same object"


def test_env_overrides_project_and_dataset(monkeypatch):
    monkeypatch.setenv("GCP_PROJECT_ID", "proj-from-env")
    monkeypatch.setenv("BQ_DATASET_ID", "ds-from-env")
    import app.config.settings as settings_mod

    s = settings_mod.get_settings()
    assert s.gcp_project_id == "proj-from-env"
    assert s.bq_dataset_id == "ds-from-env"


@pytest.mark.parametrize("env_value, expected", [("1", True), ("true", True), ("0", False), ("false", False)])
def test_boolean_flags_from_env(monkeypatch, env_value, expected):
    flags = [
        "USE_HUB_ROUTER_FOR_NG_OPTIONS",
        "USE_HUB_ROUTER_FOR_NIFTY_OPTIONS",
        "ENABLE_RISK_CHECKS",
        "ENABLE_CAPITAL_CHECKS",
        "RISK_CAPITAL_AUTO_RESIZE_ENABLED",
        "RISK_FAIL_OPEN_ON_MISSING_PNL",
        "RUNTIME_CONFIG_ENABLED",
    ]

    for flag in flags:
        monkeypatch.setenv(flag, env_value)

    import app.config.settings as settings_mod
    s = settings_mod.get_settings()

    assert bool(s.use_hub_router_for_ng_options) == expected
    assert bool(s.use_hub_router_for_nifty_options) == expected
    assert bool(s.enable_risk_checks) == expected
    assert bool(s.enable_capital_checks) == expected
    assert bool(s.capital_auto_resize_enabled) == expected
    assert bool(s.risk_fail_open_on_missing_pnl) == expected
    assert bool(s.runtime_config_enabled) == expected


def test_domain_settings_views(monkeypatch):
    monkeypatch.setenv("GCP_PROJECT_ID", "proj-123")
    monkeypatch.setenv("BQ_DATASET_ID", "dataset-123")
    monkeypatch.setenv("DEFAULT_TIME_ZONE", "UTC")
    monkeypatch.setenv("ENABLE_CAPITAL_CHECKS", "true")
    monkeypatch.setenv("ENABLE_RISK_CHECKS", "true")
    monkeypatch.setenv("RISK_ENABLE_DAILY_LOSS", "true")
    monkeypatch.setenv("RISK_MAX_DAILY_LOSS", "1000")
    monkeypatch.setenv("USE_HUB_ROUTER", "true")
    monkeypatch.setenv("USE_HUB_ROUTER_FOR_NG_OPTIONS", "true")
    import app.config.settings as settings_mod

    s = settings_mod.get_settings()

    db = s.database_settings
    infra = s.infrastructure_settings
    risk = s.risk_guard_settings
    features = s.strategy_feature_flags

    assert db.gcp_project_id == "proj-123"
    assert db.bq_dataset_id == "dataset-123"
    assert infra.default_time_zone == "UTC"
    assert risk.enable_capital_checks is True
    assert risk.risk_enable_daily_loss is True
    assert risk.risk_max_daily_loss == 1000.0
    assert features.use_hub_router is True
    assert features.use_hub_router_for_ng_options is True
