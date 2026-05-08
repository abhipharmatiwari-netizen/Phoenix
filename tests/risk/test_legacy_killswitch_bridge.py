"""Tests for issue #218: legacy ``RiskManager`` auto-trip propagation to the
durable ``KillSwitchManager``.

The bridge is implemented in ``RiskManager._propagate_kill_switch_to_durable_manager``
and called from inside the ``should_activate`` branch of
``RiskManager.evaluate_account_loss``. These tests pin the contract:

- Auto-trip on daily-loss / drawdown breach must call ``KillSwitchManager.trip``
  exactly once with ``actor='risk_manager_auto'``.
- A second auto-evaluation while already tripped must be idempotent (no
  second ``trip`` call).
- A missing / failing hub runtime must not raise into the auto-trip path.
"""

from __future__ import annotations

import datetime as dt
import importlib
from types import SimpleNamespace
from typing import List, Optional
from unittest.mock import patch

import pytest


def _build_risk_manager(tmp_path):
    mod = importlib.import_module("app.core.risk_manager")
    rm = mod.RiskManager(
        instrument_meta={"NG_FUT": {"lot_size": 1250}},
        order_client=SimpleNamespace(),
        max_daily_loss=2000.0,
        max_intraday_drawdown=2000.0,
        kill_switch_square_off_open_positions=False,
        state_path=str(tmp_path / "risk_state.json"),
    )
    return rm


class _StubKillSwitchManager:
    """Records calls; emulates the real manager's state semantics for the
    fields ``RiskManager._propagate_kill_switch_to_durable_manager`` reads.

    The bridge now consults ``get_record(scope, scope_id)`` before calling
    ``trip(...)`` so it can distinguish TRIPPED/CLEAR_PENDING (idempotent
    skip) from CLEARED (operator must rearm) from INACTIVE (proceed to
    trip). The stub mirrors that surface.
    """

    def __init__(self, *, initial_state: Optional[str] = None):
        self.trip_calls: List[dict] = []
        self.save_state_calls: int = 0
        self.save_state_should_fail: bool = False
        # ``state`` mirrors the real KillSwitchState enum's ``.value`` text
        # since the bridge compares to KillSwitchState.TRIPPED etc. We
        # store the text and translate at query time below.
        self._state_text: Optional[str] = initial_state

    def _record_or_none(self):
        if self._state_text is None:
            return None
        # Lazy import to mirror what the bridge does.
        from app.risk.kill_switch import KillSwitchState
        state_enum = KillSwitchState(self._state_text)
        return SimpleNamespace(
            id="rec-1", state=state_enum, scope=None, scope_id="GLOBAL",
            trip_reason=None, tripped_by=None,
        )

    def get_record(self, scope, scope_id):
        return self._record_or_none()

    def is_tripped(self, scope, scope_id):
        return self._state_text in {"TRIPPED", "CLEAR_PENDING"}

    def trip(self, scope, scope_id, reason, actor):
        if self._state_text in {"TRIPPED", "CLEAR_PENDING", "CLEARED"}:
            raise ValueError(
                f"Cannot trip kill switch: current state is {self._state_text}"
            )
        self._state_text = "TRIPPED"
        self.trip_calls.append(
            {"scope": scope, "scope_id": scope_id, "reason": reason, "actor": actor}
        )
        return self._record_or_none()

    def save_state(self, conn):
        self.save_state_calls += 1
        if self.save_state_should_fail:
            raise RuntimeError("simulated postgres outage")


def _force_legacy_trip_state(rm):
    """Push the legacy state into a configuration that will trip on next evaluate."""
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    now = dt.datetime.now(ist)
    rm.kill_switch_date = now.date()
    rm.daily_peak_equity = 0.0
    # Realised loss exceeding ``max_daily_loss`` (-2000) triggers ``realized_loss_hit``.
    rm.daily_realized_pnl = -2500.0
    return now


@pytest.fixture
def stub_hub_runtime():
    """Patch ``app.hub.runtime.get_hub_runtime`` to return a runtime exposing
    a stub ``KillSwitchManager``. Yields the stub for assertion.
    """
    stub_ksm = _StubKillSwitchManager()
    runtime = SimpleNamespace(kill_switch_manager=stub_ksm)
    with patch("app.hub.runtime.get_hub_runtime", return_value=runtime):
        yield stub_ksm


@pytest.fixture
def stub_postgres_save():
    """Avoid touching real Postgres in unit tests — patch the connect helper
    used by the bridge to a no-op context manager."""
    class _NoopConn:
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    with patch(
        "app.data.postgres.connect_with_retry",
        return_value=_NoopConn(),
    ), patch(
        "app.data.postgres.get_control_plane_dsn",
        return_value="postgresql://stub/stub",
    ):
        yield


def test_legacy_auto_trip_propagates_to_durable_manager(
    tmp_path, stub_hub_runtime, stub_postgres_save,
):
    rm = _build_risk_manager(tmp_path)
    now = _force_legacy_trip_state(rm)

    rm._check_kill_switch(now)

    assert rm.kill_switch_activated is True, (
        "Legacy in-memory flag must still be set"
    )
    assert len(stub_hub_runtime.trip_calls) == 1, (
        "Durable KillSwitchManager.trip must be called exactly once"
    )
    call = stub_hub_runtime.trip_calls[0]
    assert call["scope_id"] == "GLOBAL"
    assert call["actor"] == "risk_manager_auto"
    assert "risk_manager_auto" in call["reason"]


