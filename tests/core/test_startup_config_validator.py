import logging
from types import SimpleNamespace

import pytest

import app.core.startup_config_validator as startup_validator
from app.core.operating_mode import (
    OperatingMode,
    resolve_operating_mode_from_runtime,
)
from app.core.startup_config_validator import (
    validate_runtime_startup_settings,
    validate_startup_config,
)


KNOWN_STRATEGIES = {
    "ema20_strategy",
    "option_strategy",
    "delta_strangle",
    "put_momentum_scalper",
}


def _valid_runtime_settings(**overrides):
    values = {
        "eod_exit_time": "15:20",
        "eod_exit_retry_cutoff_time": "15:30",
        "hub_subscription_poll_interval": 60.0,
        "enable_capital_checks": True,
        "capital_limits_json": '{"tenant-1:A1":{"max_notional_per_order":500000,"max_gross_exposure":1000000}}',
        "allow_live_capital_limits_default_only": False,
        "capital_fail_closed_on_missing_state": True,
        "capital_fail_closed_on_missing_notional_price": True,
        "enable_risk_checks": True,
        "risk_enable_daily_loss": True,
        # Issue #221: LIVE validator now floors RISK_MAX_DAILY_LOSS at
        # RISK_MAX_DAILY_LOSS_LIVE_FLOOR (default ₹5,000). Use ₹10,000
        # so the default fixture continues to pass LIVE-path tests.
        "risk_max_daily_loss": 10000.0,
        "enable_profit_checks": True,
        "profit_enable_daily_target": True,
        "profit_daily_target": 1200.0,
        "order_router_enforce_idempotency": True,
        "position_ownership_enabled": True,
        "position_ownership_unknown_mode": "block_entries",
        "order_lifecycle_persist_markers_required": True,
        "control_plane_backend": "postgres",
        "sweep_state_backend": "postgres",
        "broker_secret_backend": "secret_manager",
        "enable_multi_hub": True,
        "use_hub_router": True,
        "risk_fail_open_on_missing_pnl": False,
        "admin_api_key": "test-admin",
        "dashboard_hmac_auth_enabled": False,
        "dashboard_hmac_secret": None,
        "circuit_breaker_persist_state": True,
        "eod_cancel_open_orders_enabled": True,
        "ownership_persist_pending_locks": True,
        "hub_instance_name": "phoenix-live-1",
        # Issue #155 / #221: LIVE startup requires the operator to
        # explicitly choose a production tenant identifier. The
        # historical placeholder ``tenant-default`` is rejected; a
        # real tenant id like ``tenant-1`` is acceptable.
        "hub_default_tenant_id": "tenant-1",
    }
    values.update(overrides)
    return type("Cfg", (), values)()


def _valid_runtime_cfg(**overrides):
    values = {
        "stream_watchdog_interval_seconds": 15.0,
        "stream_watchdog_restart_backoff_base_seconds": 5.0,
        "stream_watchdog_restart_backoff_max_seconds": 300.0,
        "stream_watchdog_restart_backoff_jitter_ratio": 0.2,
        "stream_watchdog_stable_run_window_seconds": 180.0,
        "disable_stream_worker": False,
        "leader_lease_enabled_override": True,
        "leader_lease_backend": "postgres",
        "leader_lease_id": "phoenix-live-single-stack",
        "leader_lease_ttl_seconds": 90,
        "leader_lease_renew_seconds": 30,
        "leader_lease_collection": "leader_leases",
        "app_env": "local",
        "dashboard_auth_disabled": False,
    }
    values.update(overrides)
    return type("RuntimeCfg", (), values)()


def _valid_live_env(**overrides):
    values = {
        "TRADE_MODE": "LIVE",
        "CLIENT_LOCAL_IP": "10.0.0.10",
        "CLIENT_PUBLIC_IP": "203.0.113.10",
        "MAC_ADDRESS": "02:00:00:00:00:10",
        "POSITION_SYNC_INTERVAL_SECONDS": "30",
        "ORDERS_SYNC_INTERVAL_SECONDS": "90",
        "DASHBOARD_AUTH_DISABLED": "false",
        # Issue #155 / #221: validator requires HUB_DEFAULT_TENANT_ID
        # in both the settings object AND the env so the deployment
        # cannot drift between Pydantic-resolved value and the raw
        # env-file value the operator audits.
        "HUB_DEFAULT_TENANT_ID": "tenant-1",
    }
    values.update(overrides)
    return values


