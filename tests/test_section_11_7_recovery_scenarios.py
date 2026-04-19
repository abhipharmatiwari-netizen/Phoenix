"""§11.7 failure-path tests — PHX-QA-ARCH-11.7

Tests that the startup recovery gate behaves correctly under the failure
scenarios that ARCHITECTURE.md §11.7 requires Phoenix to pass before
live deployment:

  1. Crash after broker submit but before durable lifecycle persistence
     → outbox has failed entries; strict LIVE mode must block entries
  2. Crash after durable persistence but before broker response handling
     → outbox has unresolved_active entries; LIVE strict mode must block
  3. Lease loss during pending order lifecycle processing
     → runtime.ready must return False when lease is not owned
  4. Startup with open broker positions outside the current option universe
     → position_ownership_store.mark_all_recovery_pending() must be called
        so unknown positions are flagged before reconciliation
  5. Partial-fill recovery with stale / ambiguous broker snapshots
     → outbox recovery reporting partial fills is treated as unresolved
        and blocks entries in strict mode
  6. Restart with pending outbox items, ownership locks, kill-switch state,
     and EOD markers present — runtime must complete startup and block new
     entries until recovery is resolved
"""
from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.runtime import app_runtime as app_runtime_module
from app.runtime.app_runtime import AppRuntime


# ---------------------------------------------------------------------------
# Shared fixtures / helpers (mirrors test_app_runtime_startup_recovery.py)
# ---------------------------------------------------------------------------

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
        self.mark_recovery_called = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        pass

    def mark_recovery_pending(self) -> None:
        self.mark_recovery_called = True


class _FakeOrderRouter:
    def __init__(self, recovery_summary: dict) -> None:
        self._recovery_summary = dict(recovery_summary)
        self.block_new_entries = False
        self.reason = None
        self.summary: dict = {}

    async def recover_submission_outbox(self):
        return dict(self._recovery_summary)

    def set_startup_recovery_gate(self, *, block_new_entries: bool, reason=None, summary=None):
        self.block_new_entries = bool(block_new_entries)
        self.reason = reason
        self.summary = dict(summary or {})


class _FakeOwnershipStore:
    def __init__(self) -> None:
        self.mark_called = False

    def mark_all_recovery_pending(self) -> None:
        self.mark_called = True


def _boot_config(trade_mode: str = "LIVE"):
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


def _install_bq_stubs(monkeypatch):
    async_writer = ModuleType("app.data.bq_async_writer")
    async_writer.start_global_writer = lambda client_getter: None
    async_writer.stop_global_writer = lambda: None
    persister = ModuleType("app.data.bq_persister")
    persister.get_bq_client = lambda: None
    monkeypatch.setitem(sys.modules, "app.data.bq_async_writer", async_writer)
    monkeypatch.setitem(sys.modules, "app.data.bq_persister", persister)


def _patch_deps(monkeypatch, *, trade_mode: str = "LIVE", feature_flag_strict: bool = False):
    monkeypatch.setattr(
        app_runtime_module,
        "initialize_boot_config",
        lambda force=True: _boot_config(trade_mode=trade_mode),
    )
    monkeypatch.setattr(
        app_runtime_module, "check_startup_schema",
        lambda settings, mode: SimpleNamespace(missing_tables=[], missing_indexes=[]),
    )
    monkeypatch.setattr(app_runtime_module, "validate_runtime_startup_settings", lambda **kw: None)
    monkeypatch.setattr(app_runtime_module, "validate_startup_config", lambda **kw: None)
    monkeypatch.setattr(
        app_runtime_module,
        "load_stability_feature_flags",
        lambda log=None: SimpleNamespace(order_lifecycle_strict_startup_recovery=feature_flag_strict),
    )
    monkeypatch.setattr(app_runtime_module, "log_stability_feature_flags", lambda flags, log=None: None)
    _install_bq_stubs(monkeypatch)


