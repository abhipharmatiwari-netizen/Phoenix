from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

from app.runtime import app_runtime as app_runtime_module
from app.runtime.app_runtime import AppRuntime


class _FakeHub:
    def __init__(self) -> None:
        self.initialized = False
        self.started = False
        self.stopped = False

    async def initialize(self) -> None:
        self.initialized = True

    async def start_all(self) -> None:
        self.started = True

    async def stop_all(self) -> None:
        self.stopped = True

    async def wait_for_runner_startup(self) -> None:
        return


class _FakeOrderLifecycle:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


class _FakeOrderRouter:
    def __init__(self, recovery_summary: dict[str, object]) -> None:
        self._recovery_summary = dict(recovery_summary)
        self.block_new_entries = False
        self.reason = None
        self.summary = {}

    async def recover_submission_outbox(self):
        return dict(self._recovery_summary)

    def set_startup_recovery_gate(self, *, block_new_entries: bool, reason=None, summary=None):
        self.block_new_entries = bool(block_new_entries)
        self.reason = reason
        self.summary = dict(summary or {})


def _boot_config(trade_mode: str = "PAPER"):
    return SimpleNamespace(
        runtime=SimpleNamespace(
            disable_stream_worker=True,
            leader_lease_enabled_override=False,
            leader_lease_id="lease-id",
            leader_lease_ttl_seconds=30,
            leader_lease_renew_seconds=10,
            leader_lease_collection="leader_lease",
        ),
        env={"TRADE_MODE": trade_mode},
        strategy_env={},
        to_log_dict=lambda: {},
    )


def _settings():
    return SimpleNamespace(
        app_runtime_startup_validate=False,
        schema_check_mode="warn",
        enable_multi_hub=True,
        order_lifecycle_persist_markers_required=False,
    )


def _schema_result():
    return SimpleNamespace(missing_tables=[], missing_indexes=[])


def _install_bq_stubs(monkeypatch):
    async_writer = ModuleType("app.data.bq_async_writer")
    async_writer.start_global_writer = lambda client_getter: None
    async_writer.stop_global_writer = lambda: None
    persister = ModuleType("app.data.bq_persister")
    persister.get_bq_client = lambda: None
    monkeypatch.setitem(sys.modules, "app.data.bq_async_writer", async_writer)
    monkeypatch.setitem(sys.modules, "app.data.bq_persister", persister)


def _patch_runtime_dependencies(monkeypatch, *, strict_mode: bool, trade_mode: str = "PAPER"):
    monkeypatch.setattr(
        app_runtime_module,
        "initialize_boot_config",
        lambda force=True: _boot_config(trade_mode=trade_mode),
    )
    monkeypatch.setattr(app_runtime_module, "check_startup_schema", lambda settings, mode: _schema_result())
    monkeypatch.setattr(app_runtime_module, "validate_runtime_startup_settings", lambda **kwargs: None)
    monkeypatch.setattr(app_runtime_module, "validate_startup_config", lambda **kwargs: None)
    monkeypatch.setattr(
        app_runtime_module,
        "load_stability_feature_flags",
        lambda log=None: SimpleNamespace(
            order_lifecycle_strict_startup_recovery=strict_mode,
        ),
    )
    monkeypatch.setattr(app_runtime_module, "log_stability_feature_flags", lambda flags, log=None: None)
    _install_bq_stubs(monkeypatch)


@pytest.mark.asyncio
async def test_app_runtime_marks_startup_recovery_degraded_in_strict_mode(monkeypatch):
    _patch_runtime_dependencies(monkeypatch, strict_mode=True)
    hub_runtime = SimpleNamespace(
        hub=_FakeHub(),
        order_lifecycle=_FakeOrderLifecycle(),
        order_router=_FakeOrderRouter({"failed": 1, "unresolved_active": 0}),
    )
    runtime = AppRuntime(
        settings_getter=lambda: _settings(),
        hub_runtime_getter=lambda: hub_runtime,
    )

    await runtime.start()

    assert hub_runtime.hub.initialized is True
    assert hub_runtime.hub.started is True
    assert hub_runtime.order_lifecycle.started is True
    assert hub_runtime.order_router.block_new_entries is True
    assert runtime.startup_recovery_status()["status"] == "degraded"

    await runtime.stop()


@pytest.mark.asyncio
async def test_mark_recovery_pending_calls_position_ownership_store(monkeypatch):
    """Regression: position_ownership_store (not position_ownership) must be called."""
    _patch_runtime_dependencies(monkeypatch, strict_mode=False)

    called = []

    class _FakeOwnershipStore:
        def mark_all_recovery_pending(self) -> None:
            called.append(True)

    hub_runtime = SimpleNamespace(
        hub=_FakeHub(),
        order_lifecycle=_FakeOrderLifecycle(),
        order_router=_FakeOrderRouter({"failed": 0, "unresolved_active": 0}),
        position_ownership_store=_FakeOwnershipStore(),
    )
    runtime = AppRuntime(
        settings_getter=lambda: _settings(),
        hub_runtime_getter=lambda: hub_runtime,
    )

    await runtime.start()

    assert called, "position_ownership_store.mark_all_recovery_pending() was never called"

    await runtime.stop()


@pytest.mark.asyncio
async def test_live_trade_mode_forces_strict_mode_regardless_of_feature_flag(monkeypatch):
    """Regression: TRADE_MODE=LIVE must force strict_mode=True even if feature flag is False."""
    _patch_runtime_dependencies(monkeypatch, strict_mode=False, trade_mode="LIVE")
    hub_runtime = SimpleNamespace(
        hub=_FakeHub(),
        order_lifecycle=_FakeOrderLifecycle(),
        order_router=_FakeOrderRouter({"failed": 1, "unresolved_active": 0}),
    )
    runtime = AppRuntime(
        settings_getter=lambda: _settings(),
        hub_runtime_getter=lambda: hub_runtime,
    )

    await runtime.start()

    assert hub_runtime.order_router.block_new_entries is True, (
        "LIVE mode with failed outbox recovery must block entries (strict_mode forced True)"
    )
    assert runtime.startup_recovery_status()["status"] == "degraded"

    await runtime.stop()


@pytest.mark.asyncio
async def test_app_runtime_keeps_startup_recovery_ok_when_strict_mode_off(monkeypatch):
    _patch_runtime_dependencies(monkeypatch, strict_mode=False)
    hub_runtime = SimpleNamespace(
        hub=_FakeHub(),
        order_lifecycle=_FakeOrderLifecycle(),
        order_router=_FakeOrderRouter({"failed": 1, "unresolved_active": 2}),
    )
    runtime = AppRuntime(
        settings_getter=lambda: _settings(),
        hub_runtime_getter=lambda: hub_runtime,
    )

    await runtime.start()

    assert hub_runtime.hub.initialized is True
    assert hub_runtime.hub.started is True
    assert hub_runtime.order_lifecycle.started is True
    assert hub_runtime.order_router.block_new_entries is False
    assert runtime.startup_recovery_status()["status"] == "ok"

    await runtime.stop()