def _valid_cfg(first_entry_time: str = "09:30"):
    return {
        "ng": {
            "ema20_require_rsi_falling": False,
        },
        "strategies": [
            {
                "name": "ema20_strategy",
                "enabled": True,
                "instruments": ["NG_FUT"],
                "params": {
                    "timeframe_seconds": 300,
                    "ema_period": 8,
                    "min_atr": 2.0,
                    "first_entry_time": first_entry_time,
                    "square_off_time": "23:30",
                    "sl_pct": 0.18,
                    "tp_pct": 0.12,
                    "trail_buffer_pct": 0.08,
                    "trail_trigger_pct": 0.25,
                },
            }
        ],
        "instruments": {
            "NG_FUT": {
                "enabled": True,
                "allowed_strategies": ["ema20_strategy"],
            }
        },
    }


def _valid_index_cfg():
    return {
        "strategies": [
            {
                "name": "ema20_strategy",
                "enabled": True,
                "instruments": ["NIFTY_IDX"],
                "params": {
                    "timeframe_seconds": 300,
                    "ema_period": 20,
                    "min_atr": 1.0,
                    "require_rsi_falling": False,
                },
            }
        ],
        "instruments": {
            "NIFTY_IDX": {
                "enabled": True,
                "allowed_strategies": ["ema20_strategy"],
            }
        },
    }


def _valid_put_momentum_cfg():
    return {
        "strategies": [
            {
                "name": "put_momentum_scalper",
                "enabled": True,
                "instruments": ["NIFTY_IDX"],
                "params": {
                    "timeframe_seconds_5m": 300,
                    "timeframe_seconds_15m": 900,
                    "rsi_min": 25,
                    "rsi_max": 40,
                },
            }
        ],
        "instruments": {
            "NIFTY_IDX": {
                "enabled": True,
                "allowed_strategies": ["put_momentum_scalper"],
            }
        },
    }


def test_validator_accepts_valid_ema20_ng_config():
    cfg = _valid_cfg()
    validate_startup_config(
        strategy_cfg=cfg,
        trade_mode="LIVE",
        disable_trading_window_filter=False,
        known_strategy_names=KNOWN_STRATEGIES,
    )


def test_validator_rejects_live_when_window_filter_disabled():
    cfg = _valid_cfg()
    with pytest.raises(ValueError, match="DISABLE_TRADING_WINDOW_FILTER=false"):
        validate_startup_config(
            strategy_cfg=cfg,
            trade_mode="LIVE",
            disable_trading_window_filter=True,
            known_strategy_names=KNOWN_STRATEGIES,
        )


def test_validator_accepts_shadow_mode():
    cfg = _valid_cfg()
    validate_startup_config(
        strategy_cfg=cfg,
        trade_mode="SHADOW",
        disable_trading_window_filter=False,
        known_strategy_names=KNOWN_STRATEGIES,
    )


def test_validator_rejects_unknown_trade_mode():
    cfg = _valid_cfg()
    with pytest.raises(ValueError, match="TRADE_MODE must be one of PAPER\\|LIVE\\|SHADOW"):
        validate_startup_config(
            strategy_cfg=cfg,
            trade_mode="DRY_RUN",
            disable_trading_window_filter=False,
            known_strategy_names=KNOWN_STRATEGIES,
        )


def test_validator_rejects_unknown_allowed_strategy_alias():
    cfg = _valid_cfg()
    cfg["instruments"]["NG_FUT"]["allowed_strategies"] = [
        "ema20_strategy",
        "unknown_alias",
    ]
    with pytest.raises(ValueError, match="Unknown strategy"):
        validate_startup_config(
            strategy_cfg=cfg,
            trade_mode="PAPER",
            disable_trading_window_filter=False,
            known_strategy_names=KNOWN_STRATEGIES,
        )


def test_validator_accepts_deprecated_aliases_when_canonical_known_set_is_used():
    cfg = _valid_cfg()
    cfg["strategies"][0]["name"] = "ema20"
    cfg["instruments"]["NG_FUT"]["allowed_strategies"] = ["options_generic"]
    validate_startup_config(
        strategy_cfg=cfg,
        trade_mode="PAPER",
        disable_trading_window_filter=False,
        known_strategy_names=KNOWN_STRATEGIES | {"option_strategy"},
    )


def test_validator_rejects_ng_first_entry_before_0930():
    cfg = _valid_cfg(first_entry_time="09:15")
    with pytest.raises(ValueError, match="first_entry_time must be >= 09:30"):
        validate_startup_config(
            strategy_cfg=cfg,
            trade_mode="PAPER",
            disable_trading_window_filter=False,
            known_strategy_names=KNOWN_STRATEGIES,
        )


def test_validator_rejects_invalid_adx_period():
    cfg = _valid_cfg()
    cfg["strategies"][0]["params"]["adx_period"] = 1
    with pytest.raises(ValueError, match="adx_period must be >= 2"):
        validate_startup_config(
            strategy_cfg=cfg,
            trade_mode="PAPER",
            disable_trading_window_filter=False,
            known_strategy_names=KNOWN_STRATEGIES,
        )


