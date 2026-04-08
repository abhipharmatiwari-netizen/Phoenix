from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.orders import trade_processed_store as store_mod


def test_build_processed_trade_store_falls_back_to_memory_when_postgres_init_fails_non_strict(
    monkeypatch,
):
    monkeypatch.setenv("ORDER_LIFECYCLE_PERSIST_MARKERS", "true")
    monkeypatch.setenv("ORDER_LIFECYCLE_PROCESSED_BACKEND", "postgres")
    monkeypatch.setenv("ORDER_LIFECYCLE_PERSIST_MARKERS_REQUIRED", "false")
    monkeypatch.setattr(
        store_mod,
        "get_settings",
        lambda: SimpleNamespace(
            control_plane_backend="postgres",
            order_lifecycle_persist_markers_required=False,
            firestore_collection_prefix="",
            gcp_project_id="",
        ),
    )
    monkeypatch.setattr(
        store_mod,
        "get_control_plane_dsn",
        lambda _settings: (_ for _ in ()).throw(RuntimeError("broken dsn")),
    )

    store = store_mod.build_processed_trade_store()
    assert isinstance(store, store_mod.InMemoryProcessedTradeStore)


def test_build_processed_trade_store_raises_when_strict_and_postgres_init_fails(
    monkeypatch,
):
    monkeypatch.setenv("ORDER_LIFECYCLE_PERSIST_MARKERS", "true")
    monkeypatch.setenv("ORDER_LIFECYCLE_PROCESSED_BACKEND", "postgres")
    monkeypatch.setenv("ORDER_LIFECYCLE_PERSIST_MARKERS_REQUIRED", "true")
    monkeypatch.setattr(
        store_mod,
        "get_settings",
        lambda: SimpleNamespace(
            control_plane_backend="postgres",
            order_lifecycle_persist_markers_required=True,
            firestore_collection_prefix="",
            gcp_project_id="",
        ),
    )
    monkeypatch.setattr(
        store_mod,
        "get_control_plane_dsn",
        lambda _settings: (_ for _ in ()).throw(RuntimeError("broken dsn")),
    )

    with pytest.raises(RuntimeError, match="backend=postgres"):
        store_mod.build_processed_trade_store()
