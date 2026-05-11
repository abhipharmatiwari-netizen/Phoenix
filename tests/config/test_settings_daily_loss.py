"""Issue #221: LIVE startup must fail closed when ``RISK_MAX_DAILY_LOSS``
is below ``RISK_MAX_DAILY_LOSS_LIVE_FLOOR`` (default ₹5,000).

The previous ₹2,000 committed default tripped the kill switch on a
~1.6-point adverse move on a single NG_FUT lot on 2026-05-08, making
the daily-loss limit operationally meaningless. The new gate prevents
an obvious placeholder from shipping to LIVE production.
"""

from __future__ import annotations

import pytest

from app.core.startup_config_validator import validate_runtime_startup_settings


def _runtime_settings(**overrides):
    values = {
        "eod_exit_time": "15:20",
        "eod_exit_retry_cutoff_time": "15:30",
        "hub_subscription_poll_interval": 60.0,
        "enable_capital_checks": True,
        "capital_limits_json": (
            '{"tenant-1:A1":{"max_notional_per_order":500000,"max_gross_exposure":1000000}}'
        ),
        "allow_live_capital_limits_default_only": False,
        "capital_fail_closed_on_missing_state": True,
        "capital_fail_closed_on_missing_notional_price": True,
        "enable_risk_checks": True,
        "risk_enable_daily_loss": True,
        "risk_max_daily_loss": 10000.0,
        "enable_profit_checks": True,
        "profit_enable_daily_target": True,
        "profit_daily_target": 10000.0,
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
        "hub_default_tenant_id": "tenant-1",
    }
    values.update(overrides)
    return type("Cfg", (), values)()


def _runtime_cfg(**overrides):
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
        "app_env": "production",
        "dashboard_auth_disabled": False,
    }
    values.update(overrides)
    return type("RuntimeCfg", (), values)()


def _live_env(**overrides):
    values = {
        "TRADE_MODE": "LIVE",
        "CLIENT_LOCAL_IP": "10.0.0.10",
        "CLIENT_PUBLIC_IP": "203.0.113.10",
        "MAC_ADDRESS": "02:00:00:00:00:10",
        "POSITION_SYNC_INTERVAL_SECONDS": "30",
        "ORDERS_SYNC_INTERVAL_SECONDS": "90",
        "DASHBOARD_AUTH_DISABLED": "false",
        "HUB_DEFAULT_TENANT_ID": "tenant-1",
    }
    values.update(overrides)
    return values


