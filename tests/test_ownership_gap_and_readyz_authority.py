"""Tests for issues #107 and #108.

#107 — Startup ownership-gap detection: when non-terminal position records exist but
       no ownership records are marked RECOVERY_PENDING, AppRuntime must set
       _startup_ownership_gap_detected=True and emit an ERROR log.

#108 — /readyz position-authority gate: must return 503 when position_records_loaded > 0
       and _position_authority_restored is False, OR when ownership gap is detected.
       Must NOT gate on `unresolved_active` alone (the old, too-narrow condition).
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_runtime(
    *,
    position_records_loaded: int = 0,
    position_authority_restored: bool = False,
    ownership_gap: bool = False,
    runtime_ready: bool = True,
    trade_mode: str = "LIVE",
    unresolved_active: int = 0,
) -> MagicMock:
    rt = MagicMock()
    rt.ready = runtime_ready
    rt._position_authority_restored = position_authority_restored
    rt._position_records_loaded = position_records_loaded
    rt._startup_ownership_gap_detected = ownership_gap
    # Stream worker mocks — readyz checks these before the authority gate.
    rt.stream_worker_running.return_value = True
    rt.watchdog_running.return_value = True
    rt.stream_worker_fatal_error.return_value = None
    rt.stream_worker_status.return_value = {"running": True, "fatal_error": None}
    rt.leader_lease_status.return_value = {"enabled": True, "owned": True, "task_running": True}
    rt.hub = MagicMock()
    rt.hub.runner_count = 1
    rt.hub.running_runner_count = 1
    rt.hub.failed_runner_count = 0
    rt.hub.running = True
    rt.hub._account_runners = {}  # no runners → balance sync gate skipped
    rt.startup_recovery_status.return_value = {
        "status": "ok",
        "reason": None,
        "summary": {
            "position_records_loaded": position_records_loaded,
            "startup_ownership_gap_detected": ownership_gap,
            "unresolved_active": unresolved_active,
            "scanned": 0,
            "rehydrated": 0,
            "replayed": 0,
            "failed": 0,
        },
    }
    return rt


# ---------------------------------------------------------------------------
# #107: startup ownership-gap detection unit tests
# ---------------------------------------------------------------------------

class TestOwnershipGapDetection:
    """Unit tests for AppRuntime._mark_recovery_pending ownership-gap logic."""

    @pytest.mark.asyncio
    async def test_gap_detected_when_positions_loaded_and_zero_ownership(self):
        from app.runtime.app_runtime import AppRuntime

        rt = AppRuntime.__new__(AppRuntime)
        rt._position_records_loaded = 2
        rt._startup_ownership_gap_detected = False

        # Build a fake hub runtime with position_ownership_store that marks 0 records.
        fake_lifecycle = MagicMock()
        fake_lifecycle.mark_recovery_pending.return_value = 0

        fake_ownership = MagicMock()
        fake_ownership.mark_all_recovery_pending.return_value = 0

        fake_hub_runtime = SimpleNamespace(
            order_lifecycle=fake_lifecycle,
            position_ownership_store=fake_ownership,
        )

        with patch.object(rt, "_position_records_loaded", 2):
            await rt._mark_recovery_pending(fake_hub_runtime, trade_mode="LIVE")

        assert rt._startup_ownership_gap_detected is True

    @pytest.mark.asyncio
    async def test_no_gap_when_ownership_records_match_positions(self):
        from app.runtime.app_runtime import AppRuntime

        rt = AppRuntime.__new__(AppRuntime)
        rt._position_records_loaded = 2
        rt._startup_ownership_gap_detected = False

        fake_lifecycle = MagicMock()
        fake_lifecycle.mark_recovery_pending.return_value = 0

        fake_ownership = MagicMock()
        fake_ownership.mark_all_recovery_pending.return_value = 2  # ownership records exist

        fake_hub_runtime = SimpleNamespace(
            order_lifecycle=fake_lifecycle,
            position_ownership_store=fake_ownership,
        )

        with patch.object(rt, "_position_records_loaded", 2):
            await rt._mark_recovery_pending(fake_hub_runtime, trade_mode="LIVE")

        assert rt._startup_ownership_gap_detected is False

    @pytest.mark.asyncio
    async def test_no_gap_when_zero_positions_loaded(self):
        """Clean startup (no prior state) must not flag a gap."""
        from app.runtime.app_runtime import AppRuntime

        rt = AppRuntime.__new__(AppRuntime)
        rt._position_records_loaded = 0
        rt._startup_ownership_gap_detected = False

        fake_lifecycle = MagicMock()
        fake_lifecycle.mark_recovery_pending.return_value = 0

        fake_ownership = MagicMock()
        fake_ownership.mark_all_recovery_pending.return_value = 0

        fake_hub_runtime = SimpleNamespace(
            order_lifecycle=fake_lifecycle,
            position_ownership_store=fake_ownership,
        )

        with patch.object(rt, "_position_records_loaded", 0):
            await rt._mark_recovery_pending(fake_hub_runtime, trade_mode="LIVE")

        assert rt._startup_ownership_gap_detected is False

    @pytest.mark.asyncio
    async def test_gap_detected_emits_error_log(self, caplog):
        from app.runtime.app_runtime import AppRuntime

        rt = AppRuntime.__new__(AppRuntime)
        rt._position_records_loaded = 3
        rt._startup_ownership_gap_detected = False

        fake_lifecycle = MagicMock()
        fake_lifecycle.mark_recovery_pending.return_value = 0
        fake_ownership = MagicMock()
        fake_ownership.mark_all_recovery_pending.return_value = 0
        fake_hub_runtime = SimpleNamespace(
            order_lifecycle=fake_lifecycle,
            position_ownership_store=fake_ownership,
        )

        with caplog.at_level(logging.ERROR, logger="app.runtime.app_runtime"):
            with patch.object(rt, "_position_records_loaded", 3):
                await rt._mark_recovery_pending(fake_hub_runtime, trade_mode="LIVE")

        assert "startup.ownership_gap_detected" in caplog.text
        assert "3 non-terminal" in caplog.text

    def test_startup_recovery_status_exposes_position_records_loaded(self):
        from app.runtime.app_runtime import AppRuntime

        rt = AppRuntime.__new__(AppRuntime)
        rt._position_records_loaded = 2
        rt._startup_ownership_gap_detected = True
        rt._startup_recovery_status = {
            "status": "ok",
            "reason": None,
            "summary": {"scanned": 0, "unresolved_active": 0},
        }

        status = rt.startup_recovery_status()
        assert status["summary"]["position_records_loaded"] == 2
        assert status["summary"]["startup_ownership_gap_detected"] is True


# ---------------------------------------------------------------------------
# #108: /readyz position-authority gate tests
# ---------------------------------------------------------------------------

class TestReadyzPositionAuthorityGate:
    """
    /readyz must return 503 when:
      (a) position_records_loaded > 0 AND _position_authority_restored is False
      (b) _startup_ownership_gap_detected is True
    Must return 200 when:
      (c) position_records_loaded == 0 (clean startup)
      (d) position_records_loaded > 0 AND authority restored AND no gap
    The old condition (unresolved_active > 0) must NOT be the sole gate.
    """

    def _get_client(self, runtime_mock, trade_mode: str = "LIVE"):
        from app import server as server_module
        from app.server import app

        client = TestClient(app, raise_server_exceptions=False)
        with (
            patch.object(server_module, "get_app_runtime", return_value=runtime_mock),
            patch(
                "app.server._readiness_trade_mode",
                return_value=trade_mode,
            ),
            # Silence kill-switch and schema violation checks.
            # Avoid patching multi_instrument_stream — it triggers a BQ import error
            # in the test environment due to a google.auth version mismatch.
            patch("app.server.get_kill_switch_state", return_value={"active_count": 0, "source": "postgres"}),
            patch(
                "app.brokers.angel_client.get_balance_schema_violation_count",
                return_value=0,
            ),
        ):
            response = client.get("/readyz")
        return response

    def test_503_when_positions_loaded_and_authority_not_restored(self):
        rt = _make_runtime(
            position_records_loaded=2,
            position_authority_restored=False,
            ownership_gap=False,
            runtime_ready=True,
        )
        # patch runner count checks to pass
        rt.hub.runner_count = 1
        rt.hub.running_runner_count = 1

        from app import server as server_module
        from app.server import app

        with (
            patch.object(server_module, "get_app_runtime", return_value=rt),
            patch("app.server._readiness_trade_mode", return_value="LIVE"),
            patch("app.risk.kill_switch.get_kill_switch_state", return_value={"active_count": 0, "source": "postgres"}),
        ):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/readyz")

        assert resp.status_code == 503
        body = resp.json()
        assert body["reason"] in ("position_authority_not_restored", "startup_ownership_gap_detected")

    def test_503_when_ownership_gap_detected(self):
        rt = _make_runtime(
            position_records_loaded=2,
            position_authority_restored=True,  # authority set BUT gap present
            ownership_gap=True,
            runtime_ready=True,
        )

        from app import server as server_module
        from app.server import app

        with (
            patch.object(server_module, "get_app_runtime", return_value=rt),
            patch("app.server._readiness_trade_mode", return_value="LIVE"),
            patch("app.risk.kill_switch.get_kill_switch_state", return_value={"active_count": 0, "source": "postgres"}),
        ):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/readyz")

        assert resp.status_code == 503
        body = resp.json()
        assert body["reason"] == "startup_ownership_gap_detected"

    def test_passes_on_clean_startup_zero_positions(self):
        """Clean DB startup: 0 position records, no gap — authority gate must not block."""
        rt = _make_runtime(
            position_records_loaded=0,
            position_authority_restored=False,  # False because nothing loaded
            ownership_gap=False,
            runtime_ready=True,
        )

        from app import server as server_module
        from app.server import app

        with (
            patch.object(server_module, "get_app_runtime", return_value=rt),
            patch("app.server._readiness_trade_mode", return_value="LIVE"),
            # Patch the kill-switch import path used inside readyz
            patch(
                "app.risk.kill_switch.get_kill_switch_state",
                return_value={"active_count": 0, "source": "postgres"},
            ),
        ):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/readyz")

        # The authority gate specifically must not fire for zero position records.
        # Other gates may 503 (runners, balance sync, etc.) — we only check the authority reason.
        body = resp.json()
        assert body.get("reason") not in (
            "position_authority_not_restored",
            "startup_ownership_gap_detected",
        )

    def test_old_unresolved_active_condition_no_longer_sole_gate(self):
        """
        Old condition: `unresolved_active > 0 and not pos_authority`.
        New condition must also block when position_records_loaded > 0 and
        not pos_authority, EVEN IF unresolved_active == 0.
        """
        rt = _make_runtime(
            position_records_loaded=2,
            position_authority_restored=False,
            ownership_gap=False,
            unresolved_active=0,  # clean outbox — old gate would NOT trigger
            runtime_ready=True,
        )

        from app import server as server_module
        from app.server import app

        with (
            patch.object(server_module, "get_app_runtime", return_value=rt),
            patch("app.server._readiness_trade_mode", return_value="LIVE"),
            patch("app.risk.kill_switch.get_kill_switch_state", return_value={"active_count": 0, "source": "postgres"}),
        ):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/readyz")

        # Must be 503 — positions exist but authority not restored,
        # even though unresolved_active == 0.
        assert resp.status_code == 503
        assert resp.json()["reason"] == "position_authority_not_restored"
