"""Tests for LIVE startup validation guards — issues #137, #155, #154."""

from __future__ import annotations

import pytest


def _get_validate_fn():
    from app.core.startup_config_validator import validate_runtime_settings
    return validate_runtime_settings


def _make_settings(**overrides):
    from app.config.settings import Settings
    defaults = {
        "HUB_DEFAULT_TENANT_ID": "tenant-1",
        "PROFIT_SWEEP_ENABLED": "false",
        "PROFIT_SWEEP_STRATEGY": "SIMPLE",
        "PROFIT_SIMPLE_ENABLED": "false",
        "PROFIT_DAILY_TARGET": "10000",
        "PROFIT_MULTIPLE_ENABLED": "false",
        "PROFIT_MULTIPLE_TARGET": "500",
        "PROFIT_SWEEP_COOLDOWN_MINUTES": "30",
        "PROFIT_MAX_SWEEPS_PER_DAY": "5",
    }
    import os
    env_backup = {}
    for k, v in {**defaults, **overrides}.items():
        env_backup[k] = os.environ.get(k)
        os.environ[k] = v
    try:
        s = Settings()
    finally:
        for k, orig in env_backup.items():
            if orig is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = orig
    return s


def _base_live_env(**overrides):
    """Return a minimal passing LIVE environment dict."""
    base = {
        "TRADE_MODE": "LIVE",
        "CONTROL_PLANE_BACKEND": "postgres",
        "SWEEP_STATE_BACKEND": "postgres",
        "BROKER_SECRET_BACKEND": "postgres",
        "ENABLE_CAPITAL_CHECKS": "true",
        "CAPITAL_LIMITS_JSON": '{"tenant-1:A1":{"max_notional_per_order":500000}}',
        "ENABLE_RISK_CHECKS": "true",
        "RISK_ENABLE_DAILY_LOSS": "true",
        "RISK_MAX_DAILY_LOSS": "2000",
        "ENABLE_PROFIT_CHECKS": "true",
        "PROFIT_ENABLE_DAILY_TARGET": "true",
        "PROFIT_DAILY_TARGET": "10000",
        "ORDER_ROUTER_ENFORCE_IDEMPOTENCY": "true",
        "POSITION_OWNERSHIP_ENABLED": "true",
        "POSITION_OWNERSHIP_UNKNOWN_MODE": "block_entries",
        "ORDER_LIFECYCLE_PERSIST_MARKERS_REQUIRED": "true",
        "CAPITAL_FAIL_CLOSED_ON_MISSING_STATE": "true",
        "CAPITAL_FAIL_CLOSED_ON_MISSING_NOTIONAL_PRICE": "true",
        "ORDER_SUBMISSION_OUTBOX_ENABLED": "true",
        "ORDER_SUBMISSION_OUTBOX_REQUIRED": "true",
        "ORDER_SUBMISSION_OUTBOX_BACKEND": "postgres",
        "ENABLE_MULTI_HUB": "true",
        "USE_HUB_ROUTER": "true",
        "DISABLE_STREAM_WORKER": "false",
        "LEADER_LEASE_ENABLED": "true",
        "LEADER_LEASE_BACKEND": "postgres",
        "HUB_DEFAULT_TENANT_ID": "tenant-1",
        "CLIENT_LOCAL_IP": "1.2.3.4",
        "CLIENT_PUBLIC_IP": "1.2.3.4",
        "MAC_ADDRESS": "00:11:22:33:44:55",
        "ENABLE_DEMO_AUTH": "false",
    }
    base.update(overrides)
    return base


class TestHubDefaultTenantIdValidator:
    """#155: LIVE startup must reject placeholder HUB_DEFAULT_TENANT_ID."""

    def _run(self, env_overrides=None, settings_overrides=None):
        from app.config.boot_config import initialize_boot_config
        import os
        env = _base_live_env(**(env_overrides or {}))
        with pytest.MonkeyPatch().context() as mp:
            for k, v in env.items():
                mp.setenv(k, v)
            settings = _make_settings(**(settings_overrides or {}))
            boot_cfg = initialize_boot_config(force=True)
            validate = _get_validate_fn()
            validate(settings=settings, runtime_cfg=boot_cfg.runtime, env=env)

    def test_tenant_1_passes(self):
        """tenant-1 is a valid production tenant ID — should not raise."""
        self._run()  # default env has HUB_DEFAULT_TENANT_ID=tenant-1

    def test_tenant_default_is_rejected(self):
        """'tenant-default' is the historical placeholder — must be rejected in LIVE."""
        with pytest.raises(ValueError, match="HUB_DEFAULT_TENANT_ID"):
            self._run(
                env_overrides={"HUB_DEFAULT_TENANT_ID": "tenant-default"},
                settings_overrides={"HUB_DEFAULT_TENANT_ID": "tenant-default"},
            )

    def test_empty_tenant_id_is_rejected(self):
        """Empty tenant ID must be rejected in LIVE."""
        with pytest.raises(ValueError, match="HUB_DEFAULT_TENANT_ID"):
            self._run(
                env_overrides={"HUB_DEFAULT_TENANT_ID": ""},
                settings_overrides={"HUB_DEFAULT_TENANT_ID": ""},
            )


class TestRequireLiveTradeModeGuard:
    """#137: REQUIRE_LIVE_TRADE_MODE=true must reject non-LIVE TRADE_MODE."""

    def test_require_live_with_shadow_raises(self):
        from app.config.boot_config import initialize_boot_config
        import os
        env = _base_live_env(
            TRADE_MODE="SHADOW",
            REQUIRE_LIVE_TRADE_MODE="true",
        )
        with pytest.MonkeyPatch().context() as mp:
            for k, v in env.items():
                mp.setenv(k, v)
            settings = _make_settings(**{"HUB_DEFAULT_TENANT_ID": "tenant-1"})
            boot_cfg = initialize_boot_config(force=True)
            validate = _get_validate_fn()
            with pytest.raises(ValueError, match="REQUIRE_LIVE_TRADE_MODE"):
                validate(settings=settings, runtime_cfg=boot_cfg.runtime, env=env)
