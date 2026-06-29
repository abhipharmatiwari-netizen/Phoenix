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
import json
import os
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


def test_position_sync_does_not_retrip_after_durable_clear_when_flat_and_persisted_inactive(
    tmp_path,
):
    """Regression for the 2026-06-29 re-trip: a stale in-memory legacy flag
    must not recreate a durable GLOBAL trip after the durable state has been
    cleared and persisted risk + state-store evidence are flat.
    """
    rm = _build_risk_manager(tmp_path)
    rm.broker_account_id = "ba1"
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    now = dt.datetime.now(ist)
    rm.kill_switch_date = now.date()
    rm.kill_switch_activated = True
    rm._durable_kill_switch_bridge_succeeded = False
    rm.daily_realized_pnl = -2500.0
    rm.daily_peak_equity = 0.0
    rm.last_total_pnl = -2500.0
    rm.daily_peak_total_pnl = 0.0
    rm.state_path.write_text(
        json.dumps(
            {
                "realized_pnl": 0.0,
                "max_equity": 0.0,
                "daily_realized_pnl": 0.0,
                "daily_peak_equity": 0.0,
                "daily_peak_total_pnl": 0.0,
                "last_unrealized_pnl": 0.0,
                "last_total_pnl": 0.0,
                "kill_switch_activated": False,
                "kill_switch_date": now.date().isoformat(),
                "open_positions": {},
                "open_spreads": {},
            }
        ),
        encoding="utf-8",
    )

    stub_ksm = _StubKillSwitchManager()
    state_store = SimpleNamespace(
        get_positions=lambda account_id: [],
        get_order_snapshot=lambda account_id: [],
        get_orders=lambda account_id: [],
    )
    runtime = SimpleNamespace(
        kill_switch_manager=stub_ksm,
        state_store=state_store,
        get_legacy_kill_switch_snapshot=lambda: {
            "publisher_seen": True,
            "legacy_active": False,
            "legacy_reason": "legacy_risk_manager_inactive",
        },
    )

    with patch("app.hub.runtime.get_hub_runtime", return_value=runtime):
        result = rm.evaluate_account_loss(
            now=now,
            source="position_sync",
            force=True,
        )

    assert stub_ksm.trip_calls == []
    assert result["kill_switch_activated"] is False
    assert rm.kill_switch_activated is False
    assert rm._durable_kill_switch_bridge_succeeded is True
    assert rm.daily_realized_pnl == 0.0


def test_position_sync_retries_truly_active_legacy_once_without_loop(
    tmp_path,
    stub_postgres_save,
):
    rm = _build_risk_manager(tmp_path)
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    now = dt.datetime.now(ist)
    rm.kill_switch_date = now.date()
    rm.kill_switch_activated = True
    rm._durable_kill_switch_bridge_succeeded = False
    rm.daily_realized_pnl = 0.0

    stub_ksm = _StubKillSwitchManager()
    runtime = SimpleNamespace(
        kill_switch_manager=stub_ksm,
        get_legacy_kill_switch_snapshot=lambda: {
            "publisher_seen": True,
            "legacy_active": True,
            "legacy_reason": "legacy_risk_manager_active",
        },
    )

    with patch("app.hub.runtime.get_hub_runtime", return_value=runtime):
        rm.evaluate_account_loss(now=now, source="position_sync", force=True)
        rm.evaluate_account_loss(now=now, source="position_sync", force=True)

    assert len(stub_ksm.trip_calls) == 1
    assert stub_ksm.trip_calls[0]["actor"] == "risk_manager_auto"
    assert "legacy_already_tripped_retry" in stub_ksm.trip_calls[0]["reason"]
    assert rm._durable_kill_switch_bridge_succeeded is True