def test_validator_rejects_negative_min_adx():
    cfg = _valid_cfg()
    cfg["strategies"][0]["params"]["min_adx"] = -1
    with pytest.raises(ValueError, match="min_adx must be >= 0.0"):
        validate_startup_config(
            strategy_cfg=cfg,
            trade_mode="PAPER",
            disable_trading_window_filter=False,
            known_strategy_names=KNOWN_STRATEGIES,
        )


def test_validator_rejects_negative_min_di_spread():
    cfg = _valid_cfg()
    cfg["strategies"][0]["params"]["min_di_spread"] = -0.1
    with pytest.raises(ValueError, match="min_di_spread must be >= 0.0"):
        validate_startup_config(
            strategy_cfg=cfg,
            trade_mode="PAPER",
            disable_trading_window_filter=False,
            known_strategy_names=KNOWN_STRATEGIES,
        )


def test_validator_rejects_dynamic_policy_enabled_without_policy_id():
    cfg = _valid_cfg()
    cfg["strategies"][0]["params"]["dynamic_policy"] = {
        "enabled": True,
        "freeze_on_entry": True,
    }
    with pytest.raises(ValueError, match="policy_id is required"):
        validate_startup_config(
            strategy_cfg=cfg,
            trade_mode="PAPER",
            disable_trading_window_filter=False,
            known_strategy_names=KNOWN_STRATEGIES,
        )


def test_validator_accepts_dynamic_policy_when_policy_id_present():
    cfg = _valid_cfg()
    cfg["strategies"][0]["params"]["dynamic_policy"] = {
        "enabled": True,
        "policy_id": "ema20_ng_v1",
        "freeze_on_entry": True,
        "hold_bars": 3,
        "profiles": {
            "TRENDING": {"tp_pct": 0.4},
            "NO_TRADE": {"disable_entries": True},
        },
    }
    validate_startup_config(
        strategy_cfg=cfg,
        trade_mode="PAPER",
        disable_trading_window_filter=False,
        known_strategy_names=KNOWN_STRATEGIES,
    )


def test_validator_rejects_enabled_dynamic_policy_with_only_no_trade_profile():
    cfg = _valid_cfg()
    cfg["strategies"][0]["params"]["dynamic_policy"] = {
        "enabled": True,
        "policy_id": "ema20_ng_v1",
        "freeze_on_entry": True,
        "hold_bars": 3,
        "profiles": {
            "NO_TRADE": {"disable_entries": True},
        },
    }
    with pytest.raises(
        ValueError,
        match="requires at least one non-empty regime profile outside NO_TRADE",
    ):
        validate_startup_config(
            strategy_cfg=cfg,
            trade_mode="PAPER",
            disable_trading_window_filter=False,
            known_strategy_names=KNOWN_STRATEGIES,
        )


def test_validator_rejects_legacy_live_ema20_qty_matching_lot_size():
    cfg = _valid_index_cfg()
    with pytest.raises(ValueError, match="NIFTY_EMA20_QTY=65"):
        validate_startup_config(
            strategy_cfg=cfg,
            trade_mode="LIVE",
            disable_trading_window_filter=False,
            known_strategy_names=KNOWN_STRATEGIES,
            env={"NIFTY_EMA20_QTY": "65"},
        )


def test_validator_accepts_live_ema20_lots_canonical_key():
    cfg = _valid_index_cfg()
    validate_startup_config(
        strategy_cfg=cfg,
        trade_mode="LIVE",
        disable_trading_window_filter=False,
        known_strategy_names=KNOWN_STRATEGIES,
        env={"NIFTY_EMA20_LOTS": "1"},
    )


def test_validator_accepts_legacy_live_ema20_qty_with_escape_hatch():
    cfg = _valid_index_cfg()
    validate_startup_config(
        strategy_cfg=cfg,
        trade_mode="LIVE",
        disable_trading_window_filter=False,
        known_strategy_names=KNOWN_STRATEGIES,
        env={
            "NIFTY_EMA20_QTY": "65",
            "ALLOW_LEGACY_EMA20_QTY_IN_LIVE": "true",
        },
    )


def test_validator_rejects_live_put_momentum_missing_indicator_footprint(monkeypatch):
    cfg = _valid_put_momentum_cfg()
    monkeypatch.setattr(
        startup_validator,
        "build_indicator_engine_plan",
        lambda _cfg: SimpleNamespace(
            ema_periods_by_timeframe={300: [20]},
            engine_timeframes=[300],
            adx_period=14,
        ),
    )

    with pytest.raises(ValueError, match="put_momentum_scalper NIFTY_IDX requires indicator footprint"):
        validate_startup_config(
            strategy_cfg=cfg,
            trade_mode="LIVE",
            disable_trading_window_filter=False,
            known_strategy_names=KNOWN_STRATEGIES,
        )


