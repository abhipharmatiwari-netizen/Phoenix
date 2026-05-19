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


class _FakeCursor:
    def __init__(self, rows_by_query):
        self._rows_by_query = rows_by_query
        self._rows = []

    def execute(self, sql):
        key = "indexes" if "pg_indexes" in sql else "tables"
        self._rows = list(self._rows_by_query.get(key, []))

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _FakeConn:
    def __init__(self, *, tables, indexes):
        self._rows_by_query = {
            "tables": [(name,) for name in tables],
            "indexes": [(name,) for name in indexes],
        }

    def cursor(self):
        return _FakeCursor(self._rows_by_query)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _patch_schema_probe(monkeypatch, *, tables, indexes):
    monkeypatch.setattr(schema_guard, "get_control_plane_dsn", lambda _settings: "dsn")
    monkeypatch.setattr(
        schema_guard,
        "connect_with_retry",
        lambda *_args, **_kwargs: _FakeConn(tables=tables, indexes=indexes),
    )


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


def test_schema_guard_reports_missing_strategy_config_candidates(monkeypatch):
    tables = schema_guard._required_tables(_settings()) - {"strategy_config_candidates"}
    indexes = set(schema_guard._REQUIRED_INDEXES) - {
        "idx_strategy_config_candidates_cfg_status",
        "idx_strategy_config_candidates_created_at",
    }
    _patch_schema_probe(monkeypatch, tables=tables, indexes=indexes)

    result = schema_guard.check_startup_schema(settings=_settings(), mode="warn")

    assert "strategy_config_candidates" in result.missing_tables
    assert "idx_strategy_config_candidates_cfg_status" in result.missing_indexes
    assert "idx_strategy_config_candidates_created_at" in result.missing_indexes


def test_schema_guard_strict_mode_rejects_missing_strategy_config_candidates(monkeypatch):
    tables = schema_guard._required_tables(_settings()) - {"strategy_config_candidates"}
    indexes = set(schema_guard._REQUIRED_INDEXES)
    _patch_schema_probe(monkeypatch, tables=tables, indexes=indexes)

    with pytest.raises(RuntimeError, match="strategy_config_candidates"):
        schema_guard.check_startup_schema(
            settings=_settings(schema_check_mode="strict"),
            mode="strict",
        )
