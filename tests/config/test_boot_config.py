from __future__ import annotations

from app.config.boot_config import RuntimeConfig, StrategyValueResolver


def test_strategy_value_resolver_precedence_prefixed_env_first():
    resolver = StrategyValueResolver(
        env_prefix="NIFTY_",
        params={"SL_PCT": "0.20"},
        env={
            "SL_PCT": "0.30",
            "NIFTY_SL_PCT": "0.40",
        },
    )

    assert resolver.get_float("SL_PCT", 0.0) == 0.40


def test_strategy_value_resolver_precedence_global_env_then_params_then_default():
    resolver = StrategyValueResolver(
        env_prefix="NIFTY_",
        params={"TP_PCT": "0.25"},
        env={"TP_PCT": "0.35"},
    )
    assert resolver.get_float("TP_PCT", 0.0) == 0.35

    resolver_no_env = StrategyValueResolver(
        env_prefix="NIFTY_",
        params={"TP_PCT": "0.25"},
        env={},
    )
    assert resolver_no_env.get_float("TP_PCT", 0.0) == 0.25
    assert resolver_no_env.get_float("MISSING", 0.10) == 0.10


def test_runtime_config_from_env_parses_types():
    cfg = RuntimeConfig.from_env(
        {
            "APP_ENV": "dev",
            "RUNNER_MODE": "uvicorn",
            "STREAM_WATCHDOG_INTERVAL": "12.5",
            "STREAM_WATCHDOG_RESTART_BACKOFF_BASE_SECONDS": "4",
            "STREAM_WATCHDOG_RESTART_BACKOFF_MAX_SECONDS": "90",
            "STREAM_WATCHDOG_RESTART_BACKOFF_JITTER_RATIO": "0.1",
            "STREAM_WATCHDOG_STABLE_RUN_WINDOW_SECONDS": "60",
            "DISABLE_STREAM_WORKER": "true",
            "LEADER_LEASE_ENABLED": "0",
            "LEADER_LEASE_BACKEND": "postgres",
            "LEADER_LEASE_TTL_SECONDS": "120",
            "LEADER_LEASE_RENEW_SECONDS": "45",
            "LEADER_LEASE_COLLECTION": "leases_v2",
            "ENABLE_DEMO_AUTH": "1",
            "DISABLE_CONTROL_TOWER_ROUTES": "yes",
            "DASHBOARD_AUTH_DISABLED": "true",
        }
    )

    assert cfg.app_env == "dev"
    assert cfg.runner_mode == "uvicorn"
    assert cfg.stream_watchdog_interval_seconds == 12.5
    assert cfg.stream_watchdog_restart_backoff_base_seconds == 4.0
    assert cfg.stream_watchdog_restart_backoff_max_seconds == 90.0
    assert cfg.stream_watchdog_restart_backoff_jitter_ratio == 0.1
    assert cfg.stream_watchdog_stable_run_window_seconds == 60.0
    assert cfg.disable_stream_worker is True
    assert cfg.leader_lease_enabled_override is False
    assert cfg.leader_lease_backend == "postgres"
    assert cfg.leader_lease_ttl_seconds == 120
    assert cfg.leader_lease_renew_seconds == 45
    assert cfg.leader_lease_collection == "leases_v2"
    assert cfg.enable_demo_auth is True
    assert cfg.disable_control_tower_routes is True
    assert cfg.dashboard_auth_disabled is True


def test_runtime_config_disables_demo_auth_outside_local_dev():
    cfg = RuntimeConfig.from_env(
        {
            "APP_ENV": "prod",
            "ENABLE_DEMO_AUTH": "true",
        }
    )
    assert cfg.demo_auth_requested is True
    assert cfg.enable_demo_auth is False