def test_validator_warns_in_paper_when_put_momentum_indicator_footprint_missing(
    monkeypatch, caplog
):
    cfg = _valid_put_momentum_cfg()
    monkeypatch.setattr(
        startup_validator,
        "build_indicator_engine_plan",
        lambda _cfg: SimpleNamespace(
            ema_periods_by_timeframe={300: [20]},
            engine_timeframes=[300],
            adx_period=14,
        ),
    )

    with caplog.at_level(logging.WARNING):
        validate_startup_config(
            strategy_cfg=cfg,
            trade_mode="PAPER",
            disable_trading_window_filter=False,
            known_strategy_names=KNOWN_STRATEGIES,
        )

    assert "put_momentum_scalper" in caplog.text
    assert "indicator footprint warning" in caplog.text


def test_runtime_settings_validator_accepts_valid_values():
    validate_runtime_startup_settings(
        settings=_valid_runtime_settings(),
        runtime_cfg=_valid_runtime_cfg(),
        env={
            "POSITION_SYNC_INTERVAL_SECONDS": "30",
            "ORDERS_SYNC_INTERVAL_SECONDS": "90",
        },
    )


def test_runtime_settings_validator_rejects_invalid_values():
    with pytest.raises(ValueError, match="Startup runtime setting validation failed"):
        validate_runtime_startup_settings(
            settings=_valid_runtime_settings(
                eod_exit_time="25:99",
                eod_exit_retry_cutoff_time="bad",
                hub_subscription_poll_interval=0,
                risk_max_daily_loss=None,
            ),
            runtime_cfg=_valid_runtime_cfg(
                stream_watchdog_interval_seconds=0,
                stream_watchdog_restart_backoff_base_seconds=0,
                stream_watchdog_restart_backoff_max_seconds=0,
                stream_watchdog_restart_backoff_jitter_ratio=2,
                stream_watchdog_stable_run_window_seconds=-1,
            ),
            env={
                "POSITION_SYNC_INTERVAL_SECONDS": "0",
                "ORDERS_SYNC_INTERVAL_SECONDS": "-1",
            },
        )


def test_runtime_settings_validator_rejects_non_local_without_admin_api_key():
    with pytest.raises(ValueError, match="ADMIN_API_KEY must be configured"):
        validate_runtime_startup_settings(
            settings=_valid_runtime_settings(admin_api_key=None),
            runtime_cfg=_valid_runtime_cfg(app_env="production"),
            env={
                "POSITION_SYNC_INTERVAL_SECONDS": "30",
                "ORDERS_SYNC_INTERVAL_SECONDS": "90",
                "DASHBOARD_AUTH_DISABLED": "false",
            },
        )


def test_runtime_settings_validator_rejects_non_local_when_dashboard_auth_disabled():
    with pytest.raises(ValueError, match="DASHBOARD_AUTH_DISABLED must be false"):
        validate_runtime_startup_settings(
            settings=_valid_runtime_settings(),
            runtime_cfg=_valid_runtime_cfg(app_env="staging"),
            env={
                "POSITION_SYNC_INTERVAL_SECONDS": "30",
                "ORDERS_SYNC_INTERVAL_SECONDS": "90",
                "DASHBOARD_AUTH_DISABLED": "true",
            },
        )


def test_runtime_settings_validator_rejects_hmac_without_secret_in_non_local():
    with pytest.raises(ValueError, match="DASHBOARD_HMAC_AUTH_ENABLED=true requires DASHBOARD_HMAC_SECRET"):
        validate_runtime_startup_settings(
            settings=_valid_runtime_settings(
                dashboard_hmac_auth_enabled=True,
                dashboard_hmac_secret=None,
            ),
            runtime_cfg=_valid_runtime_cfg(app_env="production"),
            env={
                "POSITION_SYNC_INTERVAL_SECONDS": "30",
                "ORDERS_SYNC_INTERVAL_SECONDS": "90",
                "DASHBOARD_AUTH_DISABLED": "false",
            },
        )


def test_runtime_settings_validator_accepts_local_mock_sweep_state_escape_hatch():
    validate_runtime_startup_settings(
        settings=_valid_runtime_settings(),
        runtime_cfg=_valid_runtime_cfg(app_env="local"),
        env={
            "ALLOW_MOCK_SWEEP_STATE": "true",
            "POSITION_SYNC_INTERVAL_SECONDS": "30",
            "ORDERS_SYNC_INTERVAL_SECONDS": "90",
        },
    )


