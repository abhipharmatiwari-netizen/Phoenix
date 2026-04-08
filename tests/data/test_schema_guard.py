from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.data import schema_guard


def _settings(**overrides):
    base = {
        "control_plane_backend": "postgres",
        "sweep_state_backend": "firestore",
        "schema_check_mode": "warn",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_schema_guard_warn_mode_does_not_raise_on_connect_error(monkeypatch):
    monkeypatch.setattr(schema_guard, "get_control_plane_dsn", lambda _settings: "dsn")
    monkeypatch.setattr(
        schema_guard,
        "connect_with_retry",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("connect failed")),
    )
    result = schema_guard.check_startup_schema(settings=_settings(), mode="warn")
    assert "broker_accounts" in set(result.missing_tables)


def test_schema_guard_strict_mode_raises_on_connect_error(monkeypatch):
    monkeypatch.setattr(schema_guard, "get_control_plane_dsn", lambda _settings: "dsn")
    monkeypatch.setattr(
        schema_guard,
        "connect_with_retry",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("connect failed")),
    )
    with pytest.raises(RuntimeError, match="Schema guard failed"):
        schema_guard.check_startup_schema(settings=_settings(schema_check_mode="strict"), mode="strict")