class TestLiveDailyLossFloor:
    """LIVE startup must reject placeholder-low RISK_MAX_DAILY_LOSS values."""

    def test_live_rejects_2000_placeholder_default(self, monkeypatch):
        """The historical ₹2,000 placeholder default must fail LIVE startup."""
        monkeypatch.delenv("RISK_MAX_DAILY_LOSS_LIVE_FLOOR", raising=False)
        with pytest.raises(ValueError, match="RISK_MAX_DAILY_LOSS >= 5000"):
            validate_runtime_startup_settings(
                settings=_runtime_settings(risk_max_daily_loss=2000.0),
                runtime_cfg=_runtime_cfg(),
                env=_live_env(),
            )

    def test_live_accepts_value_at_or_above_floor(self, monkeypatch):
        """₹10,000 (sample prod value for a small account) must pass."""
        monkeypatch.delenv("RISK_MAX_DAILY_LOSS_LIVE_FLOOR", raising=False)
        validate_runtime_startup_settings(
            settings=_runtime_settings(risk_max_daily_loss=10000.0),
            runtime_cfg=_runtime_cfg(),
            env=_live_env(),
        )

    def test_live_accepts_value_exactly_at_default_floor(self, monkeypatch):
        """A value exactly at the floor (₹5,000) must pass."""
        monkeypatch.delenv("RISK_MAX_DAILY_LOSS_LIVE_FLOOR", raising=False)
        validate_runtime_startup_settings(
            settings=_runtime_settings(risk_max_daily_loss=5000.0),
            runtime_cfg=_runtime_cfg(),
            env=_live_env(),
        )

    def test_live_rejects_value_just_below_floor(self, monkeypatch):
        """₹4,999 (just below the default floor) must fail."""
        monkeypatch.delenv("RISK_MAX_DAILY_LOSS_LIVE_FLOOR", raising=False)
        with pytest.raises(ValueError, match="RISK_MAX_DAILY_LOSS >= 5000"):
            validate_runtime_startup_settings(
                settings=_runtime_settings(risk_max_daily_loss=4999.0),
                runtime_cfg=_runtime_cfg(),
                env=_live_env(),
            )

    def test_paper_mode_accepts_low_value(self):
        """PAPER mode is unaffected — the floor only applies to LIVE."""
        env = _live_env(TRADE_MODE="PAPER")
        validate_runtime_startup_settings(
            settings=_runtime_settings(risk_max_daily_loss=100.0),
            runtime_cfg=_runtime_cfg(app_env="local"),
            env=env,
        )

    def test_live_with_custom_floor_env_var(self, monkeypatch):
        """Operators with a small account can lower the floor explicitly.

        Setting ``RISK_MAX_DAILY_LOSS_LIVE_FLOOR=1000`` accepts ₹1,500 in LIVE.
        """
        monkeypatch.setenv("RISK_MAX_DAILY_LOSS_LIVE_FLOOR", "1000")
        validate_runtime_startup_settings(
            settings=_runtime_settings(risk_max_daily_loss=1500.0),
            runtime_cfg=_runtime_cfg(),
            env=_live_env(),
        )

    def test_live_with_floor_zero_disables_gate(self, monkeypatch):
        """``RISK_MAX_DAILY_LOSS_LIVE_FLOOR=0`` explicitly disables the floor."""
        monkeypatch.setenv("RISK_MAX_DAILY_LOSS_LIVE_FLOOR", "0")
        validate_runtime_startup_settings(
            settings=_runtime_settings(risk_max_daily_loss=100.0),
            runtime_cfg=_runtime_cfg(),
            env=_live_env(),
        )

    def test_live_floor_override_via_env_mapping(self, monkeypatch):
        """Round-1 P2: ``RISK_MAX_DAILY_LOSS_LIVE_FLOOR`` in the supplied
        ``env`` mapping must be honoured even when the surrounding
        process env does not set it (used by ``lint_env_profile``)."""
        monkeypatch.delenv("RISK_MAX_DAILY_LOSS_LIVE_FLOOR", raising=False)
        validate_runtime_startup_settings(
            settings=_runtime_settings(risk_max_daily_loss=1500.0),
            runtime_cfg=_runtime_cfg(),
            env=_live_env(RISK_MAX_DAILY_LOSS_LIVE_FLOOR="1000"),
        )

    def test_live_floor_env_mapping_takes_precedence_over_process_env(self, monkeypatch):
        """When both are set, the supplied ``env`` mapping wins so the
        validator sees what the profile/lint caller intended."""
        monkeypatch.setenv("RISK_MAX_DAILY_LOSS_LIVE_FLOOR", "99999")
        # Process env says floor=99999 (would reject ₹1500); env mapping
        # says floor=1000 (would accept). The env mapping must win.
        validate_runtime_startup_settings(
            settings=_runtime_settings(risk_max_daily_loss=1500.0),
            runtime_cfg=_runtime_cfg(),
            env=_live_env(RISK_MAX_DAILY_LOSS_LIVE_FLOOR="1000"),
        )

    def test_live_with_invalid_floor_falls_back_to_default(self, monkeypatch):
        """Garbage in ``RISK_MAX_DAILY_LOSS_LIVE_FLOOR`` falls back to ₹5,000."""
        monkeypatch.setenv("RISK_MAX_DAILY_LOSS_LIVE_FLOOR", "not-a-number")
        with pytest.raises(ValueError, match="RISK_MAX_DAILY_LOSS >= 5000"):
            validate_runtime_startup_settings(
                settings=_runtime_settings(risk_max_daily_loss=2000.0),
                runtime_cfg=_runtime_cfg(),
                env=_live_env(),
            )

    def test_live_error_message_contains_runbook_pointer(self, monkeypatch):
        """The operator must be pointed at the sizing runbook."""
        monkeypatch.delenv("RISK_MAX_DAILY_LOSS_LIVE_FLOOR", raising=False)
        with pytest.raises(ValueError) as exc_info:
            validate_runtime_startup_settings(
                settings=_runtime_settings(risk_max_daily_loss=2000.0),
                runtime_cfg=_runtime_cfg(),
                env=_live_env(),
            )
        message = str(exc_info.value)
        assert "oci_live_deployment.md" in message
        assert "RISK_MAX_DAILY_LOSS_LIVE_FLOOR" in message
        assert "#221" in message
