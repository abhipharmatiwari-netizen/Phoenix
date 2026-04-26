"""Tests for settings.py production-critical defaults — issue #138 / #154."""

from __future__ import annotations

import importlib
import os

import pytest


def _fresh_settings(**overrides):
    """Create a Settings instance with clean env (no PROFIT_* or other monkeypatched vars)."""
    mod = importlib.import_module("app.config.settings")
    try:
        mod.get_settings.cache_clear()
    except AttributeError:
        pass
    return mod.Settings(**overrides)


class TestProductionCriticalDefaults:
    """Verify that settings.py defaults match the LIVE compose manifest without env injection."""

    def test_hub_default_tenant_id(self, monkeypatch):
        monkeypatch.delenv("HUB_DEFAULT_TENANT_ID", raising=False)
        # Patch profit-related env vars to satisfy validators
        for k, v in [
            ("PROFIT_SWEEP_ENABLED", "false"),
            ("PROFIT_SWEEP_STRATEGY", "SIMPLE"),
            ("PROFIT_SIMPLE_ENABLED", "false"),
            ("PROFIT_DAILY_TARGET", "2000"),
            ("PROFIT_MULTIPLE_ENABLED", "false"),
            ("PROFIT_MULTIPLE_TARGET", "500"),
            ("PROFIT_SWEEP_COOLDOWN_MINUTES", "30"),
            ("PROFIT_MAX_SWEEPS_PER_DAY", "5"),
        ]:
            monkeypatch.setenv(k, v)
        s = _fresh_settings()
        assert s.hub_default_tenant_id == "tenant-1", (
            f"Expected 'tenant-1' got {s.hub_default_tenant_id!r}. "
            "If compose env injection fails, orders route to the wrong tenant."
        )

    def test_ownership_persist_pending_locks_default_true(self, monkeypatch):
        monkeypatch.delenv("OWNERSHIP_PERSIST_PENDING_LOCKS", raising=False)
        for k, v in [
            ("PROFIT_SWEEP_ENABLED", "false"),
            ("PROFIT_SWEEP_STRATEGY", "SIMPLE"),
            ("PROFIT_SIMPLE_ENABLED", "false"),
            ("PROFIT_DAILY_TARGET", "2000"),
            ("PROFIT_MULTIPLE_ENABLED", "false"),
            ("PROFIT_MULTIPLE_TARGET", "500"),
            ("PROFIT_SWEEP_COOLDOWN_MINUTES", "30"),
            ("PROFIT_MAX_SWEEPS_PER_DAY", "5"),
        ]:
            monkeypatch.setenv(k, v)
        s = _fresh_settings()
        assert s.ownership_persist_pending_locks is True, (
            "Default False means pending locks are lost on restart, allowing duplicate entries."
        )

    def test_order_lifecycle_persist_markers_required_default_true(self, monkeypatch):
        monkeypatch.delenv("ORDER_LIFECYCLE_PERSIST_MARKERS_REQUIRED", raising=False)
        for k, v in [
            ("PROFIT_SWEEP_ENABLED", "false"),
            ("PROFIT_SWEEP_STRATEGY", "SIMPLE"),
            ("PROFIT_SIMPLE_ENABLED", "false"),
            ("PROFIT_DAILY_TARGET", "2000"),
            ("PROFIT_MULTIPLE_ENABLED", "false"),
            ("PROFIT_MULTIPLE_TARGET", "500"),
            ("PROFIT_SWEEP_COOLDOWN_MINUTES", "30"),
            ("PROFIT_MAX_SWEEPS_PER_DAY", "5"),
        ]:
            monkeypatch.setenv(k, v)
        s = _fresh_settings()
        assert s.order_lifecycle_persist_markers_required is True, (
            "Default False makes lifecycle markers optional — data loss possible in LIVE."
        )

    def test_auto_strategy_min_hold_seconds_default_180(self, monkeypatch):
        monkeypatch.delenv("AUTO_STRATEGY_MIN_HOLD_SECONDS", raising=False)
        for k, v in [
            ("PROFIT_SWEEP_ENABLED", "false"),
            ("PROFIT_SWEEP_STRATEGY", "SIMPLE"),
            ("PROFIT_SIMPLE_ENABLED", "false"),
            ("PROFIT_DAILY_TARGET", "2000"),
            ("PROFIT_MULTIPLE_ENABLED", "false"),
            ("PROFIT_MULTIPLE_TARGET", "500"),
            ("PROFIT_SWEEP_COOLDOWN_MINUTES", "30"),
            ("PROFIT_MAX_SWEEPS_PER_DAY", "5"),
        ]:
            monkeypatch.setenv(k, v)
        s = _fresh_settings()
        assert s.auto_strategy_min_hold_seconds == 180.0, (
            f"Expected 180.0 got {s.auto_strategy_min_hold_seconds}. "
            "Strategies held 67% longer than compose expects."
        )

    def test_auto_strategy_max_active_per_underlying_default_2(self, monkeypatch):
        monkeypatch.delenv("AUTO_STRATEGY_MAX_ACTIVE_PER_UNDERLYING", raising=False)
        for k, v in [
            ("PROFIT_SWEEP_ENABLED", "false"),
            ("PROFIT_SWEEP_STRATEGY", "SIMPLE"),
            ("PROFIT_SIMPLE_ENABLED", "false"),
            ("PROFIT_DAILY_TARGET", "2000"),
            ("PROFIT_MULTIPLE_ENABLED", "false"),
            ("PROFIT_MULTIPLE_TARGET", "500"),
            ("PROFIT_SWEEP_COOLDOWN_MINUTES", "30"),
            ("PROFIT_MAX_SWEEPS_PER_DAY", "5"),
        ]:
            monkeypatch.setenv(k, v)
        s = _fresh_settings()
        assert s.auto_strategy_max_active_per_underlying == 2, (
            f"Expected 2 got {s.auto_strategy_max_active_per_underlying}. "
            "Half strategies activated per underlying when env missing."
        )

    def test_new_typed_fields_present(self, monkeypatch):
        for k, v in [
            ("PROFIT_SWEEP_ENABLED", "false"),
            ("PROFIT_SWEEP_STRATEGY", "SIMPLE"),
            ("PROFIT_SIMPLE_ENABLED", "false"),
            ("PROFIT_DAILY_TARGET", "2000"),
            ("PROFIT_MULTIPLE_ENABLED", "false"),
            ("PROFIT_MULTIPLE_TARGET", "500"),
            ("PROFIT_SWEEP_COOLDOWN_MINUTES", "30"),
            ("PROFIT_MAX_SWEEPS_PER_DAY", "5"),
        ]:
            monkeypatch.setenv(k, v)
        s = _fresh_settings()
        assert hasattr(s, "allow_live_capital_checks_disabled"), "#144: field must exist"
        assert hasattr(s, "capital_margin_check_mode"), "#145: field must exist"
        assert hasattr(s, "position_sync_interval_seconds"), "#145: field must exist"
        assert s.capital_margin_check_mode == "enforce"
        assert s.position_sync_interval_seconds == 90
        assert s.allow_live_capital_checks_disabled is False
