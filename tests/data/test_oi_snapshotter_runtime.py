from __future__ import annotations

from datetime import date

import pytest

from app.data.oi_snapshotter_runtime import (
    OiSnapshotterRuntimeConfig,
    load_runtime_config,
    run_runtime,
)


def test_runtime_config_defaults_to_disabled_and_requires_explicit_expiry():
    config = load_runtime_config(env={"OI_SNAPSHOTTER_EXPIRY": "2026-05-19"})

    assert config.enabled is False
    assert config.provider == "angel"
    assert config.underlying == "NIFTY"
    assert config.expiry == date(2026, 5, 19)


def test_runtime_config_rejects_missing_expiry_even_when_disabled():
    with pytest.raises(ValueError, match="requires --expiry"):
        load_runtime_config(env={})


def test_run_runtime_disabled_is_noop_without_login(monkeypatch):
    def _boom():
        raise AssertionError("login should not be called when disabled")

    monkeypatch.setattr(
        "app.data.oi_snapshotter_runtime.angel_login.angel_login_and_get_tokens",
        _boom,
    )
    result = run_runtime(
        OiSnapshotterRuntimeConfig(
            enabled=False,
            provider="angel",
            underlying="NIFTY",
            expiry=date(2026, 5, 19),
        )
    )

    assert result == []


def test_run_runtime_rejects_unknown_provider_before_login(monkeypatch):
    def _boom():
        raise AssertionError("login should not be called for unsupported provider")

    monkeypatch.setattr(
        "app.data.oi_snapshotter_runtime.angel_login.angel_login_and_get_tokens",
        _boom,
    )
    with pytest.raises(ValueError, match="unsupported"):
        run_runtime(
            OiSnapshotterRuntimeConfig(
                enabled=True,
                provider="unknown",
                underlying="NIFTY",
                expiry=date(2026, 5, 19),
            )
        )
