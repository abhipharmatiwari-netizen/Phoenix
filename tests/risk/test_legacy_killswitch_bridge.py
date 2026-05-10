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


# ---------------------------------------------------------------------------
# Codex round-2 review fixes (PR #231): post-trip race re-verification +
# active-manager swap detection.
# ---------------------------------------------------------------------------


class _RaceStubKillSwitchManager(_StubKillSwitchManager):
    """Stub that mutates state between get_record() and trip() to simulate
    a concurrent operator clear race."""

    def __init__(self, *, initial_state, post_get_state):
        super().__init__(initial_state=initial_state)
        self._post_get_state = post_get_state
        self._get_record_calls = 0

    def get_record(self, scope, scope_id):
        self._get_record_calls += 1
        # First call: report initial_state. Second call onward: report the
        # raced state (covers the "post-trip re-read" path).
        if self._get_record_calls == 1:
            return super().get_record(scope, scope_id)
        # Apply the race transition.
        self._state_text = self._post_get_state
        return super().get_record(scope, scope_id)

    def trip(self, scope, scope_id, reason, actor):
        # Simulate the race outcome: trip raises ValueError because
        # somebody else moved state out of INACTIVE between our pre-trip
        # get_record and the trip call.
        self._state_text = self._post_get_state
        raise ValueError(
            f"Cannot trip kill switch: current state is {self._post_get_state}"
        )


def test_bridge_does_not_mark_succeeded_when_post_trip_race_lands_in_cleared(
    tmp_path, caplog, stub_postgres_save,
):
    """Codex P2 round 2: if a concurrent operator clear races between our
    pre-trip get_record() (which sees INACTIVE) and trip() (which sees
    CLEARED via the race), the bridge MUST NOT persist the cleared
    record and mark itself succeeded. Instead it must log a distinct
    blocked-by-cleared ERROR and leave the success flag False so future
    evaluations retry once the operator rearms."""
    rm = _build_risk_manager(tmp_path)
    now = _force_legacy_trip_state(rm)

    raced_ksm = _RaceStubKillSwitchManager(
        initial_state=None,         # pre-trip get_record sees no record (INACTIVE)
        post_get_state="CLEARED",   # trip raises ValueError; post-trip
                                    # re-read sees CLEARED
    )
    runtime = SimpleNamespace(kill_switch_manager=raced_ksm)

    with patch("app.hub.runtime.get_hub_runtime", return_value=runtime):
        rm._check_kill_switch(now)

    assert rm._durable_kill_switch_bridge_succeeded is False, (
        "bridge must NOT mark succeeded when post-trip race lands in "
        "CLEARED state (Codex round-2 P2)"
    )
    # No persistence attempted — the bridge bailed out.
    assert raced_ksm.save_state_calls == 0
    assert any(
        "kill_switch_bridge_blocked_by_cleared_state" in rec.message
        for rec in caplog.records
    )


def test_bridge_treats_post_trip_race_as_idempotent_when_lands_in_tripped(
    tmp_path, stub_postgres_save,
):
    """If the post-trip re-read sees TRIPPED (concurrent trip wins the
    race), the bridge proceeds to persistence and marks succeeded —
    same outcome as if our trip had won."""
    rm = _build_risk_manager(tmp_path)
    now = _force_legacy_trip_state(rm)

    raced_ksm = _RaceStubKillSwitchManager(
        initial_state=None,
        post_get_state="TRIPPED",
    )
    runtime = SimpleNamespace(kill_switch_manager=raced_ksm)

    class _NoopConn:
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    with patch("app.hub.runtime.get_hub_runtime", return_value=runtime), \
         patch("app.data.postgres.connect_with_retry", return_value=_NoopConn()), \
         patch("app.data.postgres.get_control_plane_dsn", return_value="x"):
        rm._check_kill_switch(now)

    assert raced_ksm.save_state_calls >= 1, (
        "post-trip TRIPPED race must still persist"
    )
    assert rm._durable_kill_switch_bridge_succeeded is True


def test_bridge_re_trips_active_manager_after_runtime_swap(
    tmp_path, stub_postgres_save,
):
    """Codex P2 round 2: AppRuntime can replace
    runtime.kill_switch_manager AFTER the bridge captures its reference.
    If the active manager (post-swap) is a different instance and is NOT
    tripped, the bridge MUST re-apply the trip on the active manager
    before declaring success — otherwise hub OrderRouter consults a
    stale untripped manager."""
    rm = _build_risk_manager(tmp_path)
    now = _force_legacy_trip_state(rm)

    # Bridge captures ksm_v1 (untripped). After save, the bridge re-resolves
    # runtime.kill_switch_manager and finds ksm_v2 (untripped because
    # AppRuntime's load_state from Postgres lost the race against our save).
    ksm_v1 = _StubKillSwitchManager()
    ksm_v2 = _StubKillSwitchManager()  # different instance, INACTIVE

    # Use a runtime container that returns ksm_v1 first, then ksm_v2 after
    # bridge has done its work. We model this by patching get_hub_runtime
    # to return runtime objects whose kill_switch_manager attribute changes.
    runtime_holder = SimpleNamespace(kill_switch_manager=ksm_v1)

    class _NoopConn:
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    # Side-effect on get_hub_runtime: first 2 calls (initial resolve +
    # any internal re-resolve before save) return the runtime with v1;
    # the post-save resolve sees v2.
    call_count = {"n": 0}

    def get_runtime():
        call_count["n"] += 1
        # Calls in order (post PR #234 review P3 fix):
        #   1. _publish_legacy_kill_switch_state_to_hub pre-throttle
        #   2. _publish_legacy_kill_switch_state_to_hub post-evaluate
        #   3. bridge initial resolve (sees ksm_v1)
        #   4. bridge post-save resolve (must see ksm_v2 — the swap)
        # Swap on call 4.
        if call_count["n"] >= 4:
            runtime_holder.kill_switch_manager = ksm_v2
        return runtime_holder

    with patch("app.hub.runtime.get_hub_runtime", side_effect=get_runtime), \
         patch("app.data.postgres.connect_with_retry", return_value=_NoopConn()), \
         patch("app.data.postgres.get_control_plane_dsn", return_value="x"):
        rm._check_kill_switch(now)

    # ksm_v1 was tripped + persisted by the bridge.
    assert len(ksm_v1.trip_calls) == 1
    # ksm_v2 was identified as the active replacement and re-tripped.
    assert len(ksm_v2.trip_calls) == 1, (
        "active-manager swap must trigger a re-trip on the post-swap "
        "instance (Codex round-2 P2)"
    )
    assert rm._durable_kill_switch_bridge_succeeded is True