def test_bridge_retries_when_persistence_fails_then_succeeds(
    tmp_path, monkeypatch,
):
    """Codex P1: the in-memory trip can succeed but Postgres save_state
    can fail. The legacy flag is already True; future ``should_activate``
    cycles will not call the bridge. Without a retry, on restart the
    in-memory durable trip is lost (load_state finds nothing) and the
    auto-trip becomes invisible to the hub OrderRouter — recreating the
    very gap this PR is supposed to close. The retry must persist on the
    next evaluate cycle.

    Issues #245/#246: there is now ALSO an in-call bounded retry around
    ``save_state`` (defaults 3 retries on top of the initial attempt, so
    4 attempts per evaluate cycle). Disable the backoff sleeps in this
    test so we don't add ~1.7s wall time per evaluate.
    """
    monkeypatch.setenv("RISK_BRIDGE_RETRY_DELAYS_SECONDS", "0,0,0")

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

    # Trip succeeded in-memory but persistence failed across 4 in-call
    # attempts (issues #245/#246).
    assert len(stub_ksm.trip_calls) == 1
    assert stub_ksm.save_state_calls == 4, (
        "in-call retry must exhaust 4 attempts before surrendering to the "
        "next-evaluate-cycle retry (issues #245/#246)"
    )
    assert rm._durable_kill_switch_bridge_succeeded is False

    # Next evaluate: persistence now succeeds. Trip is already in-memory
    # so the bridge skips the trip call but RETRIES persistence — and
    # this time the very first in-call attempt succeeds.
    stub_ksm.save_state_should_fail = False
    with patch("app.hub.runtime.get_hub_runtime", return_value=runtime), \
         patch("app.data.postgres.connect_with_retry", return_value=_NoopConn()), \
         patch("app.data.postgres.get_control_plane_dsn", return_value="x"):
        rm._check_kill_switch(now)

    assert len(stub_ksm.trip_calls) == 1, (
        "bridge must NOT re-trip when in-memory record already TRIPPED"
    )
    assert stub_ksm.save_state_calls == 5, (
        "second evaluate cycle persists on first in-call attempt (total "
        "4 failed + 1 success = 5)"
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


# ---------------------------------------------------------------------------
# Issues #245 / #246 — in-call bounded retry on transient bridge failures.
# The bridge already retries on subsequent evaluate cycles, but a same-tick
# transient blip would leave the legacy and durable kill-switch state
# inconsistent until the next tick. Wrap each transient-failure surface
# (get_record / ksm.trip / save_state) in a bounded same-call retry.
# ---------------------------------------------------------------------------


class _FlakyKillSwitchManager(_StubKillSwitchManager):
    """Stub whose transient-failure surfaces raise N times then succeed.

    Each counter is consumed per attempt. ``ValueError`` from trip is
    NOT modelled here — it is non-retriable and tested separately.
    """

    def __init__(
        self,
        *,
        get_record_failures: int = 0,
        trip_failures: int = 0,
        save_state_failures: int = 0,
        initial_state=None,
    ):
        super().__init__(initial_state=initial_state)
        self._get_record_failures_remaining = get_record_failures
        self._trip_failures_remaining = trip_failures
        self._save_state_failures_remaining = save_state_failures
        self.get_record_attempts = 0
        self.trip_attempts = 0

    def get_record(self, scope, scope_id):
        self.get_record_attempts += 1
        if self._get_record_failures_remaining > 0:
            self._get_record_failures_remaining -= 1
            raise ConnectionError("simulated transient get_record blip")
        return super().get_record(scope, scope_id)

    def trip(self, scope, scope_id, reason, actor):
        self.trip_attempts += 1
        if self._trip_failures_remaining > 0:
            self._trip_failures_remaining -= 1
            raise ConnectionError("simulated transient trip blip")
        return super().trip(scope, scope_id, reason, actor)

    def save_state(self, conn):
        if self._save_state_failures_remaining > 0:
            self._save_state_failures_remaining -= 1
            self.save_state_calls += 1
            raise ConnectionError("simulated transient save_state blip")
        return super().save_state(conn)


def test_in_call_retry_recovers_from_transient_save_state_blip(
    tmp_path, monkeypatch, caplog,
):
    """Issue #246: a transient Postgres blip during ``save_state`` MUST NOT
    leave the durable trip in-memory only. The bridge must retry inside
    the same call (3 retries default) with brief backoff, then mark
    succeeded. This proves the same-tick auto-trip episode persists even
    when the very first save attempt fails."""
    monkeypatch.setenv("RISK_BRIDGE_RETRY_DELAYS_SECONDS", "0,0,0")
    rm = _build_risk_manager(tmp_path)
    now = _force_legacy_trip_state(rm)

    # 2 transient save failures then success — still inside the bounded
    # retry budget (default 1 + 3 = 4 attempts).
    flaky = _FlakyKillSwitchManager(save_state_failures=2)
    runtime = SimpleNamespace(kill_switch_manager=flaky)

    class _NoopConn:
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    with patch("app.hub.runtime.get_hub_runtime", return_value=runtime), \
         patch("app.data.postgres.connect_with_retry", return_value=_NoopConn()), \
         patch("app.data.postgres.get_control_plane_dsn", return_value="x"):
        rm._check_kill_switch(now)

    # 2 failed + 1 success.
    assert flaky.save_state_calls == 3
    assert rm._durable_kill_switch_bridge_succeeded is True, (
        "in-call retry must recover from a transient save_state blip "
        "without waiting for the next evaluate cycle (issue #246)"
    )
    # Retry attempts logged at WARNING.
    assert any(
        "kill_switch_bridge_retry" in rec.message
        and "op=save_state" in rec.message
        for rec in caplog.records
    ), "in-call retry attempts must be logged"


def test_in_call_retry_recovers_from_transient_get_record_blip(
    tmp_path, monkeypatch, stub_postgres_save,
):
    """Issue #245 (Codex finding: 'is_tripped lookup failing'): a transient
    blip during the pre-trip ``get_record`` lookup MUST NOT prevent the
    bridge from proceeding. The bounded in-call retry should recover."""
    monkeypatch.setenv("RISK_BRIDGE_RETRY_DELAYS_SECONDS", "0,0,0")
    rm = _build_risk_manager(tmp_path)
    now = _force_legacy_trip_state(rm)

    flaky = _FlakyKillSwitchManager(get_record_failures=2)
    runtime = SimpleNamespace(kill_switch_manager=flaky)

    with patch("app.hub.runtime.get_hub_runtime", return_value=runtime):
        rm._check_kill_switch(now)

    # get_record retried until success, then the trip happened.
    assert flaky.get_record_attempts >= 3
    assert len(flaky.trip_calls) == 1
    assert rm._durable_kill_switch_bridge_succeeded is True


def test_in_call_retry_recovers_from_transient_trip_blip(
    tmp_path, monkeypatch, stub_postgres_save,
):
    """Issue #245: a transient blip during ``ksm.trip`` MUST NOT prevent the
    durable manager from being tripped within the same auto-trip cycle.

    Non-ValueError exceptions (i.e. genuine transient failures, not state-
    machine rejections) are retried with the bounded backoff."""
    monkeypatch.setenv("RISK_BRIDGE_RETRY_DELAYS_SECONDS", "0,0,0")
    rm = _build_risk_manager(tmp_path)
    now = _force_legacy_trip_state(rm)

    flaky = _FlakyKillSwitchManager(trip_failures=2)
    runtime = SimpleNamespace(kill_switch_manager=flaky)

    with patch("app.hub.runtime.get_hub_runtime", return_value=runtime):
        rm._check_kill_switch(now)

    # trip retried until success.
    assert flaky.trip_attempts == 3
    assert len(flaky.trip_calls) == 1
    assert rm._durable_kill_switch_bridge_succeeded is True


def test_in_call_retry_exhaustion_logs_loudly_and_leaves_retry_flag(
    tmp_path, monkeypatch, caplog,
):
    """Issues #245/#246: when ALL in-call retries are exhausted (e.g. a
    full Postgres outage that lasts longer than ~1.7s), the bridge must:
      1. log loudly at ERROR (operator-visible structured log).
      2. NOT silently drop the trip — the legacy flag stays True and the
         success tracker stays False so the next evaluate cycle retries.
    """
    monkeypatch.setenv("RISK_BRIDGE_RETRY_DELAYS_SECONDS", "0,0,0")
    rm = _build_risk_manager(tmp_path)
    now = _force_legacy_trip_state(rm)

    # Persistent failures across the entire retry budget (4 attempts).
    flaky = _FlakyKillSwitchManager(save_state_failures=99)
    runtime = SimpleNamespace(kill_switch_manager=flaky)

    class _NoopConn:
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    with patch("app.hub.runtime.get_hub_runtime", return_value=runtime), \
         patch("app.data.postgres.connect_with_retry", return_value=_NoopConn()), \
         patch("app.data.postgres.get_control_plane_dsn", return_value="x"):
        rm._check_kill_switch(now)

    # 1 initial + 3 retries = 4 attempts all failed.
    assert flaky.save_state_calls == 4
    # The in-call retry surrendered, so the bridge has NOT succeeded.
    assert rm._durable_kill_switch_bridge_succeeded is False
    # Legacy flag IS set — the trip is real, just not durable yet.
    assert rm.kill_switch_activated is True
    # Loud, structured error logged for operators.
    assert any(
        "kill_switch_bridge_retry_exhausted" in rec.message
        and "op=save_state" in rec.message
        and rec.levelname == "ERROR"
        for rec in caplog.records
    ), (
        "exhausted in-call retry must surface as a structured ERROR — "
        "operators must not have to read DEBUG logs to see a stuck bridge"
    )
    # Also surfaced via the bridge-specific outer error so existing alerts
    # keyed on this string still fire.
    assert any(
        "kill_switch_bridge_save_state_failed" in rec.message
        for rec in caplog.records
    )


def test_state_machine_value_error_is_not_retried(
    tmp_path, monkeypatch, stub_postgres_save,
):
    """Issue #245 idempotency: a ``ValueError`` from ``ksm.trip`` indicates
    the state machine has already moved past INACTIVE (TRIPPED /
    CLEAR_PENDING / CLEARED). This is NOT a transient blip and MUST NOT
    waste backoff time retrying — the bridge falls through to the
    post-trip race re-check immediately."""
    monkeypatch.setenv("RISK_BRIDGE_RETRY_DELAYS_SECONDS", "0,0,0")
    rm = _build_risk_manager(tmp_path)
    now = _force_legacy_trip_state(rm)

    # The race stub: pre-trip get_record sees INACTIVE; trip() raises
    # ValueError unconditionally; post-trip get_record sees TRIPPED.
    # The bridge MUST NOT retry the ValueError (non-transient) and MUST
    # proceed through the post-trip race re-check to persistence.
    raced = _RaceStubKillSwitchManager(
        initial_state=None,
        post_get_state="TRIPPED",
    )

    # Count distinct trip invocations to prove there was no retry on
    # ValueError.
    trip_attempts = {"n": 0}
    original_trip = raced.trip

    def counting_trip(scope, scope_id, reason, actor):
        trip_attempts["n"] += 1
        return original_trip(scope, scope_id, reason, actor)

    raced.trip = counting_trip  # type: ignore[assignment]
    runtime = SimpleNamespace(kill_switch_manager=raced)

    with patch("app.hub.runtime.get_hub_runtime", return_value=runtime):
        rm._check_kill_switch(now)

    # Critical: trip was invoked exactly ONCE — ValueError is not retried.
    assert trip_attempts["n"] == 1, (
        "state-machine ValueError must be treated as non-retriable "
        "(issue #245 — avoid wasting backoff time on a deterministic "
        "rejection)"
    )
    # Bridge proceeded through the race re-check to persistence and
    # marked succeeded (post-trip state is TRIPPED).
    assert rm._durable_kill_switch_bridge_succeeded is True


def test_retry_delay_override_respects_env_var(monkeypatch):
    """Issue #245/#246 plumbing: the retry-delay env override MUST parse
    correctly so production can tune (or tests can zero) the backoff."""
    import app.core.risk_manager as rm_mod

    monkeypatch.setenv("RISK_BRIDGE_RETRY_DELAYS_SECONDS", "0.1, 0.3,0.7")
    assert rm_mod._bridge_retry_delays() == (0.1, 0.3, 0.7)

    monkeypatch.setenv("RISK_BRIDGE_RETRY_DELAYS_SECONDS", "0,0,0")
    assert rm_mod._bridge_retry_delays() == (0.0, 0.0, 0.0)

    monkeypatch.setenv("RISK_BRIDGE_RETRY_DELAYS_SECONDS", "")
    assert rm_mod._bridge_retry_delays() == rm_mod._BRIDGE_RETRY_DEFAULT_DELAYS

    # Malformed override falls back to defaults (don't disable retries
    # silently on the live auto-trip path).
    monkeypatch.setenv("RISK_BRIDGE_RETRY_DELAYS_SECONDS", "not,a,number")
    assert rm_mod._bridge_retry_delays() == rm_mod._BRIDGE_RETRY_DEFAULT_DELAYS


# ---------------------------------------------------------------------------
# PR #258 round-1 Codex review:
#   P1 — Avoid nested Postgres retries before emergency square-off.
#   P2 — Re-verify same manager (identity fingerprint) after save retries.
# ---------------------------------------------------------------------------


def test_bridge_disables_inner_postgres_retry_and_uses_tight_connect_timeout(
    tmp_path, monkeypatch,
):
    """PR #258 round-1 P1 (Codex): when the bridge persists, it MUST call
    ``connect_with_retry`` with ``max_attempts=1`` and pass a tight
    ``connect_timeout_seconds`` so the inner retry loop is disabled.
    Without this fix, the outer 4 retries × inner 3 retries = 12
    Postgres-connect attempts could block ``evaluate_account_loss`` for
    up to ~60s, delaying emergency ``square_off_all``.
    """
    monkeypatch.setenv("RISK_BRIDGE_RETRY_DELAYS_SECONDS", "0,0,0")
    # Pin the per-attempt connect timeout to a known value so the assertion
    # is robust to default tuning.
    monkeypatch.setenv("RISK_BRIDGE_POSTGRES_CONNECT_TIMEOUT_SECONDS", "3")

    rm = _build_risk_manager(tmp_path)
    now = _force_legacy_trip_state(rm)

    stub_ksm = _StubKillSwitchManager()
    runtime = SimpleNamespace(kill_switch_manager=stub_ksm)

    class _NoopConn:
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    captured_kwargs: dict = {}

    def _capture_connect(dsn, **kwargs):
        captured_kwargs.update(kwargs)
        captured_kwargs["dsn"] = dsn
        return _NoopConn()

    with patch("app.hub.runtime.get_hub_runtime", return_value=runtime), \
         patch("app.data.postgres.connect_with_retry", side_effect=_capture_connect), \
         patch("app.data.postgres.get_control_plane_dsn", return_value="x"):
        rm._check_kill_switch(now)

    assert rm._durable_kill_switch_bridge_succeeded is True
    assert captured_kwargs.get("max_attempts") == 1, (
        "bridge MUST pass max_attempts=1 to connect_with_retry to disable "
        "the inner Postgres retry loop (PR #258 round-1 P1, Codex)"
    )
    assert captured_kwargs.get("connect_timeout_seconds") == 3, (
        "bridge MUST pass a tight per-attempt connect_timeout_seconds so "
        "the total retry budget stays under 10s (PR #258 round-1 P1)"
    )
    assert captured_kwargs.get("autocommit") is True


def test_bridge_total_retry_budget_is_bounded_under_emergency_square_off_threshold(
    tmp_path, monkeypatch,
):
    """PR #258 round-1 P1 (Codex): the WORST-CASE wall-clock for the
    bridge persistence path must stay below the 10-second emergency-
    square-off budget. This proves the bridge gives up within a bounded
    window even when ``connect_with_retry`` always raises (full Postgres
    outage on the auto-trip path).

    Mocks ``connect_with_retry`` to always raise, asserts that
    ``_propagate_kill_switch_to_durable_manager`` returns within the
    budget, and asserts the bridge did NOT mark itself succeeded.
    """
    import time as _time

    # Use the production retry-delay defaults (0.2 + 0.5 + 1.0 = 1.7s) so
    # we exercise the real backoff budget, not the test-zeroed one.
    monkeypatch.delenv("RISK_BRIDGE_RETRY_DELAYS_SECONDS", raising=False)
    monkeypatch.setenv("RISK_BRIDGE_POSTGRES_CONNECT_TIMEOUT_SECONDS", "2")

    rm = _build_risk_manager(tmp_path)
    now = _force_legacy_trip_state(rm)

    stub_ksm = _StubKillSwitchManager()
    runtime = SimpleNamespace(kill_switch_manager=stub_ksm)

    connect_call_count = {"n": 0}

    def _always_fail(*_args, **_kwargs):
        connect_call_count["n"] += 1
        # Simulate "connect refused" rather than connect_timeout (we don't
        # want the test to actually sleep for 2s on every attempt).
        raise ConnectionError("simulated full postgres outage")

    with patch("app.hub.runtime.get_hub_runtime", return_value=runtime), \
         patch("app.data.postgres.connect_with_retry", side_effect=_always_fail), \
         patch("app.data.postgres.get_control_plane_dsn", return_value="x"):
        t0 = _time.monotonic()
        rm._check_kill_switch(now)
        elapsed = _time.monotonic() - t0

    # 4 outer attempts (1 initial + 3 retries).
    assert connect_call_count["n"] == 4, (
        "bridge MUST attempt connect_with_retry exactly 4 times (no nested "
        "retry); got %d" % connect_call_count["n"]
    )
    # Bridge gave up cleanly without marking succeeded.
    assert rm._durable_kill_switch_bridge_succeeded is False
    # Legacy flag is set — operator/next cycle will retry.
    assert rm.kill_switch_activated is True
    # Total wall-clock budget under 10s (real-world: ~1.7s of backoff +
    # 4 × the mocked connect_with_retry overhead which is ~immediate).
    assert elapsed < 10.0, (
        "bridge persistence path must give up within the 10s emergency-"
        "square-off budget (PR #258 round-1 P1); took %.2fs" % elapsed
    )


def test_bridge_aborts_save_retry_on_concurrent_operator_mutation(
    tmp_path, monkeypatch, caplog,
):
    """PR #258 round-1 P2 (Codex): if the first ``save_state`` attempt
    fails and an operator clear/rearm mutates the same
    ``KillSwitchManager`` during the inter-attempt backoff, a later
    retry would persist the manager's current snapshot rather than the
    TRIPPED record that was validated before the retry. The fix:
    capture an identity fingerprint of the in-memory record before the
    first save attempt and abort if it changes between retries.

    Asserts:
      - bridge does NOT mark ``_durable_kill_switch_bridge_succeeded`` True.
      - a structured ``kill_switch_bridge_save_state_aborted_concurrent_
        mutation`` ERROR is logged.
      - the retry loop stops early (save attempts < full retry budget).
    """
    import app.core.risk_manager as rm_mod
    from app.risk.kill_switch import KillSwitchState

    monkeypatch.setenv("RISK_BRIDGE_RETRY_DELAYS_SECONDS", "0,0,0")

    rm = _build_risk_manager(tmp_path)
    now = _force_legacy_trip_state(rm)

    class _MutatingKillSwitchManager(_StubKillSwitchManager):
        """First save attempt fails. Between attempts the record id is
        mutated (simulating an operator-driven rearm / re-trip cycle
        that replaced the record on the same manager instance)."""

        def __init__(self):
            super().__init__()
            self._mutation_applied = False

        def save_state(self, conn):
            # Fail the first attempt so the loop falls into the
            # inter-attempt backoff window.
            self.save_state_calls += 1
            if self.save_state_calls == 1:
                # Simulate the operator mutation having ALREADY happened
                # before the retry's pre_attempt_check runs.
                self._apply_concurrent_mutation()
                raise ConnectionError("simulated postgres blip on save")
            # Subsequent attempts should NOT be reached — the
            # concurrent-mutation guard should abort the retry.
            raise AssertionError(
                "save_state retried despite concurrent mutation guard — "
                "PR #258 round-1 P2 regression"
            )

        def _apply_concurrent_mutation(self):
            if self._mutation_applied:
                return
            self._mutation_applied = True
            # Mutate the underlying record's id (simulates operator
            # clear/rearm cycle producing a new record id).
            self._state_text = "TRIPPED"

        def get_record(self, scope, scope_id):
            rec = super().get_record(scope, scope_id)
            if rec is None:
                return None
            # After mutation, surface a different id so the fingerprint
            # mismatches the pre-save snapshot.
            if self._mutation_applied:
                from types import SimpleNamespace as _SN
                return _SN(
                    id="rec-2-after-operator-rearm",
                    state=KillSwitchState.TRIPPED,
                    scope=scope,
                    scope_id=scope_id,
                    trip_reason=None,
                    tripped_by=None,
                    block_exits=False,
                    updated_at="2026-05-12T00:00:01Z",
                )
            return rec

    mutating = _MutatingKillSwitchManager()
    runtime = SimpleNamespace(kill_switch_manager=mutating)

    class _NoopConn:
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    with patch("app.hub.runtime.get_hub_runtime", return_value=runtime), \
         patch("app.data.postgres.connect_with_retry", return_value=_NoopConn()), \
         patch("app.data.postgres.get_control_plane_dsn", return_value="x"):
        rm._check_kill_switch(now)

    # Trip happened in-memory (initial state was INACTIVE).
    assert len(mutating.trip_calls) == 1
    # Save was attempted exactly ONCE then aborted on the next retry's
    # pre_attempt_check.
    assert mutating.save_state_calls == 1, (
        "save retry MUST abort on concurrent mutation rather than "
        "persisting a stale snapshot (PR #258 round-1 P2)"
    )
    # Bridge did NOT mark itself succeeded — next evaluate cycle re-validates.
    assert rm._durable_kill_switch_bridge_succeeded is False
    # Loud, structured ERROR for operators.
    assert any(
        "kill_switch_bridge_save_state_aborted_concurrent_mutation" in rec.message
        and rec.levelname == "ERROR"
        for rec in caplog.records
    ), (
        "concurrent-mutation abort MUST surface as a structured ERROR — "
        "operators must be able to correlate this with their clear/rearm "
        "action (PR #258 round-1 P2)"
    )
    # The retry-aborted helper log fires too.
    assert any(
        "kill_switch_bridge_retry_aborted" in rec.message
        and "op=save_state" in rec.message
        and "concurrent_mutation_detected" in rec.message
        for rec in caplog.records
    )

    # Sanity: the helper itself accepts pre_attempt_check kwarg.
    assert callable(rm_mod._retry_with_backoff)


def test_bridge_postgres_connect_timeout_env_override(monkeypatch):
    """PR #258 round-1 P1 plumbing: the per-attempt connect-timeout env
    override MUST parse correctly so ops can tune the bridge budget."""
    import app.core.risk_manager as rm_mod

    monkeypatch.delenv("RISK_BRIDGE_POSTGRES_CONNECT_TIMEOUT_SECONDS", raising=False)
    assert (
        rm_mod._bridge_postgres_connect_timeout_seconds()
        == rm_mod._BRIDGE_POSTGRES_CONNECT_TIMEOUT_DEFAULT_SECONDS
    )

    monkeypatch.setenv("RISK_BRIDGE_POSTGRES_CONNECT_TIMEOUT_SECONDS", "4")
    assert rm_mod._bridge_postgres_connect_timeout_seconds() == 4

    # Clamped to >= 1.
    monkeypatch.setenv("RISK_BRIDGE_POSTGRES_CONNECT_TIMEOUT_SECONDS", "0")
    assert rm_mod._bridge_postgres_connect_timeout_seconds() == 1

    # Malformed -> default.
    monkeypatch.setenv("RISK_BRIDGE_POSTGRES_CONNECT_TIMEOUT_SECONDS", "garbage")
    assert (
        rm_mod._bridge_postgres_connect_timeout_seconds()
        == rm_mod._BRIDGE_POSTGRES_CONNECT_TIMEOUT_DEFAULT_SECONDS
    )


# ---------------------------------------------------------------------------
# PR #258 round-2 Codex review:
#   P1 cluster — Total retry budget BEFORE emergency square-off must be < 10s
#                (lines 731, 788, 985, 1008). Addressed via a single
#                ``_BridgeDeadline`` wall-clock accumulator.
#   P2         — Non-finite timeout overrides (lines 104, 105)
#   P2         — Preserve retry count when zeroing backoff (line 164)
#   P2         — Same-manager fingerprint recheck after save (line 987)
#   P2         — Unaudited partial-trip detection (line 803)
# ---------------------------------------------------------------------------


def test_bridge_total_deadline_env_override(monkeypatch):
    """PR #258 round-2 P1: ``RISK_BRIDGE_TOTAL_DEADLINE_SECONDS`` parses
    correctly. Non-finite / overflow / malformed values fall back to the
    default rather than raise out of the auto-trip path."""
    import app.core.risk_manager as rm_mod

    monkeypatch.delenv("RISK_BRIDGE_TOTAL_DEADLINE_SECONDS", raising=False)
    assert (
        rm_mod._bridge_total_deadline_seconds()
        == rm_mod._BRIDGE_TOTAL_DEADLINE_DEFAULT_SECONDS
    )

    monkeypatch.setenv("RISK_BRIDGE_TOTAL_DEADLINE_SECONDS", "3.5")
    assert rm_mod._bridge_total_deadline_seconds() == 3.5

    # Clamped to >= 1s so the deadline is never effectively disabled.
    monkeypatch.setenv("RISK_BRIDGE_TOTAL_DEADLINE_SECONDS", "0.1")
    assert rm_mod._bridge_total_deadline_seconds() == 1.0

    # Non-finite (Codex P2 line 104/105): inf must fall back to default
    # rather than raise OverflowError into the auto-trip path.
    monkeypatch.setenv("RISK_BRIDGE_TOTAL_DEADLINE_SECONDS", "inf")
    assert (
        rm_mod._bridge_total_deadline_seconds()
        == rm_mod._BRIDGE_TOTAL_DEADLINE_DEFAULT_SECONDS
    )

    # Overflow: 1e309 is bigger than float max → float() raises
    # OverflowError. The helper must catch it.
    monkeypatch.setenv("RISK_BRIDGE_TOTAL_DEADLINE_SECONDS", "1e309")
    assert (
        rm_mod._bridge_total_deadline_seconds()
        == rm_mod._BRIDGE_TOTAL_DEADLINE_DEFAULT_SECONDS
    )

    # Malformed -> default.
    monkeypatch.setenv("RISK_BRIDGE_TOTAL_DEADLINE_SECONDS", "garbage")
    assert (
        rm_mod._bridge_total_deadline_seconds()
        == rm_mod._BRIDGE_TOTAL_DEADLINE_DEFAULT_SECONDS
    )


def test_bridge_postgres_connect_timeout_handles_non_finite_safely(monkeypatch):
    """PR #258 round-2 P2 (Codex line 104/105): ``int(float('inf'))`` raises
    ``OverflowError``, not ``ValueError``. The env override parser MUST
    catch every parse failure so a malformed override during an auto-trip
    cannot raise out of ``_propagate_kill_switch_to_durable_manager``
    before ``square_off_all`` runs."""
    import app.core.risk_manager as rm_mod

    # Non-finite float -> default.
    monkeypatch.setenv("RISK_BRIDGE_POSTGRES_CONNECT_TIMEOUT_SECONDS", "inf")
    assert (
        rm_mod._bridge_postgres_connect_timeout_seconds()
        == rm_mod._BRIDGE_POSTGRES_CONNECT_TIMEOUT_DEFAULT_SECONDS
    )

    # OverflowError-producing literal -> default (the bug we are fixing).
    monkeypatch.setenv("RISK_BRIDGE_POSTGRES_CONNECT_TIMEOUT_SECONDS", "1e309")
    assert (
        rm_mod._bridge_postgres_connect_timeout_seconds()
        == rm_mod._BRIDGE_POSTGRES_CONNECT_TIMEOUT_DEFAULT_SECONDS
    )

    # Negative infinity treated the same.
    monkeypatch.setenv("RISK_BRIDGE_POSTGRES_CONNECT_TIMEOUT_SECONDS", "-inf")
    assert (
        rm_mod._bridge_postgres_connect_timeout_seconds()
        == rm_mod._BRIDGE_POSTGRES_CONNECT_TIMEOUT_DEFAULT_SECONDS
    )

    # NaN treated the same.
    monkeypatch.setenv("RISK_BRIDGE_POSTGRES_CONNECT_TIMEOUT_SECONDS", "nan")
    assert (
        rm_mod._bridge_postgres_connect_timeout_seconds()
        == rm_mod._BRIDGE_POSTGRES_CONNECT_TIMEOUT_DEFAULT_SECONDS
    )


def test_bridge_retry_delays_preserves_attempt_count_for_zero_shorthand(monkeypatch):
    """PR #258 round-2 P2 (Codex line 164): the docstring lets operators
    write ``RISK_BRIDGE_RETRY_DELAYS_SECONDS=0`` to disable backoff. But
    the retry count is ``len(delays)``, so the shorthand silently reduced
    the bridge from 4 attempts (1 + 3) to 2 attempts (1 + 1). Fix:
    preserve the default attempt count when every parsed delay is zero
    and the parsed length is shorter than the default."""
    import app.core.risk_manager as rm_mod

    # Single-token zero shorthand → preserves 3-delay length (4 attempts).
    monkeypatch.setenv("RISK_BRIDGE_RETRY_DELAYS_SECONDS", "0")
    delays = rm_mod._bridge_retry_delays()
    assert len(delays) == len(rm_mod._BRIDGE_RETRY_DEFAULT_DELAYS), (
        "PR #258 round-2 P2: '0' shorthand must NOT reduce the retry "
        "count — it must expand to all-zero delays of the default length"
    )
    assert all(d == 0.0 for d in delays), (
        "all expanded delays must be zero (the operator's intent)"
    )

    # Two-token zero shorthand also expands.
    monkeypatch.setenv("RISK_BRIDGE_RETRY_DELAYS_SECONDS", "0,0")
    delays = rm_mod._bridge_retry_delays()
    assert len(delays) == len(rm_mod._BRIDGE_RETRY_DEFAULT_DELAYS)
    assert all(d == 0.0 for d in delays)

    # Explicit full-length "0,0,0" stays unchanged.
    monkeypatch.setenv("RISK_BRIDGE_RETRY_DELAYS_SECONDS", "0,0,0")
    assert rm_mod._bridge_retry_delays() == (0.0, 0.0, 0.0)

    # Non-zero single value remains a single retry (operator explicitly
    # asked for fewer attempts) — only the zero-shorthand case expands.
    monkeypatch.setenv("RISK_BRIDGE_RETRY_DELAYS_SECONDS", "0.5")
    assert rm_mod._bridge_retry_delays() == (0.5,)

    # Mixed-zero-and-nonzero stays as parsed (operator is explicit).
    monkeypatch.setenv("RISK_BRIDGE_RETRY_DELAYS_SECONDS", "0,0.5")
    assert rm_mod._bridge_retry_delays() == (0.0, 0.5)


def test_bridge_retry_delays_rejects_non_finite_safely(monkeypatch):
    """PR #258 round-2 P2: ``float('inf')`` is finite from a parse POV
    but ``math.isfinite(inf) == False``. A non-finite delay would cause
    unbounded sleep — fall back to defaults instead."""
    import app.core.risk_manager as rm_mod

    monkeypatch.setenv("RISK_BRIDGE_RETRY_DELAYS_SECONDS", "0.2,inf,1.0")
    assert (
        rm_mod._bridge_retry_delays()
        == rm_mod._BRIDGE_RETRY_DEFAULT_DELAYS
    ), "non-finite delay must fall back to default rather than parse to inf"


def test_bridge_aborts_when_deadline_exceeded_by_slow_save(
    tmp_path, monkeypatch, caplog,
):
    """PR #258 round-2 P1: when ``connect_with_retry`` is SLOW (the
    realistic Postgres-outage scenario where DSN candidates each consume
    their full connect_timeout), the bridge MUST abort within the total
    deadline rather than spend the full retry budget. Mock
    ``connect_with_retry`` to sleep 3s per call; with a 2s deadline the
    bridge should attempt at most ONE save and then exit cleanly.
    """
    import time as _time
    import app.core.risk_manager as rm_mod

    # 2s deadline — tight. Each fake connect sleeps 3s and raises so the
    # retry helper would otherwise loop 4× × 3s + backoff = ~13s.
    monkeypatch.setenv("RISK_BRIDGE_TOTAL_DEADLINE_SECONDS", "2")
    monkeypatch.setenv("RISK_BRIDGE_RETRY_DELAYS_SECONDS", "0,0,0")
    monkeypatch.setenv("RISK_BRIDGE_POSTGRES_CONNECT_TIMEOUT_SECONDS", "5")

    rm = _build_risk_manager(tmp_path)
    now = _force_legacy_trip_state(rm)

    stub_ksm = _StubKillSwitchManager()
    runtime = SimpleNamespace(kill_switch_manager=stub_ksm)

    save_call_count = {"n": 0}

    def _slow_connect(*_args, **_kwargs):
        save_call_count["n"] += 1
        _time.sleep(3.0)  # exceeds the 2s deadline on the very first call
        raise ConnectionError("simulated slow postgres outage")

    t0 = _time.monotonic()
    with patch("app.hub.runtime.get_hub_runtime", return_value=runtime), \
         patch("app.data.postgres.connect_with_retry", side_effect=_slow_connect), \
         patch("app.data.postgres.get_control_plane_dsn", return_value="x"):
        rm._check_kill_switch(now)
    elapsed = _time.monotonic() - t0

    # Wall-clock must stay near the deadline (one slow attempt + abort).
    # Allow generous slack for CI / Windows scheduler jitter.
    assert elapsed < 5.0, (
        "bridge MUST abort once the wall-clock deadline is reached "
        "(PR #258 round-2 P1); took %.2fs" % elapsed
    )
    # The bridge gave up before exhausting retries.
    assert save_call_count["n"] <= 2, (
        "deadline must short-circuit retries; got %d save attempts"
        % save_call_count["n"]
    )
    assert rm._durable_kill_switch_bridge_succeeded is False
    # Legacy flag is set — square-off proceeds on the legacy path.
    assert rm.kill_switch_activated is True
    # Loud deadline log surfaced.
    assert any(
        "kill_switch_bridge_deadline_exceeded" in rec.message
        and rec.levelname == "ERROR"
        for rec in caplog.records
    ), "deadline abort MUST surface as structured ERROR"

    assert rm_mod._BRIDGE_TOTAL_DEADLINE_DEFAULT_SECONDS == 5.0


def test_bridge_aborts_when_audit_write_blocks_past_deadline(
    tmp_path, monkeypatch, caplog,
):
    """PR #258 round-2 P1 (Codex line 788): ``KillSwitchManager.trip``
    emits an audit event via ``app/core/audit_log.py``, which calls
    ``connect_with_retry`` with default ``max_attempts=3`` and 5s
    connect timeout. During a Postgres outage that single call can
    block for ~15s — long past the emergency square-off budget. The
    bridge's hard deadline MUST short-circuit even when the slow step
    is INSIDE ``ksm.trip`` (audit) rather than in our own save path.

    We model this by making the stub ``ksm.trip`` sleep ~3s per call
    so the per-attempt sleep + backoff exceed a tight 2s deadline.
    """
    import time as _time

    monkeypatch.setenv("RISK_BRIDGE_TOTAL_DEADLINE_SECONDS", "2")
    monkeypatch.setenv("RISK_BRIDGE_RETRY_DELAYS_SECONDS", "0,0,0")

    rm = _build_risk_manager(tmp_path)
    now = _force_legacy_trip_state(rm)

    class _SlowAuditKillSwitchManager(_StubKillSwitchManager):
        def __init__(self):
            super().__init__()
            self.trip_call_count = 0

        def trip(self, scope, scope_id, reason, actor):
            self.trip_call_count += 1
            # Simulate the audit-write Postgres retry blocking ~3s.
            _time.sleep(3.0)
            raise ConnectionError("simulated slow audit-write blip")

    slow = _SlowAuditKillSwitchManager()
    runtime = SimpleNamespace(kill_switch_manager=slow)

    class _NoopConn:
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    t0 = _time.monotonic()
    with patch("app.hub.runtime.get_hub_runtime", return_value=runtime), \
         patch("app.data.postgres.connect_with_retry", return_value=_NoopConn()), \
         patch("app.data.postgres.get_control_plane_dsn", return_value="x"):
        rm._check_kill_switch(now)
    elapsed = _time.monotonic() - t0

    # Deadline short-circuited the retry loop — at most a couple of
    # ksm.trip invocations should have run.
    assert elapsed < 5.0, (
        "bridge MUST exit within the deadline even when ksm.trip "
        "is slow due to audit-write retries (PR #258 round-2 P1, "
        "Codex line 788); took %.2fs" % elapsed
    )
    assert slow.trip_call_count <= 2, (
        "audit-write deadline guard must short-circuit ksm.trip retries; "
        "got %d attempts" % slow.trip_call_count
    )
    assert rm._durable_kill_switch_bridge_succeeded is False
    assert any(
        "kill_switch_bridge_deadline_exceeded" in rec.message
        and "op=ksm_trip" in rec.message
        and rec.levelname == "ERROR"
        for rec in caplog.records
    )


def test_bridge_aborts_when_dsn_candidates_exhaust_deadline(
    tmp_path, monkeypatch, caplog,
):
    """PR #258 round-2 P1 (Codex line 985): when ``POSTGRES_FORCE_IPV4``
    is true or the host is ``host.docker.internal``,
    ``connect_with_retry`` iterates 2 DSN candidates per call — even
    with ``max_attempts=1``. So a single save attempt can consume
    ``2 × connect_timeout`` rather than ``1 × connect_timeout``. The
    deadline guard must bound the TOTAL wall-clock regardless. Model
    this by making each connect attempt sleep 2s (simulating the IPv4
    candidate timing out, then the original host candidate timing out)
    and asserting the bridge exits within the deadline.
    """
    import time as _time

    monkeypatch.setenv("RISK_BRIDGE_TOTAL_DEADLINE_SECONDS", "2")
    monkeypatch.setenv("RISK_BRIDGE_RETRY_DELAYS_SECONDS", "0,0,0")

    rm = _build_risk_manager(tmp_path)
    now = _force_legacy_trip_state(rm)

    stub_ksm = _StubKillSwitchManager()
    runtime = SimpleNamespace(kill_switch_manager=stub_ksm)

    save_call_count = {"n": 0}

    def _two_candidate_sim(*_args, **_kwargs):
        # Simulate connect_with_retry iterating 2 DSN candidates, each
        # waiting its full per-attempt timeout.
        save_call_count["n"] += 1
        _time.sleep(2.0)  # IPv4 candidate timeout
        _time.sleep(2.0)  # original host candidate timeout
        raise ConnectionError("simulated all-DSN-candidates exhausted")

    t0 = _time.monotonic()
    with patch("app.hub.runtime.get_hub_runtime", return_value=runtime), \
         patch("app.data.postgres.connect_with_retry", side_effect=_two_candidate_sim), \
         patch("app.data.postgres.get_control_plane_dsn", return_value="x"):
        rm._check_kill_switch(now)
    elapsed = _time.monotonic() - t0

    # Without the deadline, 4 outer attempts × 4s = 16s. With the
    # deadline, the bridge must give up after the first attempt's
    # 4s already exceeded the 2s budget — typically just one call.
    assert elapsed < 8.0, (
        "DSN-candidate iteration must not defeat the bridge deadline "
        "(PR #258 round-2 P1, Codex line 985); took %.2fs" % elapsed
    )
    assert save_call_count["n"] <= 2
    assert rm._durable_kill_switch_bridge_succeeded is False


def test_bridge_rejects_unaudited_partial_trip_with_audit_reemit(
    tmp_path, monkeypatch, caplog,
):
    """PR #258 round-2 P2 (Codex line 803): when ``ksm.trip`` mutates the
    in-memory record but raises in ``_emit_audit`` AFTER the mutation,
    the retry calls ``trip`` again → ValueError → post-trip recheck
    sees TRIPPED state and treats it as success — even though the
    original transition was NOT audited.

    Fix: detect this case. If the FIRST trip attempt raised AND the
    record is now TRIPPED, re-emit the audit through the bridge's
    fallback path so the trip remains durably accountable.
    """
    monkeypatch.setenv("RISK_BRIDGE_RETRY_DELAYS_SECONDS", "0,0,0")

    rm = _build_risk_manager(tmp_path)
    now = _force_legacy_trip_state(rm)

    class _MutateThenAuditFailsKillSwitchManager(_StubKillSwitchManager):
        """First trip: mutate state to TRIPPED then raise (audit failed).
        Second trip: state is now TRIPPED so raise ValueError. Get_record
        reports TRIPPED throughout."""

        def __init__(self):
            super().__init__()
            self.trip_call_count = 0

        def trip(self, scope, scope_id, reason, actor):
            self.trip_call_count += 1
            if self.trip_call_count == 1:
                # Mutate in-memory state to TRIPPED, then raise (model
                # _emit_audit blowing up after _records[key] = record).
                self._state_text = "TRIPPED"
                raise IOError("simulated audit-write disk-full")
            # Subsequent attempts see the existing TRIPPED state.
            raise ValueError(
                "Cannot trip kill switch: current state is TRIPPED"
            )

    flaky = _MutateThenAuditFailsKillSwitchManager()
    runtime = SimpleNamespace(kill_switch_manager=flaky)

    class _NoopConn:
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    # Capture audit emissions via the bridge's fallback path.
    audit_emissions: list = []

    def _capture_audit(**kwargs):
        audit_emissions.append(kwargs)
        return {"audit_id": "stub"}

    with patch("app.hub.runtime.get_hub_runtime", return_value=runtime), \
         patch("app.data.postgres.connect_with_retry", return_value=_NoopConn()), \
         patch("app.data.postgres.get_control_plane_dsn", return_value="x"), \
         patch("app.core.audit_log.emit_audit_event", side_effect=_capture_audit):
        rm._check_kill_switch(now)

    # The bridge MUST log that an unaudited partial trip was detected.
    assert any(
        "kill_switch_bridge_unaudited_partial_trip" in rec.message
        and rec.levelname == "ERROR"
        for rec in caplog.records
    ), (
        "bridge MUST log unaudited_partial_trip when the first ksm.trip "
        "mutated state but raised before audit (PR #258 round-2 P2)"
    )
    # And it MUST re-emit the audit through the fallback path.
    assert any(
        evt.get("actor") == "risk_manager_auto"
        and evt.get("action") == "kill_switch.trip"
        and evt.get("metadata", {}).get("bridge_reemit") is True
        for evt in audit_emissions
    ), (
        "bridge MUST re-emit the trip audit when the original audit "
        "write failed post-mutation (PR #258 round-2 P2)"
    )
    # The bridge marks succeeded because the durable state IS TRIPPED
    # and we've now ensured the audit is on record.
    assert rm._durable_kill_switch_bridge_succeeded is True
    assert any(
        "kill_switch_bridge_audit_reemitted" in rec.message
        for rec in caplog.records
    )


def test_bridge_aborts_on_same_manager_mutation_between_save_retries(
    tmp_path, monkeypatch, caplog,
):
    """PR #258 round-2 P2 (Codex line 987): the round-1 fingerprint
    guard only verified the active manager when it was a DIFFERENT
    object. If the SAME manager object is mutated (operator
    clear/rearm during backoff or between the final pre_attempt_check
    and the save flush), the bridge would mark success despite the
    state mismatch. Fix: ALWAYS re-read the record state after save
    succeeds and check fingerprint against the entry fingerprint
    regardless of manager identity.

    We model the race by mutating the manager's record AFTER save
    returns successfully (the pre_attempt_check window has closed)
    and asserting the bridge does NOT mark succeeded.
    """
    monkeypatch.setenv("RISK_BRIDGE_RETRY_DELAYS_SECONDS", "0,0,0")

    rm = _build_risk_manager(tmp_path)
    now = _force_legacy_trip_state(rm)

    from app.risk.kill_switch import KillSwitchState

    class _MutateAfterSaveKillSwitchManager(_StubKillSwitchManager):
        """Save succeeds; AFTER save the get_record returns a different
        fingerprint (simulating operator clear/rearm landing between
        the final pre_attempt_check and the post-save recheck)."""

        def __init__(self):
            super().__init__()
            self._save_succeeded = False
            # Stable initial fingerprint shape so pre-save snapshot
            # captures it; we'll mutate id after save.
            self._record_id = "rec-original"

        def get_record(self, scope, scope_id):
            from types import SimpleNamespace as _SN
            return _SN(
                id=self._record_id,
                state=KillSwitchState.TRIPPED,
                scope=scope,
                scope_id=scope_id,
                trip_reason=None,
                tripped_by=None,
                block_exits=False,
                updated_at="2026-05-12T00:00:00Z",
            )

        def is_tripped(self, scope, scope_id):
            return True

        def trip(self, scope, scope_id, reason, actor):
            # Already TRIPPED; the bridge should not need to call us.
            raise ValueError(
                "Cannot trip kill switch: current state is TRIPPED"
            )

        def save_state(self, conn):
            self.save_state_calls += 1
            self._save_succeeded = True
            # Mutate the record id to simulate operator-driven rearm
            # cycle landing after pre_attempt_check but before our
            # post-save recheck.
            self._record_id = "rec-after-operator-rearm"

    mutating = _MutateAfterSaveKillSwitchManager()
    runtime = SimpleNamespace(kill_switch_manager=mutating)

    class _NoopConn:
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    with patch("app.hub.runtime.get_hub_runtime", return_value=runtime), \
         patch("app.data.postgres.connect_with_retry", return_value=_NoopConn()), \
         patch("app.data.postgres.get_control_plane_dsn", return_value="x"):
        rm._check_kill_switch(now)

    # Save was attempted (pre_attempt_check passed because the mutation
    # had not yet happened) and succeeded; PR #258 round-3 P2 (Codex
    # line 1445) then re-saves the CURRENT manager state to overwrite
    # the bridge's stale snapshot — so we expect TWO save calls now
    # (original save + repair save). Both run on the same in-memory
    # manager, but the second snapshot reflects post-mutation state.
    assert mutating.save_state_calls == 2, (
        "post-save mismatch repair (PR #258 round-3 P2, Codex line "
        "1445) MUST re-save the current manager state — got %d calls"
        % mutating.save_state_calls
    )
    # But the post-save fingerprint recheck (PR #258 round-2 P2) caught
    # the same-manager mutation and refused to mark succeeded.
    assert rm._durable_kill_switch_bridge_succeeded is False, (
        "bridge MUST NOT mark succeeded when the same-manager record "
        "fingerprint changed AFTER save succeeded (PR #258 round-2 P2, "
        "Codex line 987)"
    )
    assert any(
        "kill_switch_bridge_post_save_fingerprint_mismatch" in rec.message
        and rec.levelname == "ERROR"
        for rec in caplog.records
    ), "same-manager mutation MUST surface as structured ERROR"
    # PR #258 round-3 P2 (Codex line 1445): the repair MUST surface a
    # structured log so operators can correlate the stale-row overwrite
    # with the operator clear/rearm that intervened.
    assert any(
        "kill_switch_bridge_post_save_repair_succeeded" in rec.message
        for rec in caplog.records
    ), "post-save mismatch MUST trigger a repair save (PR #258 round-3 P2)"


def test_bridge_total_deadline_default_under_emergency_square_off_threshold():
    """Sanity: the default deadline must be substantially under the
    legacy 10s square-off budget so a real outage in production gives
    operators headroom AND the legacy emergency exit fires within the
    broker action window."""
    import app.core.risk_manager as rm_mod
    assert rm_mod._BRIDGE_TOTAL_DEADLINE_DEFAULT_SECONDS == 5.0
    assert rm_mod._BRIDGE_TOTAL_DEADLINE_DEFAULT_SECONDS < 10.0


# ---------------------------------------------------------------------------
# PR #258 round-3 — new P2 findings (5)
# ---------------------------------------------------------------------------


def _build_partial_trip_manager_class():
    """Build a stub KillSwitchManager subclass that simulates an
    unaudited partial trip on the FIRST trip() call (mutates state then
    raises) and ValueError on subsequent calls. Used by multiple
    round-3 tests below."""
    from app.risk.kill_switch import KillSwitchState

    class _PartialTripKillSwitchManager(_StubKillSwitchManager):
        def __init__(self):
            super().__init__()
            self.trip_call_count = 0
            # Tests can override this hook to simulate audit failure
            # vs success during the bridge's re-emit call.
            self._audit_after_mutation_state = KillSwitchState.TRIPPED

        def trip(self, scope, scope_id, reason, actor):
            self.trip_call_count += 1
            if self.trip_call_count == 1:
                self._state_text = "TRIPPED"
                raise IOError("simulated audit-write disk-full")
            raise ValueError(
                "Cannot trip kill switch: current state is TRIPPED"
            )
    return _PartialTripKillSwitchManager


def test_bridge_reemit_failure_does_not_mark_success(
    tmp_path, monkeypatch, caplog,
):
    """PR #258 round-3 P2 (Codex line 1608): when the audit re-emit
    fails (e.g. unwritable AUDIT_LOG_PATH), the helper now returns
    False and the caller MUST leave the bridge unsucceeded so the
    NEXT evaluate cycle retries. Without this fix the bridge would
    permanently suppress retries while the trip has no audit row.
    """
    monkeypatch.setenv("RISK_BRIDGE_RETRY_DELAYS_SECONDS", "0,0,0")

    rm = _build_risk_manager(tmp_path)
    now = _force_legacy_trip_state(rm)

    PartialKsm = _build_partial_trip_manager_class()
    flaky = PartialKsm()
    runtime = SimpleNamespace(kill_switch_manager=flaky)

    class _NoopConn:
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    def _emit_raises(**kwargs):
        raise IOError("simulated AUDIT_LOG_PATH unwritable")

    with patch("app.hub.runtime.get_hub_runtime", return_value=runtime), \
         patch("app.data.postgres.connect_with_retry", return_value=_NoopConn()), \
         patch("app.data.postgres.get_control_plane_dsn", return_value="x"), \
         patch("app.core.audit_log.emit_audit_event", side_effect=_emit_raises):
        rm._check_kill_switch(now)

    # PR #258 round-3 P2: bridge MUST refuse to mark succeeded when
    # the audit re-emit failed — otherwise retries are permanently
    # suppressed with no audit row.
    assert rm._durable_kill_switch_bridge_succeeded is False, (
        "bridge MUST NOT mark succeeded when audit re-emit failed "
        "(PR #258 round-3 P2, Codex line 1608)"
    )
    # The pending-reemit marker MUST be set so the next cycle retries
    # the audit on the already-tripped path (PR #258 round-3 P2,
    # Codex line 1266).
    assert rm._pending_audit_reemit is True, (
        "pending_audit_reemit marker MUST persist across cycles when "
        "the re-emit failed (PR #258 round-3 P2, Codex line 1266)"
    )
    assert any(
        "kill_switch_bridge_audit_reemit_failed" in rec.message
        for rec in caplog.records
    )


def test_bridge_reemit_respects_deadline(tmp_path, monkeypatch, caplog):
    """PR #258 round-3 P2 (Codex line 1573): the audit re-emit MUST
    respect the bridge deadline. Without this, the re-emit's nested
    ``connect_with_retry`` (via ``_try_postgres_persist``) defaults to
    3 attempts × 5s = up to 15s of audit-write blocking BEFORE
    ``square_off_all`` runs. Model this by:
      1. Setting deadline to 1s.
      2. Tripping with a stub that consumes ~0.5s before the
         post-trip recheck, leaving the helper ~0.5s of budget.
      3. Asserting the helper either (a) skips due to deadline, or
         (b) propagates a tight POSTGRES_CONNECT_TIMEOUT_SECONDS to
         the env so the dual-write cannot block 15s.
    """
    import time as _time

    monkeypatch.setenv("RISK_BRIDGE_TOTAL_DEADLINE_SECONDS", "1")
    monkeypatch.setenv("RISK_BRIDGE_RETRY_DELAYS_SECONDS", "0,0,0")

    rm = _build_risk_manager(tmp_path)
    now = _force_legacy_trip_state(rm)

    PartialKsm = _build_partial_trip_manager_class()
    flaky = PartialKsm()
    runtime = SimpleNamespace(kill_switch_manager=flaky)

    class _NoopConn:
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    def _slow_emit(**kwargs):
        # The bridge MUST tighten POSTGRES_CONNECT_TIMEOUT_SECONDS
        # from the system default before calling emit_audit_event;
        # a separate test verifies that contract precisely. Here we
        # only assert the WALL-CLOCK stays bounded.
        return {"audit_id": "stub"}

    # Burn most of the budget BEFORE the bridge starts the re-emit
    # so the deadline gate fires (most direct way to model "audit
    # would block past deadline").
    real_propagate = rm._propagate_kill_switch_to_durable_manager

    def _slow_propagate(*args, **kwargs):
        _time.sleep(0.9)
        return real_propagate(*args, **kwargs)

    rm._propagate_kill_switch_to_durable_manager = _slow_propagate
    t0 = _time.monotonic()
    with patch("app.hub.runtime.get_hub_runtime", return_value=runtime), \
         patch("app.data.postgres.connect_with_retry", return_value=_NoopConn()), \
         patch("app.data.postgres.get_control_plane_dsn", return_value="x"), \
         patch("app.core.audit_log.emit_audit_event", side_effect=_slow_emit):
        rm._check_kill_switch(now)
    elapsed = _time.monotonic() - t0

    # Bridge MUST stay under ~3s worst-case under a 1s deadline +
    # 0.9s warm-up + some test overhead. Without the deadline
    # threading, audit_log's default 15s connect window would push
    # us well past this.
    assert elapsed < 4.0, (
        "bridge re-emit MUST respect deadline (PR #258 round-3 P2, "
        "Codex line 1573); took %.2fs" % elapsed
    )


def test_bridge_reemit_tightens_postgres_timeout_env(
    tmp_path, monkeypatch,
):
    """PR #258 round-3 P2 (Codex line 1573): the audit re-emit helper
    MUST tighten ``POSTGRES_CONNECT_TIMEOUT_SECONDS`` for the
    duration of the ``emit_audit_event`` call so the nested
    dual-write to Postgres cannot blow the bridge deadline. Verify
    the env override is applied during the call AND restored
    afterwards."""
    monkeypatch.setenv("RISK_BRIDGE_TOTAL_DEADLINE_SECONDS", "3")
    monkeypatch.setenv("RISK_BRIDGE_RETRY_DELAYS_SECONDS", "0,0,0")
    # Set a baseline value the bridge MUST shrink.
    monkeypatch.setenv("POSTGRES_CONNECT_TIMEOUT_SECONDS", "30")

    rm = _build_risk_manager(tmp_path)

    captured: list = []

    def _capture(**kwargs):
        captured.append(
            os.environ.get("POSTGRES_CONNECT_TIMEOUT_SECONDS")
        )
        return {"audit_id": "stub"}

    from types import SimpleNamespace as _SN
    from app.risk.kill_switch import KillSwitchState

    rec = _SN(
        id="rec-x",
        state=KillSwitchState.TRIPPED,
        scope=None,
        scope_id="GLOBAL",
        trip_reason=None,
        tripped_by=None,
    )

    deadline = importlib.import_module(
        "app.core.risk_manager"
    )._BridgeDeadline(timeout_seconds=3.0)

    with patch("app.core.audit_log.emit_audit_event", side_effect=_capture):
        ok = rm._reemit_kill_switch_bridge_audit(
            record=rec, bridge_reason="test", deadline=deadline,
        )

    assert ok is True
    assert captured, "emit_audit_event MUST be invoked"
    inside_value = captured[0]
    assert inside_value is not None
    assert int(inside_value) <= 3, (
        "bridge MUST tighten POSTGRES_CONNECT_TIMEOUT_SECONDS to <= "
        "remaining deadline (PR #258 round-3 P2, Codex line 1573); "
        "saw %r" % inside_value
    )
    # After the call returns, the env value MUST be restored to the
    # original "30" — the override must not leak into other paths.
    assert os.environ.get("POSTGRES_CONNECT_TIMEOUT_SECONDS") == "30", (
        "env override MUST be restored after _reemit returns"
    )


def test_bridge_dsn_candidate_count_scales_deadline_clamp(monkeypatch):
    """PR #258 round-3 P2 (Codex line 1375): when ``POSTGRES_FORCE_IPV4``
    is true, ``connect_with_retry`` iterates 2 DSN candidates per call.
    The deadline-clamp helper MUST divide the remaining budget by 2
    so the SUM of per-candidate connect timeouts cannot exceed the
    remaining deadline."""
    import app.core.risk_manager as rm_mod

    monkeypatch.delenv("POSTGRES_FORCE_IPV4", raising=False)
    assert rm_mod._bridge_dsn_candidate_count() in (1, 2), (
        "candidate count must be 1 or 2"
    )

    monkeypatch.setenv("POSTGRES_FORCE_IPV4", "true")
    assert rm_mod._bridge_dsn_candidate_count() == 2, (
        "POSTGRES_FORCE_IPV4=true MUST report 2 candidates"
    )

    # With 2 candidates and ~4s remaining, the clamp MUST return
    # AT MOST 2s so the sum cannot exceed the deadline.
    deadline = rm_mod._BridgeDeadline(timeout_seconds=4.0)
    # No sleeping; remaining ≈ 4s.
    clamp = deadline.connect_timeout_seconds(
        min_default=5, candidate_count=2,
    )
    assert clamp <= 2, (
        "PR #258 round-3 P2 (Codex line 1375): 2 candidates × clamp "
        "must fit in remaining budget; got clamp=%d (need <=2)" % clamp
    )

    # With 1 candidate and ~4s remaining, the clamp is bounded by
    # min_default (5) but the budget allows up to 4s.
    clamp_one = deadline.connect_timeout_seconds(
        min_default=5, candidate_count=1,
    )
    assert clamp_one >= 2 and clamp_one <= 4


def test_bridge_repairs_stale_save_after_post_save_mismatch(
    tmp_path, monkeypatch, caplog,
):
    """PR #258 round-3 P2 (Codex line 1445): on post-save fingerprint
    mismatch, the bridge has ALREADY written its stale TRIPPED
    snapshot to Postgres. Without the repair, the stale row remains
    until the next save cycle and can be observed by readers
    (KillSwitchManager.load_state on restart sees stale TRIPPED).
    The bridge MUST re-save the current manager state to overwrite
    the stale row."""
    monkeypatch.setenv("RISK_BRIDGE_RETRY_DELAYS_SECONDS", "0,0,0")

    rm = _build_risk_manager(tmp_path)
    now = _force_legacy_trip_state(rm)

    from app.risk.kill_switch import KillSwitchState

    class _OperatorClearRaceKsm(_StubKillSwitchManager):
        """Simulate an operator clear/rearm landing between the final
        pre_attempt_check and the post-save read — so save_state
        succeeds (bridge writes stale TRIPPED) but post-save get_record
        returns the CURRENT (post-mutation) record with different id.
        The repair MUST then re-call save_state with the new state."""

        def __init__(self):
            super().__init__()
            self._record_id = "rec-stale"
            self._save_succeeded = False

        def get_record(self, scope, scope_id):
            from types import SimpleNamespace as _SN
            return _SN(
                id=self._record_id,
                state=KillSwitchState.TRIPPED,
                scope=scope, scope_id=scope_id,
                trip_reason=None, tripped_by=None,
                block_exits=False,
                updated_at="2026-05-12T00:00:00Z",
            )

        def is_tripped(self, scope, scope_id):
            return True

        def trip(self, scope, scope_id, reason, actor):
            raise ValueError("already TRIPPED")

        def save_state(self, conn):
            self.save_state_calls += 1
            self._save_succeeded = True
            # On the FIRST save, mutate the record id to simulate the
            # operator landing between pre_attempt_check and the
            # post-save read. The repair save (#2) should NOT mutate.
            if self.save_state_calls == 1:
                self._record_id = "rec-after-operator-rearm"

    ksm = _OperatorClearRaceKsm()
    runtime = SimpleNamespace(kill_switch_manager=ksm)

    class _NoopConn:
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    with patch("app.hub.runtime.get_hub_runtime", return_value=runtime), \
         patch("app.data.postgres.connect_with_retry", return_value=_NoopConn()), \
         patch("app.data.postgres.get_control_plane_dsn", return_value="x"):
        rm._check_kill_switch(now)

    # PR #258 round-3 P2 (Codex line 1445): the bridge MUST call
    # save_state TWICE — original (with stale snapshot) + repair
    # (with current post-mutation snapshot) — so operator intent
    # wins in Postgres.
    assert ksm.save_state_calls == 2, (
        "post-save mismatch MUST trigger a repair save (PR #258 "
        "round-3 P2, Codex line 1445); got %d calls"
        % ksm.save_state_calls
    )
    assert any(
        "kill_switch_bridge_post_save_repair_succeeded" in rec.message
        for rec in caplog.records
    )
    # Bridge MUST NOT mark succeeded; next cycle re-validates.
    assert rm._durable_kill_switch_bridge_succeeded is False


def test_bridge_preserves_pending_reemit_across_cycles(
    tmp_path, monkeypatch, caplog,
):
    """PR #258 round-3 P2 (Codex line 1266): when an unaudited
    partial-trip is detected but the cycle exits before the re-emit
    completes (e.g. emit_audit_event raises), the local
    ``first_attempt_post_mutation_error`` flag is lost. Without
    persisting the marker, the NEXT cycle sees TRIPPED in
    get_record, sets already_tripped=True, and skips the re-emit
    entirely — the audit miss is never repaired.

    Fix: persist ``_pending_audit_reemit`` on RiskManager. On the
    next cycle, replay the re-emit against the existing TRIPPED
    record. Clear on success."""
    monkeypatch.setenv("RISK_BRIDGE_RETRY_DELAYS_SECONDS", "0,0,0")

    rm = _build_risk_manager(tmp_path)
    now = _force_legacy_trip_state(rm)

    PartialKsm = _build_partial_trip_manager_class()
    flaky = PartialKsm()
    runtime = SimpleNamespace(kill_switch_manager=flaky)

    class _NoopConn:
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    # First call: audit re-emit fails (env unwritable, etc.).
    fail_count = {"n": 0}

    def _emit_first_fails_then_ok(**kwargs):
        fail_count["n"] += 1
        if fail_count["n"] == 1:
            raise IOError("disk full on first cycle")
        return {"audit_id": "stub"}

    with patch("app.hub.runtime.get_hub_runtime", return_value=runtime), \
         patch("app.data.postgres.connect_with_retry", return_value=_NoopConn()), \
         patch("app.data.postgres.get_control_plane_dsn", return_value="x"), \
         patch(
             "app.core.audit_log.emit_audit_event",
             side_effect=_emit_first_fails_then_ok,
         ):
        rm._check_kill_switch(now)
        # First cycle: re-emit failed, bridge unsucceeded, marker set.
        assert rm._pending_audit_reemit is True
        assert rm._durable_kill_switch_bridge_succeeded is False

        # Simulate another evaluate cycle — legacy flag still set,
        # so the retry path in evaluate_account_loss calls the
        # bridge again. The bridge sees already_tripped=True and
        # MUST replay the re-emit because _pending_audit_reemit
        # is True.
        rm.kill_switch_activated = True
        rm.daily_realized_pnl = -3000.0
        rm._propagate_kill_switch_to_durable_manager(
            reasons=["legacy_already_tripped_retry"], source="test",
        )

    # Second cycle's re-emit succeeded; marker cleared and bridge
    # marked succeeded.
    assert fail_count["n"] >= 2, (
        "second cycle MUST replay the audit re-emit on the "
        "already_tripped path (PR #258 round-3 P2, Codex line 1266)"
    )
    assert rm._pending_audit_reemit is False, (
        "marker MUST clear on successful re-emit (PR #258 round-3 P2)"
    )
    assert any(
        "kill_switch_bridge_pending_audit_reemit_replay" in rec.message
        for rec in caplog.records
    )


def test_bridge_worst_case_wallclock_under_deadline_with_dsn_candidates(
    tmp_path, monkeypatch,
):
    """PR #258 round-3 (overall): worst-case bridge wall-clock with
    DSN-candidate iteration AND audit re-emit AND post-save repair
    MUST stay under the deadline + a small grace window. Models the
    pessimistic case where every Postgres connect attempt waits its
    full per-candidate timeout."""
    import time as _time

    monkeypatch.setenv("RISK_BRIDGE_TOTAL_DEADLINE_SECONDS", "2")
    monkeypatch.setenv("RISK_BRIDGE_RETRY_DELAYS_SECONDS", "0,0,0")
    monkeypatch.setenv("POSTGRES_FORCE_IPV4", "true")

    rm = _build_risk_manager(tmp_path)
    now = _force_legacy_trip_state(rm)

    stub_ksm = _StubKillSwitchManager()
    runtime = SimpleNamespace(kill_switch_manager=stub_ksm)

    def _slow_connect(*_args, **_kwargs):
        # Model 2 DSN candidates × full connect timeout each.
        _time.sleep(1.0)
        _time.sleep(1.0)
        raise ConnectionError("simulated all-DSN-candidates exhausted")

    t0 = _time.monotonic()
    with patch("app.hub.runtime.get_hub_runtime", return_value=runtime), \
         patch("app.data.postgres.connect_with_retry", side_effect=_slow_connect), \
         patch("app.data.postgres.get_control_plane_dsn", return_value="x"):
        rm._check_kill_switch(now)
    elapsed = _time.monotonic() - t0

    # 2s deadline + worst-case 2s burn on the failing connect attempt
    # + small grace ≤ ~5s. Without all 3 round-3 fixes this would
    # be 16s+ on the IPv4-prefer path with 4 retries.
    assert elapsed < 6.0, (
        "Worst-case bridge wall-clock under deadline+DSN candidates "
        "MUST stay bounded (PR #258 round-3); took %.2fs" % elapsed
    )
    assert rm._durable_kill_switch_bridge_succeeded is False


# ---------------------------------------------------------------------------
# PR #258 round-5 — newly-discovered P1/P2 findings
# ---------------------------------------------------------------------------


def test_bridge_subsecond_budget_aborts_before_postgres_connect(monkeypatch):
    """PR #258 round-5 P1 (Codex line 343): when earlier bridge work
    has consumed almost all of the budget, the deadline-clamp helper
    MUST raise ``_BridgeDeadlineExceeded`` rather than clamping up to
    1s and entering ``connect_with_retry``. With ``candidate_count=2``
    and remaining < 2.0s, psycopg's per-candidate minimum 1s timeout
    would let a single connect call consume ``2 × 1s = 2s`` past the
    deadline."""
    import time as _time
    import app.core.risk_manager as rm_mod

    # Build a deadline near-expiry.
    deadline = rm_mod._BridgeDeadline(timeout_seconds=0.5)
    # Burn most of the budget; remaining ≈ 0.0–0.1s.
    _time.sleep(0.45)

    # With 2 candidates and < 2s remaining, the helper MUST raise.
    with pytest.raises(rm_mod._BridgeDeadlineExceeded):
        deadline.connect_timeout_seconds(
            min_default=2, candidate_count=2,
        )

    # With 1 candidate the same call also fails because remaining < 1.0.
    with pytest.raises(rm_mod._BridgeDeadlineExceeded):
        deadline.connect_timeout_seconds(
            min_default=2, candidate_count=1,
        )


def test_bridge_subsecond_budget_does_not_enter_psycopg(
    tmp_path, monkeypatch, caplog,
):
    """PR #258 round-5 P1 (Codex line 343): end-to-end — when the
    bridge enters the save step with subsecond budget remaining (the
    trip/get_record steps having burned the budget), ``_do_save``
    MUST NOT enter ``connect_with_retry`` at all. The deadline-clamp
    helper inside ``_do_save`` raises and propagates out as a
    deadline-exceeded short-circuit through ``_retry_with_backoff``.
    """
    import time as _time

    monkeypatch.setenv("RISK_BRIDGE_TOTAL_DEADLINE_SECONDS", "0.6")
    monkeypatch.setenv("RISK_BRIDGE_RETRY_DELAYS_SECONDS", "0,0,0")
    monkeypatch.setenv("POSTGRES_FORCE_IPV4", "true")  # 2 candidates

    rm = _build_risk_manager(tmp_path)
    now = _force_legacy_trip_state(rm)

    stub_ksm = _StubKillSwitchManager()
    runtime = SimpleNamespace(kill_switch_manager=stub_ksm)

    # Make get_record artificially slow so we enter save with < 1s
    # remaining.
    orig_get = stub_ksm.get_record

    def _slow_get(scope, scope_id):
        _time.sleep(0.55)  # burn most of 0.6s budget
        return orig_get(scope, scope_id)

    stub_ksm.get_record = _slow_get  # type: ignore[assignment]

    connect_calls = {"n": 0}

    def _track_connect(*_args, **_kwargs):
        connect_calls["n"] += 1
        raise ConnectionError("MUST NOT be reached on subsecond budget")

    with patch("app.hub.runtime.get_hub_runtime", return_value=runtime), \
         patch("app.data.postgres.connect_with_retry", side_effect=_track_connect), \
         patch("app.data.postgres.get_control_plane_dsn", return_value="x"):
        rm._check_kill_switch(now)

    # PR #258 round-5 P1 (Codex line 343): the save step MUST refuse
    # to start a Postgres connect when remaining < candidate_count.
    # connect_with_retry is the audit path inside ksm.trip which we
    # don't model here, so the count for save should be 0 entries
    # from the save path. The stub ksm.trip() doesn't call postgres
    # directly, so any non-zero count would be from the save attempt
    # — which the deadline-clamp MUST have blocked.
    assert connect_calls["n"] == 0, (
        "save step MUST NOT enter connect_with_retry with subsecond "
        "budget (PR #258 round-5 P1, Codex line 343); got %d calls"
        % connect_calls["n"]
    )
    assert rm._durable_kill_switch_bridge_succeeded is False


def test_bridge_initial_trip_audit_write_is_bounded(
    tmp_path, monkeypatch,
):
    """PR #258 round-5 P1 (Codex line 1208/1213): the initial
    ``ksm.trip()`` call's nested audit dual-write to Postgres MUST be
    bounded by env overrides so it cannot block for default 3 ×
    5s = 15s during a Postgres outage.

    Verify by patching ``app.core.audit_log.emit_audit_event`` and
    asserting the two env vars are tight (== 1 for max_attempts; <=
    remaining-budget for connect-timeout) at the moment the audit
    dual-write would run."""
    monkeypatch.setenv("RISK_BRIDGE_TOTAL_DEADLINE_SECONDS", "3")
    monkeypatch.setenv("RISK_BRIDGE_RETRY_DELAYS_SECONDS", "0,0,0")
    monkeypatch.setenv("POSTGRES_CONNECT_TIMEOUT_SECONDS", "30")
    monkeypatch.delenv("AUDIT_POSTGRES_MAX_ATTEMPTS", raising=False)

    rm = _build_risk_manager(tmp_path)
    now = _force_legacy_trip_state(rm)

    observed_env: list = []

    # ksm.trip is the stub that does NOT call emit_audit_event itself;
    # to model the real KillSwitchManager.trip() emitting audit AFTER
    # mutation we wrap the stub.
    class _AuditEmittingKsm(_StubKillSwitchManager):
        def trip(self, scope, scope_id, reason, actor):
            # Snapshot env vars at the moment trip's audit dual-write
            # would run — BEFORE returning the record.
            observed_env.append({
                "POSTGRES_CONNECT_TIMEOUT_SECONDS": os.environ.get(
                    "POSTGRES_CONNECT_TIMEOUT_SECONDS"
                ),
                "AUDIT_POSTGRES_MAX_ATTEMPTS": os.environ.get(
                    "AUDIT_POSTGRES_MAX_ATTEMPTS"
                ),
            })
            return super().trip(scope, scope_id, reason, actor)

    ksm = _AuditEmittingKsm()
    runtime = SimpleNamespace(kill_switch_manager=ksm)

    class _NoopConn:
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    with patch("app.hub.runtime.get_hub_runtime", return_value=runtime), \
         patch("app.data.postgres.connect_with_retry", return_value=_NoopConn()), \
         patch("app.data.postgres.get_control_plane_dsn", return_value="x"):
        rm._check_kill_switch(now)

    assert observed_env, "ksm.trip MUST be invoked"
    inside = observed_env[0]
    assert inside["AUDIT_POSTGRES_MAX_ATTEMPTS"] == "1", (
        "initial trip MUST set AUDIT_POSTGRES_MAX_ATTEMPTS=1 to "
        "bound the audit dual-write (PR #258 round-5 P1, Codex "
        "line 1208/1213); saw %r" % inside["AUDIT_POSTGRES_MAX_ATTEMPTS"]
    )
    inside_timeout = inside["POSTGRES_CONNECT_TIMEOUT_SECONDS"]
    assert inside_timeout is not None and int(inside_timeout) <= 3, (
        "initial trip MUST tighten POSTGRES_CONNECT_TIMEOUT_SECONDS "
        "to <= remaining deadline (PR #258 round-5 P1, Codex line "
        "1208/1213); saw %r" % inside_timeout
    )
    # And after the bridge returns, the env vars MUST be restored.
    assert os.environ.get("POSTGRES_CONNECT_TIMEOUT_SECONDS") == "30", (
        "env override MUST be restored after the trip loop exits"
    )
    assert os.environ.get("AUDIT_POSTGRES_MAX_ATTEMPTS") is None, (
        "AUDIT_POSTGRES_MAX_ATTEMPTS MUST be removed (no prior value) "
        "after the trip loop exits"
    )


def test_bridge_marker_persists_immediately_on_post_mutation_detection(
    tmp_path, monkeypatch,
):
    """PR #258 round-5 P1 (Codex line 1247): when an attempt mutates
    the in-memory record then raises, ``self._pending_audit_reemit``
    MUST be set IMMEDIATELY on detection — not only later in the
    ValueError branch. If a subsequent deadline fires before the
    ValueError handler runs, the marker survives so the NEXT cycle's
    already-tripped repair path replays the audit.

    Simulate: trip mutates state then raises; the loop hits the
    deadline before doing anything else. The marker MUST be set on
    the RiskManager."""
    monkeypatch.setenv("RISK_BRIDGE_TOTAL_DEADLINE_SECONDS", "0.5")
    monkeypatch.setenv("RISK_BRIDGE_RETRY_DELAYS_SECONDS", "0,0,0")

    rm = _build_risk_manager(tmp_path)
    now = _force_legacy_trip_state(rm)

    import time as _time

    class _MutateThenRaiseAndStallKsm(_StubKillSwitchManager):
        def __init__(self):
            super().__init__()
            self.trip_calls_n = 0

        def trip(self, scope, scope_id, reason, actor):
            self.trip_calls_n += 1
            # First attempt: mutate in-memory state then raise.
            self._state_text = "TRIPPED"
            # Burn budget so the deadline is exceeded BEFORE the
            # ValueError branch can run its re-emit.
            _time.sleep(0.6)
            raise IOError("simulated audit disk-full after mutation")

    flaky = _MutateThenRaiseAndStallKsm()
    runtime = SimpleNamespace(kill_switch_manager=flaky)

    class _NoopConn:
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    with patch("app.hub.runtime.get_hub_runtime", return_value=runtime), \
         patch("app.data.postgres.connect_with_retry", return_value=_NoopConn()), \
         patch("app.data.postgres.get_control_plane_dsn", return_value="x"):
        rm._check_kill_switch(now)

    # PR #258 round-5 P1 (Codex line 1247): the persistent marker MUST
    # be set so the next cycle repairs the audit. Without the fix,
    # the local flag was lost when the deadline fired before the
    # ValueError handler.
    assert rm._pending_audit_reemit is True, (
        "pending_audit_reemit MUST be set IMMEDIATELY on post-mutation "
        "detection (PR #258 round-5 P1, Codex line 1247)"
    )
    assert rm._durable_kill_switch_bridge_succeeded is False


def test_bridge_detects_post_mutation_failure_on_every_retry(
    tmp_path, monkeypatch, caplog,
):
    """PR #258 round-5 P1 (Codex line 1234): post-mutation detection
    MUST run on every retry, not only attempt 1. Model: attempt 1
    raises BEFORE mutating; attempt 2 mutates THEN raises; attempt 3
    raises ValueError (state already TRIPPED). Without the fix, the
    bridge would silently mark success with no audit row."""
    monkeypatch.setenv("RISK_BRIDGE_RETRY_DELAYS_SECONDS", "0,0,0")

    rm = _build_risk_manager(tmp_path)
    now = _force_legacy_trip_state(rm)

    class _MutateOnSecondAttemptKsm(_StubKillSwitchManager):
        def __init__(self):
            super().__init__()
            self.n = 0

        def trip(self, scope, scope_id, reason, actor):
            self.n += 1
            if self.n == 1:
                # Pre-mutation failure (no state change).
                raise ConnectionError("attempt 1 transient blip")
            if self.n == 2:
                # Mutate THEN raise (audit failed post-mutation).
                self._state_text = "TRIPPED"
                raise IOError("attempt 2 audit-write disk-full")
            # Subsequent attempts: state is TRIPPED so raise ValueError.
            raise ValueError(
                "Cannot trip kill switch: current state is TRIPPED"
            )

    flaky = _MutateOnSecondAttemptKsm()
    runtime = SimpleNamespace(kill_switch_manager=flaky)

    class _NoopConn:
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    audit_emissions: list = []

    def _capture_audit(**kwargs):
        audit_emissions.append(kwargs)
        return {"audit_id": "stub"}

    with patch("app.hub.runtime.get_hub_runtime", return_value=runtime), \
         patch("app.data.postgres.connect_with_retry", return_value=_NoopConn()), \
         patch("app.data.postgres.get_control_plane_dsn", return_value="x"), \
         patch("app.core.audit_log.emit_audit_event", side_effect=_capture_audit):
        rm._check_kill_switch(now)

    # PR #258 round-5 P1 (Codex line 1234): detection MUST fire on
    # attempt 2 — without the every-retry check we'd silently mark
    # success with no audit row.
    assert any(
        "kill_switch_bridge_unaudited_partial_trip" in rec.message
        and rec.levelname == "ERROR"
        for rec in caplog.records
    ), (
        "bridge MUST detect post-mutation audit failure on every retry "
        "(PR #258 round-5 P1, Codex line 1234)"
    )
    # The audit MUST be re-emitted via the fallback path.
    assert any(
        evt.get("metadata", {}).get("bridge_reemit") is True
        for evt in audit_emissions
    ), (
        "bridge MUST re-emit audit when post-mutation failure detected "
        "on retry > 1 (PR #258 round-5 P1, Codex line 1234)"
    )


def test_audit_postgres_max_attempts_env_override():
    """PR #258 round-5 P2 (Codex line 1845): the audit dual-write
    helper MUST honor the ``AUDIT_POSTGRES_MAX_ATTEMPTS`` env var so
    callers (the bridge re-emit, the initial trip loop) can bound
    its wall-clock by setting it to ``1``."""
    import app.core.audit_log as audit_mod

    # Default is 3.
    if "AUDIT_POSTGRES_MAX_ATTEMPTS" in os.environ:
        del os.environ["AUDIT_POSTGRES_MAX_ATTEMPTS"]
    assert audit_mod._audit_postgres_max_attempts() == 3

    os.environ["AUDIT_POSTGRES_MAX_ATTEMPTS"] = "1"
    try:
        assert audit_mod._audit_postgres_max_attempts() == 1
    finally:
        del os.environ["AUDIT_POSTGRES_MAX_ATTEMPTS"]

    # Malformed -> default.
    os.environ["AUDIT_POSTGRES_MAX_ATTEMPTS"] = "garbage"
    try:
        assert audit_mod._audit_postgres_max_attempts() == 3
    finally:
        del os.environ["AUDIT_POSTGRES_MAX_ATTEMPTS"]

    # Non-positive -> default.
    os.environ["AUDIT_POSTGRES_MAX_ATTEMPTS"] = "0"
    try:
        assert audit_mod._audit_postgres_max_attempts() == 3
    finally:
        del os.environ["AUDIT_POSTGRES_MAX_ATTEMPTS"]


def test_bridge_reemit_tightens_audit_max_attempts_env(tmp_path, monkeypatch):
    """PR #258 round-5 P2 (Codex line 1845): the audit re-emit helper
    MUST set ``AUDIT_POSTGRES_MAX_ATTEMPTS=1`` for the duration of
    the ``emit_audit_event`` call so the nested dual-write makes
    exactly one connect attempt rather than the default 3 (which
    would compound with the per-attempt timeout to blow the
    deadline). Verify the override is applied during the call AND
    restored afterwards."""
    monkeypatch.setenv("RISK_BRIDGE_TOTAL_DEADLINE_SECONDS", "3")
    monkeypatch.setenv("RISK_BRIDGE_RETRY_DELAYS_SECONDS", "0,0,0")
    monkeypatch.delenv("AUDIT_POSTGRES_MAX_ATTEMPTS", raising=False)

    rm = _build_risk_manager(tmp_path)

    captured: list = []

    def _capture(**kwargs):
        captured.append(
            os.environ.get("AUDIT_POSTGRES_MAX_ATTEMPTS")
        )
        return {"audit_id": "stub"}

    from types import SimpleNamespace as _SN
    from app.risk.kill_switch import KillSwitchState

    rec = _SN(
        id="rec-x",
        state=KillSwitchState.TRIPPED,
        scope=None,
        scope_id="GLOBAL",
        trip_reason=None,
        tripped_by=None,
    )

    deadline = importlib.import_module(
        "app.core.risk_manager"
    )._BridgeDeadline(timeout_seconds=3.0)

    with patch("app.core.audit_log.emit_audit_event", side_effect=_capture):
        ok = rm._reemit_kill_switch_bridge_audit(
            record=rec, bridge_reason="test", deadline=deadline,
        )

    assert ok is True
    assert captured, "emit_audit_event MUST be invoked"
    assert captured[0] == "1", (
        "bridge MUST set AUDIT_POSTGRES_MAX_ATTEMPTS=1 during the "
        "audit re-emit (PR #258 round-5 P2, Codex line 1845); saw %r"
        % captured[0]
    )
    # After the call returns the env var MUST be removed (no prior).
    assert os.environ.get("AUDIT_POSTGRES_MAX_ATTEMPTS") is None, (
        "AUDIT_POSTGRES_MAX_ATTEMPTS MUST be removed after _reemit "
        "returns (PR #258 round-5 P2)"
    )


def test_bridge_worst_case_wallclock_round5_under_deadline_full_outage(
    tmp_path, monkeypatch,
):
    """PR #258 round-5: combined worst-case — the bridge MUST stay
    under the deadline + small grace EVEN when:
      - Postgres is fully out (every connect raises after 2 DSN
        candidates × full per-candidate timeout).
      - The initial trip's nested audit dual-write would otherwise
        consume default 3 attempts × full timeout.
      - The re-emit path would otherwise also consume 3 × timeout.
    With round-5 env overrides (initial trip + re-emit set
    AUDIT_POSTGRES_MAX_ATTEMPTS=1), the worst case is bounded by the
    deadline."""
    import time as _time

    monkeypatch.setenv("RISK_BRIDGE_TOTAL_DEADLINE_SECONDS", "2")
    monkeypatch.setenv("RISK_BRIDGE_RETRY_DELAYS_SECONDS", "0,0,0")
    monkeypatch.setenv("POSTGRES_FORCE_IPV4", "true")  # 2 candidates

    rm = _build_risk_manager(tmp_path)
    now = _force_legacy_trip_state(rm)

    stub_ksm = _StubKillSwitchManager()
    runtime = SimpleNamespace(kill_switch_manager=stub_ksm)

    def _slow_connect(*_args, **_kwargs):
        # Model 2 DSN candidates × full connect timeout each.
        _time.sleep(1.0)
        _time.sleep(1.0)
        raise ConnectionError("simulated full outage")

    t0 = _time.monotonic()
    with patch("app.hub.runtime.get_hub_runtime", return_value=runtime), \
         patch("app.data.postgres.connect_with_retry", side_effect=_slow_connect), \
         patch("app.data.postgres.get_control_plane_dsn", return_value="x"):
        rm._check_kill_switch(now)
    elapsed = _time.monotonic() - t0

    # Same bound as round-3 worst-case test but expanded to cover
    # round-5 scenarios (initial trip audit, re-emit audit).
    assert elapsed < 6.0, (
        "Worst-case bridge wall-clock (round-5 full-outage model) "
        "MUST stay bounded; took %.2fs" % elapsed
    )
    assert rm._durable_kill_switch_bridge_succeeded is False