# ---------------------------------------------------------------------------
# §11.7 scenario 1: Crash after submit, before durable persistence
#   Outbox recovery reports failed > 0 → LIVE strict mode must block entries.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_crash_after_submit_before_persistence_blocks_live_entries(monkeypatch):
    """§11.7 #1: Outbox has failed entries — LIVE strict mode must block new entries."""
    _patch_deps(monkeypatch, trade_mode="LIVE")

    hub_runtime = SimpleNamespace(
        hub=_FakeHub(),
        order_lifecycle=_FakeOrderLifecycle(),
        order_router=_FakeOrderRouter({"failed": 2, "unresolved_active": 0, "replayed": 0}),
        position_ownership_store=_FakeOwnershipStore(),
    )
    runtime = AppRuntime(
        settings_getter=lambda: _settings(),
        hub_runtime_getter=lambda: hub_runtime,
    )

    await runtime.start()

    assert hub_runtime.order_router.block_new_entries is True, (
        "Failed outbox entries in LIVE mode must block new order entries"
    )
    assert runtime.startup_recovery_status()["status"] == "degraded"

    await runtime.stop()


# ---------------------------------------------------------------------------
# §11.7 scenario 2: Crash after persistence, before broker response handling
#   Outbox has unresolved_active entries → LIVE strict mode must block.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_crash_after_persistence_before_broker_response_blocks_live_entries(monkeypatch):
    """§11.7 #2: Unresolved active outbox entries — LIVE must block new entries."""
    _patch_deps(monkeypatch, trade_mode="LIVE")

    hub_runtime = SimpleNamespace(
        hub=_FakeHub(),
        order_lifecycle=_FakeOrderLifecycle(),
        order_router=_FakeOrderRouter({"failed": 0, "unresolved_active": 3, "replayed": 0}),
        position_ownership_store=_FakeOwnershipStore(),
    )
    runtime = AppRuntime(
        settings_getter=lambda: _settings(),
        hub_runtime_getter=lambda: hub_runtime,
    )

    await runtime.start()

    assert hub_runtime.order_router.block_new_entries is True, (
        "Unresolved active outbox entries in LIVE mode must block new entries"
    )
    assert runtime.startup_recovery_status()["status"] == "degraded"

    await runtime.stop()


# ---------------------------------------------------------------------------
# §11.7 scenario 3: Lease loss during pending lifecycle processing
#   runtime.ready must return False when lease is not owned.
# ---------------------------------------------------------------------------

def test_lease_loss_makes_runtime_not_ready():
    """§11.7 #3: Leader lease loss must make runtime.ready return False."""
    runtime = AppRuntime(
        settings_getter=lambda: SimpleNamespace(
            app_runtime_startup_validate=False,
            schema_check_mode="warn",
            enable_multi_hub=True,
            order_lifecycle_persist_markers_required=False,
        ),
        hub_runtime_getter=lambda: SimpleNamespace(
            hub=_FakeHub(),
            order_lifecycle=_FakeOrderLifecycle(),
            order_router=_FakeOrderRouter({}),
        ),
    )
    # Simulate lease registered and not owned.
    runtime._ready = True  # startup completed
    mock_lease = MagicMock()
    mock_lease.__bool__ = MagicMock(return_value=True)
    runtime._leader_lease = mock_lease
    runtime._is_leader = False  # lease lost

    assert runtime.ready is False, (
        "runtime.ready must be False when leader lease is not owned"
    )


def test_lease_owned_allows_ready():
    """§11.7 #3 complement: runtime.ready is True when lease is owned and latch set."""
    runtime = AppRuntime(
        settings_getter=lambda: SimpleNamespace(
            app_runtime_startup_validate=False,
            schema_check_mode="warn",
            enable_multi_hub=True,
            order_lifecycle_persist_markers_required=False,
        ),
        hub_runtime_getter=lambda: SimpleNamespace(
            hub=_FakeHub(),
            order_lifecycle=_FakeOrderLifecycle(),
            order_router=_FakeOrderRouter({}),
        ),
    )
    runtime._ready = True
    mock_lease = MagicMock()
    mock_lease.__bool__ = MagicMock(return_value=True)
    runtime._leader_lease = mock_lease
    runtime._is_leader = True  # owns lease

    assert runtime.ready is True


