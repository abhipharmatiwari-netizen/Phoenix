"""Tests for feature flag versioning and rollback (H2)."""

import pytest

from app.core.feature_flags import (
    FeatureFlagVersionStore,
    StabilityFeatureFlags,
    load_stability_feature_flags,
)


def _make_flags(**overrides) -> StabilityFeatureFlags:
    defaults = {
        "strategy_bridge_mode": "per_call_loop",
        "strategy_bridge_timeout_s": 2.5,
        "strategy_bridge_max_inflight": 50,
        "broker_schema_check_mode": "off",
        "order_place_timeout_safe_mode": False,
        "tenant_cooldown_local_clear_only": False,
        "order_lifecycle_strict_startup_recovery": False,
        "order_snapshot_preserve_terminal": False,
        "order_lifecycle_use_broker_snapshot": False,
        "option_atm_refresh_reconcile_open_legs": False,
        "capital_margin_check_mode": "off",
        "angel_postback_auth_mode": "legacy",
        "dashboard_fix_recent_trades": False,
        "executed_tokens_persist_enabled": False,
        "executed_tokens_state_path": "logs/executed_tokens_state.json",
        "executed_tokens_state_flush_interval_s": 10.0,
        "clock_adoption": "dependency_injected_system_default",
    }
    defaults.update(overrides)
    return StabilityFeatureFlags(**defaults)


class TestFeatureFlagVersionStore:
    def test_initial_version_is_zero(self):
        store = FeatureFlagVersionStore()
        assert store.version == 0
        assert store.current() is None

    def test_load_increments_version(self):
        store = FeatureFlagVersionStore()
        flags = _make_flags()
        v = store.load(flags, actor="test")
        assert v == 1
        assert store.version == 1
        assert store.current() is flags

    def test_multiple_loads_increment(self):
        store = FeatureFlagVersionStore()
        store.load(_make_flags(), actor="v1")
        store.load(_make_flags(broker_schema_check_mode="warn"), actor="v2")
        assert store.version == 2

    def test_history_tracks_changes(self):
        store = FeatureFlagVersionStore()
        store.load(_make_flags(), actor="init")
        store.load(_make_flags(broker_schema_check_mode="strict"), actor="upgrade")
        history = store.history()
        assert len(history) == 2
        assert history[0].version == 2  # newest first
        assert history[0].actor == "upgrade"
        assert history[1].version == 1

    def test_history_records_before_after(self):
        store = FeatureFlagVersionStore()
        store.load(_make_flags(broker_schema_check_mode="off"), actor="v1")
        store.load(_make_flags(broker_schema_check_mode="strict"), actor="v2")
        record = store.history()[0]  # v2
        assert record.before["broker_schema_check_mode"] == "off"
        assert record.after["broker_schema_check_mode"] == "strict"

    def test_rollback_restores_previous_version(self):
        store = FeatureFlagVersionStore()
        store.load(_make_flags(broker_schema_check_mode="off"), actor="v1")
        store.load(_make_flags(broker_schema_check_mode="strict"), actor="v2")
        restored = store.rollback(1, actor="operator")
        assert restored.broker_schema_check_mode == "off"
        assert store.version == 3  # rollback creates new version

    def test_rollback_unknown_version_raises(self):
        store = FeatureFlagVersionStore()
        store.load(_make_flags(), actor="v1")
        with pytest.raises(ValueError, match="not found"):
            store.rollback(99)

    def test_history_limit(self):
        store = FeatureFlagVersionStore()
        for i in range(5):
            store.load(_make_flags(), actor=f"v{i}")
        assert len(store.history(limit=3)) == 3
