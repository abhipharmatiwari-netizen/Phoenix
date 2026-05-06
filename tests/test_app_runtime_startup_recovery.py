from __future__ import annotations

import sys
from contextlib import contextmanager
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

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
    if trade_mode == "LIVE":
        # Prevent the repo-root .runtime artifact check from finding real files
        # (e.g. .backend-live.env.runtime) that exist in the local workspace.
        # The guard is tested separately; here we only want to test LIVE strict-mode logic.
        import pathlib
        monkeypatch.setattr(pathlib.Path, "iterdir", lambda self: iter([]))
        # Allow SSL skip and supply non-placeholder secrets so the inline LIVE
        # guards (SSL check, placeholder check) don't fire before we reach the
        # code under test.
        monkeypatch.setenv("LIVE_PG_SSL_SKIP_CHECK", "true")
        monkeypatch.setenv("ADMIN_API_KEY", "live-test-key-injected-by-test")
        monkeypatch.setenv("DEMO_AUTH_TOKEN_SECRET", "live-test-secret-injected-by-test")


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
    # LIVE path calls load_position_records + kill_switch restore via DB.
    _patch_db(monkeypatch)
    call_log: list[str] = []
    hub_runtime = SimpleNamespace(
        hub=_FakeHub(),
        order_lifecycle=_OrderTrackingLifecycle(call_log),
        order_router=_FakeOrderRouter({"failed": 1, "unresolved_active": 0}),
    )
    runtime = AppRuntime(
        settings_getter=lambda: _settings_live(),
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


# ---------------------------------------------------------------------------
# Startup ordering tests (Issues #64 / #67)
# ---------------------------------------------------------------------------

class _OrderTrackingHub(_FakeHub):
    """Records the order in which start_all() is called."""
    def __init__(self, call_log: list) -> None:
        super().__init__()
        self._call_log = call_log

    async def start_all(self) -> None:
        self._call_log.append("hub.start_all")
        self.started = True


class _OrderTrackingLifecycle(_FakeOrderLifecycle):
    """Records calls to load_position_records."""
    def __init__(self, call_log: list) -> None:
        super().__init__()
        self._call_log = call_log

    def load_position_records(self, conn) -> int:
        self._call_log.append("lifecycle.load_position_records")
        return 0


def _fake_connect_ctx(call_log=None):
    """Context manager that returns a dummy connection."""
    @contextmanager
    def _ctx(dsn, autocommit=False):
        yield MagicMock()
    return _ctx


def _patch_db(monkeypatch):
    monkeypatch.setattr(app_runtime_module, "get_control_plane_dsn", lambda *a, **kw: "dsn://test")
    monkeypatch.setattr(app_runtime_module, "connect_with_retry", _fake_connect_ctx())


@pytest.mark.asyncio
async def test_startup_restore_happens_before_hub_start_all(monkeypatch):
    """Regression #64: position restore must complete before hub.start_all()."""
    _patch_runtime_dependencies(monkeypatch, strict_mode=False)
    _patch_db(monkeypatch)

    call_log: list[str] = []
    hub_runtime = SimpleNamespace(
        hub=_OrderTrackingHub(call_log),
        order_lifecycle=_OrderTrackingLifecycle(call_log),
        order_router=_FakeOrderRouter({"failed": 0, "unresolved_active": 0}),
    )
    runtime = AppRuntime(
        settings_getter=lambda: _settings(),
        hub_runtime_getter=lambda: hub_runtime,
    )

    await runtime.start()

    assert "lifecycle.load_position_records" in call_log, "load_position_records not called"
    assert "hub.start_all" in call_log, "hub.start_all not called"
    pos_idx = call_log.index("lifecycle.load_position_records")
    hub_idx = call_log.index("hub.start_all")
    assert pos_idx < hub_idx, (
        f"load_position_records (idx={pos_idx}) must happen before hub.start_all "
        f"(idx={hub_idx}). Actual log: {call_log}"
    )

    await runtime.stop()


@pytest.mark.asyncio
async def test_mark_recovery_pending_before_hub_start_all(monkeypatch):
    """Regression #64: mark_all_recovery_pending must be called before hub.start_all()."""
    _patch_runtime_dependencies(monkeypatch, strict_mode=False)
    _patch_db(monkeypatch)

    call_log: list[str] = []

    class _TrackingOwnershipStore:
        def mark_all_recovery_pending(self) -> int:
            call_log.append("ownership.mark_all_recovery_pending")
            return 0

    hub_runtime = SimpleNamespace(
        hub=_OrderTrackingHub(call_log),
        order_lifecycle=_OrderTrackingLifecycle(call_log),
        order_router=_FakeOrderRouter({"failed": 0, "unresolved_active": 0}),
        position_ownership_store=_TrackingOwnershipStore(),
    )
    runtime = AppRuntime(
        settings_getter=lambda: _settings(),
        hub_runtime_getter=lambda: hub_runtime,
    )

    await runtime.start()

    assert "ownership.mark_all_recovery_pending" in call_log
    mark_idx = call_log.index("ownership.mark_all_recovery_pending")
    hub_idx = call_log.index("hub.start_all")
    assert mark_idx < hub_idx, (
        f"mark_all_recovery_pending (idx={mark_idx}) must happen before hub.start_all "
        f"(idx={hub_idx}). Actual log: {call_log}"
    )

    await runtime.stop()


def _settings_live():
    return SimpleNamespace(
        app_runtime_startup_validate=False,
        schema_check_mode="warn",
        enable_multi_hub=True,
        order_lifecycle_persist_markers_required=False,
        control_plane_pg_sslmode="require",
        control_plane_pg_password=None,
    )


@pytest.mark.asyncio
async def test_kill_switch_load_failure_is_fatal_in_live(monkeypatch):
    """Regression #67: kill-switch restore failure must abort startup in LIVE."""
    _patch_runtime_dependencies(monkeypatch, strict_mode=False, trade_mode="LIVE")
    _patch_db(monkeypatch)

    # Patch out the env.runtime artifact check and LIVE placeholder checks.
    import pathlib
    monkeypatch.setattr(pathlib.Path, "iterdir", lambda self: iter([]))
    monkeypatch.setenv("ADMIN_API_KEY", "real-key")
    monkeypatch.setenv("DEMO_AUTH_TOKEN_SECRET", "real-secret")

    class _BrokenKillSwitchManager:
        @staticmethod
        def load_state(conn):
            raise RuntimeError("DB unavailable: kill switch table missing")

    fake_ks_module = ModuleType("app.risk.kill_switch")
    fake_ks_module.KillSwitchManager = _BrokenKillSwitchManager
    monkeypatch.setitem(sys.modules, "app.risk.kill_switch", fake_ks_module)

    _call_log: list[str] = []
    hub_runtime = SimpleNamespace(
        hub=_FakeHub(),
        order_lifecycle=_OrderTrackingLifecycle(_call_log),  # has load_position_records
        order_router=_FakeOrderRouter({"failed": 0, "unresolved_active": 0}),
        kill_switch_manager=object(),  # non-None triggers the load path
    )
    runtime = AppRuntime(
        settings_getter=lambda: _settings_live(),
        hub_runtime_getter=lambda: hub_runtime,
    )

    with pytest.raises(RuntimeError, match="DB unavailable"):
        await runtime.start()


@pytest.mark.asyncio
async def test_position_restore_failure_is_fatal_in_live(monkeypatch):
    """Regression #67: position records restore failure must abort startup in LIVE."""
    _patch_runtime_dependencies(monkeypatch, strict_mode=False, trade_mode="LIVE")
    _patch_db(monkeypatch)

    import pathlib
    monkeypatch.setattr(pathlib.Path, "iterdir", lambda self: iter([]))
    monkeypatch.setenv("ADMIN_API_KEY", "real-key")
    monkeypatch.setenv("DEMO_AUTH_TOKEN_SECRET", "real-secret")

    class _BrokenLifecycle(_FakeOrderLifecycle):
        def load_position_records(self, conn) -> int:
            raise RuntimeError("position_records table missing")

    hub_runtime = SimpleNamespace(
        hub=_FakeHub(),
        order_lifecycle=_BrokenLifecycle(),
        order_router=_FakeOrderRouter({"failed": 0, "unresolved_active": 0}),
    )
    runtime = AppRuntime(
        settings_getter=lambda: _settings_live(),
        hub_runtime_getter=lambda: hub_runtime,
    )

    with pytest.raises(RuntimeError, match="position_records table missing"):
        await runtime.start()