def test_runtime_settings_validator_rejects_non_local_mock_sweep_state_escape_hatch():
    with pytest.raises(
        ValueError,
        match="APP_ENV=production forbids enabling mock sweep/EOD state escape hatches: ALLOW_MOCK_SWEEP_STATE",
    ):
        validate_runtime_startup_settings(
            settings=_valid_runtime_settings(),
            runtime_cfg=_valid_runtime_cfg(app_env="production"),
            env={
                "ALLOW_MOCK_SWEEP_STATE": "true",
                "POSITION_SYNC_INTERVAL_SECONDS": "30",
                "ORDERS_SYNC_INTERVAL_SECONDS": "90",
                "DASHBOARD_AUTH_DISABLED": "false",
            },
        )


def test_runtime_settings_validator_accepts_live_safe_defaults():
    validate_runtime_startup_settings(
        settings=_valid_runtime_settings(),
        runtime_cfg=_valid_runtime_cfg(app_env="production", disable_stream_worker=False),
        env=_valid_live_env(),
        )


def test_runtime_settings_validator_rejects_live_with_capital_checks_disabled():
    with pytest.raises(
        ValueError,
        match="TRADE_MODE=LIVE requires ENABLE_CAPITAL_CHECKS=true",
    ):
        validate_runtime_startup_settings(
            settings=_valid_runtime_settings(enable_capital_checks=False),
            runtime_cfg=_valid_runtime_cfg(
                app_env="production",
                disable_stream_worker=False,
            ),
            env=_valid_live_env(),
        )


def test_runtime_settings_validator_rejects_live_capital_checks_disabled_even_with_override():
    # ALLOW_LIVE_CAPITAL_CHECKS_DISABLED no longer bypasses the hard gate.
    with pytest.raises(
        ValueError,
        match="TRADE_MODE=LIVE requires ENABLE_CAPITAL_CHECKS=true",
    ):
        validate_runtime_startup_settings(
            settings=_valid_runtime_settings(enable_capital_checks=False),
            runtime_cfg=_valid_runtime_cfg(
                app_env="production",
                disable_stream_worker=False,
            ),
            env=_valid_live_env(ALLOW_LIVE_CAPITAL_CHECKS_DISABLED="true"),
        )


def test_runtime_settings_validator_rejects_live_without_leader_lease():
    with pytest.raises(
        ValueError,
        match="TRADE_MODE=LIVE requires LEADER_LEASE_ENABLED=true",
    ):
        validate_runtime_startup_settings(
            settings=_valid_runtime_settings(),
            runtime_cfg=_valid_runtime_cfg(
                app_env="production",
                disable_stream_worker=False,
                leader_lease_enabled_override=False,
            ),
            env=_valid_live_env(),
        )


def test_runtime_settings_validator_rejects_live_without_postgres_leader_lease():
    with pytest.raises(
        ValueError,
        match="TRADE_MODE=LIVE requires LEADER_LEASE_BACKEND=postgres",
    ):
        validate_runtime_startup_settings(
            settings=_valid_runtime_settings(),
            runtime_cfg=_valid_runtime_cfg(
                app_env="production",
                disable_stream_worker=False,
                leader_lease_backend="firestore",
            ),
            env=_valid_live_env(),
        )


def test_runtime_settings_validator_rejects_live_missing_broker_network_identity():
    with pytest.raises(ValueError, match="CLIENT_PUBLIC_IP must be set in TRADE_MODE=LIVE"):
        validate_runtime_startup_settings(
            settings=_valid_runtime_settings(),
            runtime_cfg=_valid_runtime_cfg(app_env="production", disable_stream_worker=False),
            env=_valid_live_env(
                CLIENT_LOCAL_IP="",
                CLIENT_PUBLIC_IP="",
                MAC_ADDRESS="",
            ),
        )


def test_runtime_settings_validator_rejects_live_placeholder_broker_network_identity():
    with pytest.raises(ValueError, match="CLIENT_LOCAL_IP must not be loopback"):
        validate_runtime_startup_settings(
            settings=_valid_runtime_settings(),
            runtime_cfg=_valid_runtime_cfg(app_env="production", disable_stream_worker=False),
            env=_valid_live_env(
                CLIENT_LOCAL_IP="127.0.0.1",
                CLIENT_PUBLIC_IP="127.0.0.1",
                MAC_ADDRESS="AA:BB:CC:DD:EE:FF",
            ),
        )


def test_runtime_settings_validator_rejects_live_mock_sweep_state_escape_hatch():
    with pytest.raises(
        ValueError,
        match="TRADE_MODE=LIVE forbids mock sweep/EOD state escape hatches: ALLOW_MOCK_SWEEP_STATE",
    ):
        validate_runtime_startup_settings(
            settings=_valid_runtime_settings(),
            runtime_cfg=_valid_runtime_cfg(
                app_env="production",
                disable_stream_worker=False,
            ),
            env={
                "TRADE_MODE": "LIVE",
                "ALLOW_MOCK_SWEEP_STATE": "true",
                "POSITION_SYNC_INTERVAL_SECONDS": "30",
                "ORDERS_SYNC_INTERVAL_SECONDS": "90",
                "DASHBOARD_AUTH_DISABLED": "false",
            },
        )