def test_legacy_auto_trip_is_idempotent_when_durable_already_tripped(
    tmp_path, stub_hub_runtime, stub_postgres_save,
):
    rm = _build_risk_manager(tmp_path)
    now = _force_legacy_trip_state(rm)

    # First trip — both legacy and durable trip.
    rm._check_kill_switch(now)
    assert len(stub_hub_runtime.trip_calls) == 1

    # Reset legacy flag to allow a second `should_activate` evaluation.
    rm.kill_switch_activated = False
    # Force the legacy state to trip again. Durable side is still TRIPPED.
    rm.daily_realized_pnl = -3000.0

    rm._check_kill_switch(now)

    # Durable side remains a single trip — bridge skipped via is_tripped check.
    assert len(stub_hub_runtime.trip_calls) == 1, (
        "Bridge must be idempotent when durable side is already tripped"
    )


def test_bridge_is_failure_safe_when_hub_runtime_unavailable(
    tmp_path, caplog,
):
    """When ``get_hub_runtime`` raises, the legacy auto-trip path must not
    raise; legacy flag still flips and an ERROR log is emitted."""
    rm = _build_risk_manager(tmp_path)
    now = _force_legacy_trip_state(rm)

    with patch(
        "app.hub.runtime.get_hub_runtime", side_effect=RuntimeError("not initialised"),
    ):
        rm._check_kill_switch(now)

    assert rm.kill_switch_activated is True, (
        "Legacy flag must still be set even if bridge fails"
    )
    assert any(
        "kill_switch_bridge_unavailable" in rec.message
        for rec in caplog.records
    ), "Bridge failure must be logged at ERROR level"


