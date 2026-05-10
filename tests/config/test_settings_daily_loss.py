from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.startup_config_validator import validate_runtime_startup_settings


def _settings(**overrides):
    defaults = {
        "eod_exit_time": "15:20",
        "eod_exit_retry_cutoff_time": "15:30",
        "hub_subscription_poll_interval": 60.0,
        "risk_enable_daily_loss": True,
        "risk_max_daily_loss": 2000.0,
        "risk_live_min_daily_loss_inr": 5000.0,
        "risk_live_low_daily_loss_action": "warn",
        "admin_api_key": "test-admin",
        "dashboard_hmac_auth_enabled": False,
        "dashboard_hmac_secret": "",
        "control_plane_backend": "postgres",
        "sweep_state_backend": "postgres",
        "enable_capital_checks": True,
        "capital_fail_closed_on_missing_state": True,
        "capital_fail_closed_on_missing_notional_price": True,
        "enable_risk_checks": True,
        "risk_fail_open_on_missing_pnl": False,
        "risk_include_unrealized_in_daily_loss": True,
        "enable_profit_checks": True,
        "profit_enable_daily_target": True,
        "profit_daily_target": 10000.0,
        "order_router_enforce_idempotency": True,
        "position_ownership_enabled": True,
        "position_ownership_unknown_mode": "block_entries",
        "ownership_persist_pending_locks": True,
        "order_lifecycle_persist_markers_required": True,
        "enable_multi_hub": True,
        "use_hub_router": True,
        "eod_cancel_open_orders_enabled": True,
        "circuit_breaker_persist_state": True,
        "hub_instance_name": "phoenix-live-1",
        "hub_default_tenant_id": "tenant-1",
        "dashboard_auth_disabled": False,
        "capital_limits_json": '{"tenant-1:A1":{"max_notional_per_order":500000}}',
        "allow_live_capital_limits_default_only": False,
        "allow_live_capital_checks_disabled": False,
        "angel_postback_token": "token",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _runtime(**overrides):
    defaults = {
        "app_env": "production",
        "dashboard_auth_disabled": False,
        "stream_watchdog_interval_seconds": 30.0,
        "stream_watchdog_restart_backoff_base_seconds": 1.0,
        "stream_watchdog_restart_backoff_max_seconds": 60.0,
        "stream_watchdog_restart_backoff_jitter_ratio": 0.1,
        "stream_watchdog_stable_run_window_seconds": 120.0,
        "demo_auth_requested": False,
        "enable_demo_auth": False,
        "disable_stream_worker": False,
        "leader_lease_enabled_override": True,
        "leader_lease_backend": "postgres",
        "leader_lease_id": "phoenix-live-single-stack",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _env(**overrides):
    base = {
        "APP_ENV": "production",
        "TRADE_MODE": "LIVE",
        "POSITION_SYNC_INTERVAL_SECONDS": "30",
        "ORDERS_SYNC_INTERVAL_SECONDS": "90",
        "BROKER_SECRET_BACKEND": "postgres",
        "CONTROL_PLANE_BACKEND": "postgres",
        "SWEEP_STATE_BACKEND": "postgres",
        "ENABLE_CAPITAL_CHECKS": "true",
        "CAPITAL_LIMITS_JSON": '{"tenant-1:A1":{"max_notional_per_order":500000}}',
        "ENABLE_RISK_CHECKS": "true",
        "RISK_ENABLE_DAILY_LOSS": "true",
        "RISK_MAX_DAILY_LOSS": "2000",
        "RISK_LIVE_MIN_DAILY_LOSS_INR": "5000",
        "ENABLE_PROFIT_CHECKS": "true",
        "PROFIT_ENABLE_DAILY_TARGET": "true",
        "PROFIT_DAILY_TARGET": "10000",
        "ORDER_ROUTER_ENFORCE_IDEMPOTENCY": "true",
        "POSITION_OWNERSHIP_ENABLED": "true",
        "POSITION_OWNERSHIP_UNKNOWN_MODE": "block_entries",
        "OWNERSHIP_PERSIST_PENDING_LOCKS": "true",
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


def test_live_low_daily_loss_warns_by_default(caplog):
    validate_runtime_startup_settings(
        settings=_settings(),
        runtime_cfg=_runtime(),
        env=_env(),
    )

    assert "startup.risk_daily_loss_low" in caplog.text
    assert "daily loss = 1% of capital" in caplog.text


def test_live_low_daily_loss_can_be_escalated_to_error():
    with pytest.raises(ValueError, match="RISK_MAX_DAILY_LOSS=2000"):
        validate_runtime_startup_settings(
            settings=_settings(risk_live_low_daily_loss_action="error"),
            runtime_cfg=_runtime(),
            env=_env(RISK_LIVE_LOW_DAILY_LOSS_ACTION="error"),
        )


def test_paper_low_daily_loss_does_not_warn(caplog):
    validate_runtime_startup_settings(
        settings=_settings(),
        runtime_cfg=_runtime(app_env="local"),
        env=_env(APP_ENV="local", TRADE_MODE="PAPER"),
    )

    assert "startup.risk_daily_loss_low" not in caplog.text
