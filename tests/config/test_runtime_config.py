import time
from types import SimpleNamespace

import pytest


def _reset_settings_cache():
    import app.config.settings as settings_mod

    settings_mod.get_settings.cache_clear()


def test_runtime_settings_passthrough_for_non_settings_base():
    from app.config.runtime_config import get_runtime_settings

    base = SimpleNamespace(enable_risk_checks=True)
    resolved = get_runtime_settings(base_settings=base)
    assert resolved is base


def test_runtime_settings_applies_overrides(monkeypatch):
    monkeypatch.setenv("PROFIT_SWEEP_STRATEGY", "SIMPLE")
    monkeypatch.setenv("PROFIT_DAILY_TARGET", "2000")
    _reset_settings_cache()

    import app.config.settings as settings_mod
    import app.config.runtime_config as runtime_mod

    base = settings_mod.get_settings()
    snapshot = runtime_mod._RuntimeOverrideSnapshot(
        global_overrides={
            "enable_risk_checks": False,
            "auto_strategy_select_enabled": True,
            "auto_strategy_max_active_per_underlying": 2,
        },
        account_overrides={"tenant-1:acc-1": {"enable_profit_checks": True}},
        strategy_overrides={"strategy-1": {"risk_max_daily_loss": 1234.0}},
        fetched_at_epoch=time.time(),
    )
    monkeypatch.setattr(runtime_mod._provider, "_get_snapshot", lambda _s: snapshot)

    resolved = runtime_mod.get_runtime_settings(
        base_settings=base,
        tenant_id="tenant-1",
        broker_account_id="acc-1",
        strategy_id="strategy-1",
    )

    assert resolved.enable_risk_checks is False
    assert resolved.auto_strategy_select_enabled is True
    assert resolved.auto_strategy_max_active_per_underlying == 2
    assert resolved.enable_profit_checks is True
    assert resolved.risk_max_daily_loss == 1234.0


def test_runtime_config_auto_enable_ignores_docker_desktop_service(monkeypatch):
    monkeypatch.delenv("RUNTIME_CONFIG_AUTO_ENABLE", raising=False)
    monkeypatch.setenv("K_SERVICE", "docker-desktop")
    monkeypatch.delenv("K_REVISION", raising=False)
    monkeypatch.delenv("K_CONFIGURATION", raising=False)
    _reset_settings_cache()

    import app.config.runtime_config as runtime_mod
    import app.config.settings as settings_mod

    settings = settings_mod.get_settings()
    assert runtime_mod.RuntimeConfigProvider._is_enabled(settings) is False


def test_runtime_config_auto_enable_allows_cloud_run_markers(monkeypatch):
    monkeypatch.delenv("RUNTIME_CONFIG_AUTO_ENABLE", raising=False)
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.setenv("K_REVISION", "svc-00001-abc")
    monkeypatch.delenv("K_CONFIGURATION", raising=False)
    _reset_settings_cache()

    import app.config.runtime_config as runtime_mod
    import app.config.settings as settings_mod

    settings = settings_mod.get_settings()
    assert runtime_mod.RuntimeConfigProvider._is_enabled(settings) is True


def test_live_override_allows_upward_cap_raise_without_break_glass(monkeypatch):
    """PR #265 round-9 (Codex round-8 P2 "Allow safe cap raises from
    runtime config"): a runtime override that raises
    ``auto_strategy_max_active_per_underlying`` above the base value is
    strictly safer (the selection can only include more strategies,
    never truncate any), so it must be allowed in LIVE without
    break-glass. Downward overrides must still be rejected because they
    re-introduce the truncation risk the round-5 protection was added
    to prevent.
    """
    monkeypatch.setenv("TRADE_MODE", "LIVE")
    monkeypatch.setenv("PROFIT_SWEEP_STRATEGY", "SIMPLE")
    monkeypatch.setenv("PROFIT_DAILY_TARGET", "2000")
    monkeypatch.delenv("LIVE_RUNTIME_OVERRIDE_BREAK_GLASS", raising=False)
    monkeypatch.delenv(
        "LIVE_RUNTIME_OVERRIDE_BREAK_GLASS_APPROVED_BY", raising=False
    )
    monkeypatch.delenv(
        "LIVE_RUNTIME_OVERRIDE_BREAK_GLASS_UNTIL", raising=False
    )
    # Pin base cap to legacy 2 so the upward override (3) is a strict
    # increase regardless of the committed Settings default.
    monkeypatch.setenv("AUTO_STRATEGY_MAX_ACTIVE_PER_UNDERLYING", "2")
    _reset_settings_cache()

    import app.config.settings as settings_mod
    import app.config.runtime_config as runtime_mod

    base = settings_mod.get_settings()
    snapshot_up = runtime_mod._RuntimeOverrideSnapshot(
        global_overrides={"auto_strategy_max_active_per_underlying": 3},
        account_overrides={},
        strategy_overrides={},
        fetched_at_epoch=time.time(),
    )
    monkeypatch.setattr(
        runtime_mod._provider, "_get_snapshot", lambda _s: snapshot_up
    )
    monkeypatch.setattr(runtime_mod, "emit_audit_event", lambda **kwargs: kwargs)

    resolved = runtime_mod.get_runtime_settings(base_settings=base)
    assert resolved.auto_strategy_max_active_per_underlying == 3, (
        "Safe upward cap override must be applied without break-glass; "
        f"got {resolved.auto_strategy_max_active_per_underlying!r}"
    )

    snapshot_down = runtime_mod._RuntimeOverrideSnapshot(
        global_overrides={"auto_strategy_max_active_per_underlying": 1},
        account_overrides={},
        strategy_overrides={},
        fetched_at_epoch=time.time(),
    )
    monkeypatch.setattr(
        runtime_mod._provider, "_get_snapshot", lambda _s: snapshot_down
    )
    with pytest.raises(
        ValueError,
        match="LIVE runtime overrides require break-glass approval",
    ):
        runtime_mod.get_runtime_settings(base_settings=base)