def test_bridge_is_failure_safe_when_kill_switch_manager_absent(tmp_path, caplog):
    """If the hub runtime exposes no ``kill_switch_manager`` attribute, the
    bridge must log ERROR and return without raising."""
    rm = _build_risk_manager(tmp_path)
    now = _force_legacy_trip_state(rm)

    runtime_without_ksm = SimpleNamespace()
    with patch("app.hub.runtime.get_hub_runtime", return_value=runtime_without_ksm):
        rm._check_kill_switch(now)

    assert rm.kill_switch_activated is True
    assert any(
        "kill_switch_bridge_unavailable" in rec.message
        and "KillSwitchManager not bound" in rec.message
        for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# PR #231 review (Codex) — retry + CLEARED + persistence-failure scenarios.
# ---------------------------------------------------------------------------


def test_bridge_retries_on_subsequent_evaluate_after_initial_failure(
    tmp_path, stub_postgres_save,
):
    """Codex P1: when the FIRST attempt fails (hub runtime unavailable),
    ``self.kill_switch_activated`` is already True and ``should_activate``
    will not re-fire. The bridge MUST retry on the next evaluate cycle so
    a transient outage does not leave the durable manager INACTIVE
    forever. Verified by failing the import once, then succeeding on the
    second call."""
    rm = _build_risk_manager(tmp_path)
    now = _force_legacy_trip_state(rm)

    # First evaluate: hub runtime raises. Bridge logs ERROR, returns;
    # legacy flag now True; bridge_succeeded still False.
    with patch(
        "app.hub.runtime.get_hub_runtime",
        side_effect=RuntimeError("not initialised"),
    ):
        rm._check_kill_switch(now)
    assert rm.kill_switch_activated is True
    assert rm._durable_kill_switch_bridge_succeeded is False

    # Second evaluate: hub runtime now available. The bridge retries
    # (because the legacy flag is set and bridge_succeeded is False)
    # without needing ``should_activate`` to re-fire.
    stub_ksm = _StubKillSwitchManager()
    runtime = SimpleNamespace(kill_switch_manager=stub_ksm)
    with patch("app.hub.runtime.get_hub_runtime", return_value=runtime):
        # Force a re-evaluation past the min-interval gate.
        rm._check_kill_switch(now)

    assert len(stub_ksm.trip_calls) == 1, (
        "bridge must retry trip on the next evaluate after first-attempt "
        "failure (Codex P1 / PR #231 review)"
    )
    assert rm._durable_kill_switch_bridge_succeeded is True


def test_bridge_retries_when_persistence_fails_then_succeeds(
    tmp_path,
):
    """Codex P1: the in-memory trip can succeed but Postgres save_state
    can fail. The legacy flag is already True; future ``should_activate``
    cycles will not call the bridge. Without a retry, on restart the
    in-memory durable trip is lost (load_state finds nothing) and the
    auto-trip becomes invisible to the hub OrderRouter — recreating the
    very gap this PR is supposed to close. The retry must persist on the
    next evaluate cycle."""
    rm = _build_risk_manager(tmp_path)
    now = _force_legacy_trip_state(rm)

    stub_ksm = _StubKillSwitchManager()
    stub_ksm.save_state_should_fail = True
    runtime = SimpleNamespace(kill_switch_manager=stub_ksm)

    class _NoopConn:
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    with patch("app.hub.runtime.get_hub_runtime", return_value=runtime), \
         patch("app.data.postgres.connect_with_retry", return_value=_NoopConn()), \
         patch("app.data.postgres.get_control_plane_dsn", return_value="x"):
        rm._check_kill_switch(now)

    # Trip succeeded in-memory but persistence failed.
    assert len(stub_ksm.trip_calls) == 1
    assert stub_ksm.save_state_calls == 1
    assert rm._durable_kill_switch_bridge_succeeded is False

    # Next evaluate: persistence now succeeds. Trip is already in-memory
    # so the bridge skips the trip call but RETRIES persistence.
    stub_ksm.save_state_should_fail = False
    with patch("app.hub.runtime.get_hub_runtime", return_value=runtime), \
         patch("app.data.postgres.connect_with_retry", return_value=_NoopConn()), \
         patch("app.data.postgres.get_control_plane_dsn", return_value="x"):
        rm._check_kill_switch(now)

    assert len(stub_ksm.trip_calls) == 1, (
        "bridge must NOT re-trip when in-memory record already TRIPPED"
    )
    assert stub_ksm.save_state_calls == 2, (
        "bridge MUST retry persistence on the next evaluate after first-"
        "attempt save failure (Codex P1 / PR #231 review)"
    )
    assert rm._durable_kill_switch_bridge_succeeded is True


def test_bridge_blocks_in_cleared_state_with_distinct_log(
    tmp_path, caplog, stub_postgres_save,
):
    """Codex P2: when the durable record is in CLEARED state (operator
    confirm-clear done but rearm not yet issued), a fresh legacy auto-trip
    MUST NOT silently treat that as 'already tripped' (idempotent). The
    state machine forbids a fresh trip in CLEARED. The bridge must log a
    distinct ERROR pointing at the operator action required."""
    rm = _build_risk_manager(tmp_path)
    now = _force_legacy_trip_state(rm)

    stub_ksm = _StubKillSwitchManager(initial_state="CLEARED")
    runtime = SimpleNamespace(kill_switch_manager=stub_ksm)

    with patch("app.hub.runtime.get_hub_runtime", return_value=runtime):
        rm._check_kill_switch(now)

    # Trip is NOT called (state machine forbids).
    assert len(stub_ksm.trip_calls) == 0
    # Bridge has not succeeded (durable state is not TRIPPED).
    assert rm._durable_kill_switch_bridge_succeeded is False
    # Distinct error message for the operator.
    assert any(
        "kill_switch_bridge_blocked_by_cleared_state" in rec.message
        for rec in caplog.records
    ), (
        "bridge must emit a distinct ERROR for CLEARED state — operator "
        "must rearm before legacy auto-trip can register (Codex P2)"
    )


def test_bridge_treats_existing_TRIPPED_record_as_idempotent_and_persists(
    tmp_path, stub_postgres_save,
):
    """If the durable record already exists in TRIPPED state (from a
    prior process or operator action), the bridge must NOT call
    ``trip(...)`` again but MUST still persist (covers the race where a
    same-process retry sees an in-memory trip already in place but the
    prior persistence had failed)."""
    rm = _build_risk_manager(tmp_path)
    now = _force_legacy_trip_state(rm)

    stub_ksm = _StubKillSwitchManager(initial_state="TRIPPED")
    runtime = SimpleNamespace(kill_switch_manager=stub_ksm)

    class _NoopConn:
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    with patch("app.hub.runtime.get_hub_runtime", return_value=runtime), \
         patch("app.data.postgres.connect_with_retry", return_value=_NoopConn()), \
         patch("app.data.postgres.get_control_plane_dsn", return_value="x"):
        rm._check_kill_switch(now)

    # No fresh trip call (already TRIPPED).
    assert len(stub_ksm.trip_calls) == 0
    # Persistence still attempted.
    assert stub_ksm.save_state_calls >= 1
    # Bridge marked succeeded.
    assert rm._durable_kill_switch_bridge_succeeded is True


def test_bridge_resets_success_flag_on_new_trading_day(tmp_path):
    """A fresh trading day is a new auto-trip episode. The
    bridge-success tracker must reset so a new day's first auto-trip
    propagates again (it MUST NOT be skipped on the stale 'succeeded
    yesterday' signal)."""
    rm = _build_risk_manager(tmp_path)
    now = _force_legacy_trip_state(rm)

    rm._durable_kill_switch_bridge_succeeded = True
    rm.kill_switch_activated = True

    # Force a "new day" by setting kill_switch_date to yesterday.
    rm.kill_switch_date = (now - dt.timedelta(days=1)).date()

    rm._reset_daily_if_new_day(now)

    assert rm.kill_switch_activated is False, (
        "legacy flag must reset on new day"
    )
    assert rm._durable_kill_switch_bridge_succeeded is False, (
        "durable-bridge success tracker must reset on new day so the next "
        "auto-trip episode re-propagates"
    )