def test_operating_mode_resolution_returns_legacy_authoritative_for_legacy_only():
    resolution = resolve_operating_mode_from_runtime(
        settings=_valid_runtime_settings(
            enable_multi_hub=False,
            use_hub_router=False,
        ),
        runtime_cfg=_valid_runtime_cfg(
            app_env="production",
            disable_stream_worker=False,
        ),
    )

    assert resolution.mode == OperatingMode.LEGACY_AUTHORITATIVE
    assert resolution.authority_path == "legacy"


def test_operating_mode_resolution_returns_hub_authoritative_for_hub_only():
    resolution = resolve_operating_mode_from_runtime(
        settings=_valid_runtime_settings(
            enable_multi_hub=True,
            use_hub_router=True,
        ),
        runtime_cfg=_valid_runtime_cfg(
            app_env="production",
            disable_stream_worker=False,
        ),
    )

    assert resolution.mode == OperatingMode.HUB_AUTHORITATIVE
    assert resolution.authority_path == "hub"


def test_runtime_settings_validator_rejects_live_ambiguous_authority_path():
    with pytest.raises(
        ValueError,
        match="ENABLE_MULTI_HUB and USE_HUB_ROUTER to resolve exactly one authority path",
    ):
        validate_runtime_startup_settings(
            settings=_valid_runtime_settings(enable_multi_hub=True, use_hub_router=False),
            runtime_cfg=_valid_runtime_cfg(app_env="production"),
            env={
                "TRADE_MODE": "LIVE",
                "POSITION_SYNC_INTERVAL_SECONDS": "30",
                "ORDERS_SYNC_INTERVAL_SECONDS": "90",
                "DASHBOARD_AUTH_DISABLED": "false",
            },
        )


def test_runtime_settings_validator_rejects_live_without_any_authoritative_writer():
    with pytest.raises(
        ValueError,
        match="rejects legacy authoritative mode",
    ):
        validate_runtime_startup_settings(
            settings=_valid_runtime_settings(enable_multi_hub=False, use_hub_router=False),
            runtime_cfg=_valid_runtime_cfg(
                app_env="production",
                disable_stream_worker=False,
            ),
            env={
                "TRADE_MODE": "LIVE",
                "POSITION_SYNC_INTERVAL_SECONDS": "30",
                "ORDERS_SYNC_INTERVAL_SECONDS": "90",
                "DASHBOARD_AUTH_DISABLED": "false",
            },
        )


def test_runtime_settings_validator_rejects_live_legacy_authority_path():
    with pytest.raises(
        ValueError,
        match="kill-switch durability is still local-file only",
    ):
        validate_runtime_startup_settings(
            settings=_valid_runtime_settings(enable_multi_hub=False, use_hub_router=False),
            runtime_cfg=_valid_runtime_cfg(app_env="production"),
            env={
                "TRADE_MODE": "LIVE",
                "POSITION_SYNC_INTERVAL_SECONDS": "30",
                "ORDERS_SYNC_INTERVAL_SECONDS": "90",
                "DASHBOARD_AUTH_DISABLED": "false",
            },
        )


def test_runtime_settings_validator_rejects_live_when_stream_worker_disabled():
    with pytest.raises(
        ValueError,
        match="TRADE_MODE=LIVE requires DISABLE_STREAM_WORKER=false",
    ):
        validate_runtime_startup_settings(
            settings=_valid_runtime_settings(),
            runtime_cfg=_valid_runtime_cfg(
                app_env="production",
                disable_stream_worker=True,
            ),
            env={
                "TRADE_MODE": "LIVE",
                "POSITION_SYNC_INTERVAL_SECONDS": "30",
                "ORDERS_SYNC_INTERVAL_SECONDS": "90",
                "DASHBOARD_AUTH_DISABLED": "false",
            },
        )


def test_runtime_settings_validator_rejects_live_env_secret_backend():
    """LIVE mode rejects env as a broker secret source."""
    with pytest.raises(
        ValueError,
        match="TRADE_MODE=LIVE requires BROKER_SECRET_BACKEND to resolve to secret_manager or postgres",
    ):
        validate_runtime_startup_settings(
            settings=_valid_runtime_settings(broker_secret_backend="env"),
            runtime_cfg=_valid_runtime_cfg(app_env="production", disable_stream_worker=False),
            env={
                "TRADE_MODE": "LIVE",
                "POSITION_SYNC_INTERVAL_SECONDS": "30",
                "ORDERS_SYNC_INTERVAL_SECONDS": "90",
                "DASHBOARD_AUTH_DISABLED": "false",
            },
        )