# ---------------------------------------------------------------------------
# §11.7 scenario 4: Startup with open positions outside current universe
#   position_ownership_store.mark_all_recovery_pending() must fire so that
#   unknown positions are flagged RECOVERY_PENDING before reconciliation.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_startup_marks_position_ownership_pending_before_reconciliation(monkeypatch):
    """§11.7 #4: mark_all_recovery_pending() fires before outbox recovery."""
    _patch_deps(monkeypatch, trade_mode="LIVE")
    call_order: list[str] = []

    class _TrackingOwnershipStore:
        def mark_all_recovery_pending(self) -> None:
            call_order.append("ownership_pending")

    class _TrackingOrderRouter(_FakeOrderRouter):
        async def recover_submission_outbox(self):
            call_order.append("outbox_recovery")
            return {"failed": 0, "unresolved_active": 0}

    hub_runtime = SimpleNamespace(
        hub=_FakeHub(),
        order_lifecycle=_FakeOrderLifecycle(),
        order_router=_TrackingOrderRouter({}),
        position_ownership_store=_TrackingOwnershipStore(),
    )
    runtime = AppRuntime(
        settings_getter=lambda: _settings(),
        hub_runtime_getter=lambda: hub_runtime,
    )

    await runtime.start()

    assert "ownership_pending" in call_order, "mark_all_recovery_pending must be called"
    assert "outbox_recovery" in call_order, "outbox recovery must also run"
    ownership_idx = call_order.index("ownership_pending")
    outbox_idx = call_order.index("outbox_recovery")
    assert ownership_idx < outbox_idx, (
        "mark_all_recovery_pending must fire BEFORE outbox recovery (§11.1 ordering)"
    )

    await runtime.stop()


# ---------------------------------------------------------------------------
# §11.7 scenario 5: Partial-fill recovery with stale/ambiguous snapshots
#   Outbox recovery treating deferred/partial fills as unresolved_active
#   must block entries in LIVE strict mode.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_partial_fill_reported_as_unresolved_blocks_live_entries(monkeypatch):
    """§11.7 #5: Partial fills flagged as unresolved_active must block entries in LIVE."""
    _patch_deps(monkeypatch, trade_mode="LIVE")

    hub_runtime = SimpleNamespace(
        hub=_FakeHub(),
        order_lifecycle=_FakeOrderLifecycle(),
        order_router=_FakeOrderRouter({
            "failed": 0,
            "unresolved_active": 1,  # partial fill not yet resolved
            "replayed": 2,
            "deferred": 1,
        }),
        position_ownership_store=_FakeOwnershipStore(),
    )
    runtime = AppRuntime(
        settings_getter=lambda: _settings(),
        hub_runtime_getter=lambda: hub_runtime,
    )

    await runtime.start()

    assert hub_runtime.order_router.block_new_entries is True, (
        "Partial fills (unresolved_active=1) in LIVE must block new entries"
    )

    await runtime.stop()


# ---------------------------------------------------------------------------
# §11.7 scenario 6: Restart with pending outbox items, ownership locks,
#   kill-switch state, and EOD markers present.
#   All recovery hooks must fire; status must be degraded in strict mode.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_restart_with_pending_state_blocks_entries_until_resolved(monkeypatch):
    """§11.7 #6: Full restart with pending outbox + ownership in LIVE must degrade."""
    _patch_deps(monkeypatch, trade_mode="LIVE")

    ownership_store = _FakeOwnershipStore()
    lifecycle = _FakeOrderLifecycle()

    hub_runtime = SimpleNamespace(
        hub=_FakeHub(),
        order_lifecycle=lifecycle,
        order_router=_FakeOrderRouter({
            "failed": 1,
            "unresolved_active": 2,
            "replayed": 5,
        }),
        position_ownership_store=ownership_store,
    )
    runtime = AppRuntime(
        settings_getter=lambda: _settings(),
        hub_runtime_getter=lambda: hub_runtime,
    )

    await runtime.start()

    # All recovery hooks fired
    assert ownership_store.mark_called is True, "Ownership RECOVERY_PENDING must be marked"
    assert lifecycle.started is True, "Order lifecycle must have started"

    # Gate is engaged
    assert hub_runtime.order_router.block_new_entries is True
    status = runtime.startup_recovery_status()
    assert status["status"] == "degraded"

    await runtime.stop()