def test_auto_strategy_max_active_per_underlying_is_protected_in_live():
    """Issue #212 / PR #265 round-5 (Codex round-4 P1 "Protect runtime cap
    overrides from dropping spread management"): the NIFTY direction-aware
    regime mapping lists 3 strategies per TRENDING_UP / TRENDING_DOWN
    split. A runtime override that lowers
    ``auto_strategy_max_active_per_underlying`` below 3 would truncate the
    spread strategy out of selection and lose SL/TP/EOD management of any
    spread carried into the trending regime. This regression test pins
    that ``auto_strategy_max_active_per_underlying`` cannot be overridden
    by a runtime config provider in LIVE without break-glass approval.
    """
    import app.config.runtime_config as runtime_mod

    assert "auto_strategy_max_active_per_underlying" in runtime_mod._LIVE_PROTECTED_OVERRIDE_KEYS, (
        "auto_strategy_max_active_per_underlying must be in "
        "_LIVE_PROTECTED_OVERRIDE_KEYS so a runtime override cannot lower "
        "the cap below the NIFTY direction-aware mapping requirement "
        "(PR #265 Codex round-4 P1)."
    )
    # Sanity: the key must also be in the broader allow-list otherwise the
    # protection is moot (the value would never be applied either way).
    assert "auto_strategy_max_active_per_underlying" in runtime_mod._ALLOWED_OVERRIDE_KEYS


def test_live_runtime_settings_rejects_protected_overrides_without_break_glass(
    monkeypatch,
):
    monkeypatch.setenv("TRADE_MODE", "LIVE")
    monkeypatch.setenv("PROFIT_SWEEP_STRATEGY", "SIMPLE")
    monkeypatch.setenv("PROFIT_DAILY_TARGET", "2000")
    monkeypatch.delenv("LIVE_RUNTIME_OVERRIDE_BREAK_GLASS", raising=False)
    monkeypatch.delenv(
        "LIVE_RUNTIME_OVERRIDE_BREAK_GLASS_APPROVED_BY",
        raising=False,
    )
    monkeypatch.delenv(
        "LIVE_RUNTIME_OVERRIDE_BREAK_GLASS_UNTIL",
        raising=False,
    )
    _reset_settings_cache()

    import app.config.settings as settings_mod
    import app.config.runtime_config as runtime_mod

    base = settings_mod.get_settings()
    snapshot = runtime_mod._RuntimeOverrideSnapshot(
        global_overrides={"enable_risk_checks": False},
        account_overrides={},
        strategy_overrides={},
        fetched_at_epoch=time.time(),
    )
    events: list[dict[str, object]] = []
    monkeypatch.setattr(runtime_mod._provider, "_get_snapshot", lambda _s: snapshot)
    monkeypatch.setattr(
        runtime_mod,
        "emit_audit_event",
        lambda **kwargs: events.append(kwargs) or kwargs,
    )

    with pytest.raises(
        ValueError,
        match="LIVE runtime overrides require break-glass approval",
    ):
        runtime_mod.get_runtime_settings(base_settings=base)

    assert events
    assert events[-1]["action"] == "runtime_override_rejected"
    assert events[-1]["after"] == {"enable_risk_checks": False}


def test_live_runtime_settings_allows_protected_overrides_with_break_glass(
    monkeypatch,
):
    monkeypatch.setenv("TRADE_MODE", "LIVE")
    monkeypatch.setenv("PROFIT_SWEEP_STRATEGY", "SIMPLE")
    monkeypatch.setenv("PROFIT_DAILY_TARGET", "2000")
    monkeypatch.setenv("LIVE_RUNTIME_OVERRIDE_BREAK_GLASS", "true")
    monkeypatch.setenv(
        "LIVE_RUNTIME_OVERRIDE_BREAK_GLASS_APPROVED_BY",
        "ops-oncall",
    )
    monkeypatch.setenv(
        "LIVE_RUNTIME_OVERRIDE_BREAK_GLASS_UNTIL",
        "2099-01-01T00:00:00Z",
    )
    _reset_settings_cache()

    import app.config.settings as settings_mod
    import app.config.runtime_config as runtime_mod

    base = settings_mod.get_settings()
    snapshot = runtime_mod._RuntimeOverrideSnapshot(
        global_overrides={"enable_risk_checks": False},
        account_overrides={},
        strategy_overrides={},
        fetched_at_epoch=time.time(),
    )
    events: list[dict[str, object]] = []
    monkeypatch.setattr(runtime_mod._provider, "_get_snapshot", lambda _s: snapshot)
    monkeypatch.setattr(
        runtime_mod,
        "emit_audit_event",
        lambda **kwargs: events.append(kwargs) or kwargs,
    )

    resolved = runtime_mod.get_runtime_settings(base_settings=base)

    assert resolved.enable_risk_checks is False
    assert events
    assert events[-1]["action"] == "runtime_override_break_glass_allowed"
    assert events[-1]["metadata"]["approved_by"] == "ops-oncall"