def test_runtime_settings_validator_accepts_live_postgres_secret_backend():
    """LIVE mode accepts postgres as broker secret backend."""
    validate_runtime_startup_settings(
        settings=_valid_runtime_settings(broker_secret_backend="postgres"),
        runtime_cfg=_valid_runtime_cfg(app_env="production", disable_stream_worker=False),
        env=_valid_live_env(),
    )


def test_runtime_settings_validator_rejects_live_unknown_secret_backend():
    """LIVE mode rejects unrecognized broker secret backends."""
    with pytest.raises(
        ValueError,
        match="TRADE_MODE=LIVE requires BROKER_SECRET_BACKEND to resolve to secret_manager or postgres",
    ):
        validate_runtime_startup_settings(
            settings=_valid_runtime_settings(broker_secret_backend="plaintext_file"),
            runtime_cfg=_valid_runtime_cfg(app_env="production", disable_stream_worker=False),
            env={
                "TRADE_MODE": "LIVE",
                "POSITION_SYNC_INTERVAL_SECONDS": "30",
                "ORDERS_SYNC_INTERVAL_SECONDS": "90",
                "DASHBOARD_AUTH_DISABLED": "false",
            },
        )


def test_runtime_settings_validator_rejects_live_demo_auth():
    with pytest.raises(
        ValueError,
        match="TRADE_MODE=LIVE requires ENABLE_DEMO_AUTH=false",
    ):
        validate_runtime_startup_settings(
            settings=_valid_runtime_settings(),
            runtime_cfg=_valid_runtime_cfg(
                app_env="production",
                demo_auth_requested=True,
                dashboard_auth_disabled=False,
            ),
            env={
                "TRADE_MODE": "LIVE",
                "POSITION_SYNC_INTERVAL_SECONDS": "30",
                "ORDERS_SYNC_INTERVAL_SECONDS": "90",
                "DASHBOARD_AUTH_DISABLED": "false",
                "ENABLE_DEMO_AUTH": "true",
            },
        )


def test_runtime_settings_validator_rejects_live_without_required_guardrails():
    with pytest.raises(
        ValueError,
        match="TRADE_MODE=LIVE requires ORDER_ROUTER_ENFORCE_IDEMPOTENCY=true",
    ):
        validate_runtime_startup_settings(
            settings=_valid_runtime_settings(
                order_router_enforce_idempotency=False,
                risk_fail_open_on_missing_pnl=True,
            ),
            runtime_cfg=_valid_runtime_cfg(app_env="production"),
            env={
                "TRADE_MODE": "LIVE",
                "POSITION_SYNC_INTERVAL_SECONDS": "30",
                "ORDERS_SYNC_INTERVAL_SECONDS": "90",
                "DASHBOARD_AUTH_DISABLED": "false",
            },
        )


def test_runtime_settings_validator_rejects_live_without_required_submission_outbox():
    with pytest.raises(
        ValueError,
        match="TRADE_MODE=LIVE requires ORDER_SUBMISSION_OUTBOX_ENABLED=true",
    ):
        validate_runtime_startup_settings(
            settings=_valid_runtime_settings(),
            runtime_cfg=_valid_runtime_cfg(app_env="production"),
            env={
                "TRADE_MODE": "LIVE",
                "POSITION_SYNC_INTERVAL_SECONDS": "30",
                "ORDERS_SYNC_INTERVAL_SECONDS": "90",
                "DASHBOARD_AUTH_DISABLED": "false",
                "ORDER_SUBMISSION_OUTBOX_ENABLED": "false",
                "ORDER_SUBMISSION_OUTBOX_REQUIRED": "false",
                "ORDER_SUBMISSION_OUTBOX_BACKEND": "memory",
            },
        )


def test_runtime_settings_validator_rejects_live_missing_eod_exit_time():
    with pytest.raises(
        ValueError,
        match="TRADE_MODE=LIVE requires EOD_EXIT_TIME to be set",
    ):
        validate_runtime_startup_settings(
            settings=_valid_runtime_settings(eod_exit_time=""),
            runtime_cfg=_valid_runtime_cfg(app_env="production"),
            env={
                "TRADE_MODE": "LIVE",
                "POSITION_SYNC_INTERVAL_SECONDS": "30",
                "ORDERS_SYNC_INTERVAL_SECONDS": "90",
                "DASHBOARD_AUTH_DISABLED": "false",
            },
        )


def test_runtime_settings_validator_rejects_live_missing_eod_cancel_orders():
    """Gate 6: eod_cancel_open_orders_enabled must be explicitly configured."""
    # Build settings without eod_cancel_open_orders_enabled to simulate missing config
    settings = _valid_runtime_settings()
    delattr(type(settings), "eod_cancel_open_orders_enabled")
    with pytest.raises(
        ValueError,
        match="TRADE_MODE=LIVE requires EOD_CANCEL_OPEN_ORDERS_ENABLED to be explicitly configured",
    ):
        validate_runtime_startup_settings(
            settings=settings,
            runtime_cfg=_valid_runtime_cfg(app_env="production"),
            env={
                "TRADE_MODE": "LIVE",
                "POSITION_SYNC_INTERVAL_SECONDS": "30",
                "ORDERS_SYNC_INTERVAL_SECONDS": "90",
                "DASHBOARD_AUTH_DISABLED": "false",
            },
        )


def test_runtime_settings_validator_rejects_live_without_circuit_breaker_persist():
    """Gate 7: Kill-switch durability requires CIRCUIT_BREAKER_PERSIST_STATE=true."""
    with pytest.raises(
        ValueError,
        match="TRADE_MODE=LIVE requires CIRCUIT_BREAKER_PERSIST_STATE=true",
    ):
        validate_runtime_startup_settings(
            settings=_valid_runtime_settings(circuit_breaker_persist_state=False),
            runtime_cfg=_valid_runtime_cfg(app_env="production"),
            env={
                "TRADE_MODE": "LIVE",
                "POSITION_SYNC_INTERVAL_SECONDS": "30",
                "ORDERS_SYNC_INTERVAL_SECONDS": "90",
                "DASHBOARD_AUTH_DISABLED": "false",
            },
        )


def test_runtime_settings_validator_rejects_live_dashboard_auth_disabled():
    """Gate: DASHBOARD_AUTH_DISABLED must be false in LIVE mode."""
    with pytest.raises(
        ValueError,
        match="TRADE_MODE=LIVE requires DASHBOARD_AUTH_DISABLED=false",
    ):
        validate_runtime_startup_settings(
            settings=_valid_runtime_settings(),
            runtime_cfg=_valid_runtime_cfg(app_env="production"),
            env={
                "TRADE_MODE": "LIVE",
                "POSITION_SYNC_INTERVAL_SECONDS": "30",
                "ORDERS_SYNC_INTERVAL_SECONDS": "90",
                "DASHBOARD_AUTH_DISABLED": "true",
            },
        )


def test_runtime_settings_validator_rejects_live_without_ownership_persist_pending_locks():
    """OWNERSHIP_PERSIST_PENDING_LOCKS must be true in LIVE to prevent duplicate-entry windows."""
    with pytest.raises(
        ValueError,
        match="OWNERSHIP_PERSIST_PENDING_LOCKS=true",
    ):
        validate_runtime_startup_settings(
            settings=_valid_runtime_settings(ownership_persist_pending_locks=False),
            runtime_cfg=_valid_runtime_cfg(app_env="production"),
            env=_valid_live_env(),
        )


def test_runtime_settings_validator_rejects_live_without_hub_instance_name():
    """HUB_INSTANCE_NAME must be non-empty in LIVE to scope circuit-breaker state per deployment."""
    with pytest.raises(
        ValueError,
        match="HUB_INSTANCE_NAME",
    ):
        validate_runtime_startup_settings(
            settings=_valid_runtime_settings(hub_instance_name=""),
            runtime_cfg=_valid_runtime_cfg(app_env="production"),
            env=_valid_live_env(),
        )


def test_runtime_settings_validator_accepts_live_with_ownership_persist_and_instance_name():
    """Both new LIVE gates pass when the required fields are configured."""
    validate_runtime_startup_settings(
        settings=_valid_runtime_settings(
            ownership_persist_pending_locks=True,
            hub_instance_name="phoenix-live-1",
        ),
        runtime_cfg=_valid_runtime_cfg(app_env="production"),
        env=_valid_live_env(),
    )


# ---------------------------------------------------------------------------
# Issue #28: REQUIRE_LIVE_TRADE_MODE guard
# ---------------------------------------------------------------------------

def test_require_live_trade_mode_rejects_shadow_when_flag_set():
    """REQUIRE_LIVE_TRADE_MODE=true must cause startup failure when TRADE_MODE != LIVE."""
    with pytest.raises(ValueError, match="REQUIRE_LIVE_TRADE_MODE=true"):
        validate_runtime_startup_settings(
            settings=_valid_runtime_settings(),
            runtime_cfg=_valid_runtime_cfg(),
            env={
                **_valid_live_env(TRADE_MODE="SHADOW"),
                "REQUIRE_LIVE_TRADE_MODE": "true",
            },
        )


def test_require_live_trade_mode_passes_when_trade_mode_is_live():
    """REQUIRE_LIVE_TRADE_MODE=true must not add errors when TRADE_MODE=LIVE."""
    validate_runtime_startup_settings(
        settings=_valid_runtime_settings(hub_instance_name="phoenix-live-1"),
        runtime_cfg=_valid_runtime_cfg(app_env="production"),
        env={
            **_valid_live_env(),
            "REQUIRE_LIVE_TRADE_MODE": "true",
        },
    )
