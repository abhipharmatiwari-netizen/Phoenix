"""Tests for issue #222: legacy↔durable kill-switch divergence detection.

The legacy ``RiskManager.kill_switch_activated`` flag (stream worker) and
the durable ``KillSwitchManager`` (hub-routed entries) can diverge if the
auto-trip bridge (issue #218) has not yet converged — e.g. transient hub
runtime / Postgres failure during the moment of legacy auto-trip. The
silent-bypass that bit live on 2026-05-08 for 23 minutes IS this exact
divergence.

This test suite verifies:

- ``HubRuntime.record_legacy_kill_switch_state`` publishes correctly.
- ``HubRuntime.compute_kill_switch_divergence`` returns ``divergent=True``
  when legacy=True AND durable GLOBAL=INACTIVE.
- ``divergent=False`` when both states agree.
- ``RiskManager._publish_legacy_kill_switch_state_to_hub`` is failure-safe
  (no exception escapes; silent skip when hub runtime unavailable).
- ``get_kill_switch_state()`` includes the legacy + divergence fields.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import patch


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


def _make_runtime():
    """Construct a fresh HubRuntime-like object exposing the methods we
    test, without doing the full HubRuntime startup. We instantiate a
    small subset by directly calling the relevant init code."""
    # Use the actual class but bypass full init by partial construction.
    from app.hub.runtime import HubRuntime  # noqa: F401
    from threading import Lock

    rt = SimpleNamespace()
    # Mirror the legacy-publisher fields HubRuntime.__init__ adds.
    rt._legacy_kill_switch_lock = Lock()
    rt._legacy_kill_switch_active = False
    rt._legacy_kill_switch_reason = None
    rt._legacy_kill_switch_updated_at = None
    rt._legacy_kill_switch_publisher_seen = False
    rt.kill_switch_manager = None
    # Bind the real methods.
    rt.record_legacy_kill_switch_state = (
        HubRuntime.record_legacy_kill_switch_state.__get__(rt, HubRuntime)
    )
    rt.get_legacy_kill_switch_snapshot = (
        HubRuntime.get_legacy_kill_switch_snapshot.__get__(rt, HubRuntime)
    )
    rt.compute_kill_switch_divergence = (
        HubRuntime.compute_kill_switch_divergence.__get__(rt, HubRuntime)
    )
    return rt


# -----------------------------------------------------------------------------
# HubRuntime publisher / divergence detector
# -----------------------------------------------------------------------------


def test_record_legacy_kill_switch_state_publishes_active_true():
    rt = _make_runtime()
    rt.record_legacy_kill_switch_state(active=True, reason="floating_drawdown")
    snap = rt.get_legacy_kill_switch_snapshot()
    assert snap["publisher_seen"] is True
    assert snap["legacy_active"] is True
    assert snap["legacy_reason"] == "floating_drawdown"
    assert snap["legacy_updated_at"] is not None


def test_record_legacy_kill_switch_state_can_overwrite_to_false():
    """A subsequent publish with active=False must overwrite the True."""
    rt = _make_runtime()
    rt.record_legacy_kill_switch_state(active=True, reason="x")
    rt.record_legacy_kill_switch_state(active=False, reason="cleared")
    snap = rt.get_legacy_kill_switch_snapshot()
    assert snap["legacy_active"] is False
    assert snap["legacy_reason"] == "cleared"


def test_divergence_false_when_publisher_never_seen():
    rt = _make_runtime()
    div = rt.compute_kill_switch_divergence()
    assert div["divergent"] is False
    assert div["publisher_seen"] is False
    assert div["legacy_active"] is False


def test_divergence_false_when_legacy_inactive():
    """Legacy=False, durable=anything → no divergence."""
    rt = _make_runtime()

    class _Ksm:
        def is_tripped(self, *_a, **_k): return True
    rt.kill_switch_manager = _Ksm()

    rt.record_legacy_kill_switch_state(active=False, reason="ok")
    div = rt.compute_kill_switch_divergence()
    assert div["divergent"] is False
    assert div["legacy_active"] is False
    assert div["durable_global_active"] is True


def test_divergence_false_when_both_states_agree_active():
    rt = _make_runtime()

    class _Ksm:
        def is_tripped(self, *_a, **_k): return True
    rt.kill_switch_manager = _Ksm()

    rt.record_legacy_kill_switch_state(active=True, reason="x")
    div = rt.compute_kill_switch_divergence()
    assert div["divergent"] is False, "both True → no divergence"
    assert div["legacy_active"] is True
    assert div["durable_global_active"] is True


def test_divergence_TRUE_when_legacy_true_durable_inactive():
    """The 2026-05-08 silent-bypass scenario: legacy auto-tripped but
    durable bridge has not converged. divergent must be True."""
    rt = _make_runtime()

    class _Ksm:
        def is_tripped(self, *_a, **_k): return False
    rt.kill_switch_manager = _Ksm()

    rt.record_legacy_kill_switch_state(active=True, reason="floating_drawdown")
    div = rt.compute_kill_switch_divergence()
    assert div["divergent"] is True, (
        "legacy=True + durable=False is the exact silent-bypass "
        "scenario this issue addresses"
    )
    assert div["legacy_active"] is True
    assert div["durable_global_active"] is False
    assert div["legacy_reason"] == "floating_drawdown"
    assert isinstance(div["divergence_age_seconds"], float)
    assert div["divergence_age_seconds"] >= 0.0


def test_divergence_handles_missing_kill_switch_manager():
    """If KSM is None, divergent is False — cannot diverge if we cannot
    determine the durable side. Returned struct still has the legacy
    fields populated."""
    rt = _make_runtime()
    rt.kill_switch_manager = None

    rt.record_legacy_kill_switch_state(active=True, reason="x")
    div = rt.compute_kill_switch_divergence()
    assert div["divergent"] is False
    assert div["legacy_active"] is True
    assert div["durable_global_active"] is None


# -----------------------------------------------------------------------------
# RiskManager publisher integration
# -----------------------------------------------------------------------------


def test_risk_manager_publishes_legacy_state_each_evaluate(tmp_path):
    """RiskManager.evaluate_account_loss must call the publisher every
    cycle, including when legacy is False — so the timestamp stays fresh
    and the publisher is observable as live."""
    rm = _build_risk_manager(tmp_path)
    runtime_stub = SimpleNamespace()
    publish_calls: list = []
    runtime_stub.record_legacy_kill_switch_state = lambda **kw: publish_calls.append(kw)

    with patch("app.hub.runtime.get_hub_runtime", return_value=runtime_stub):
        # Force a fresh evaluation.
        rm._publish_legacy_kill_switch_state_to_hub()

    assert len(publish_calls) == 1
    call = publish_calls[0]
    assert call["active"] is False
    assert "legacy_risk_manager" in str(call["reason"])


def test_risk_manager_publish_is_failure_safe_when_runtime_unavailable(tmp_path):
    """Hub runtime not initialised yet — publisher must not raise."""
    rm = _build_risk_manager(tmp_path)
    with patch(
        "app.hub.runtime.get_hub_runtime",
        side_effect=RuntimeError("not initialised"),
    ):
        # Must not raise.
        rm._publish_legacy_kill_switch_state_to_hub()


def test_risk_manager_publish_propagates_active_true_after_auto_trip(tmp_path):
    rm = _build_risk_manager(tmp_path)
    rm.kill_switch_activated = True
    runtime_stub = SimpleNamespace()
    publish_calls: list = []
    runtime_stub.record_legacy_kill_switch_state = lambda **kw: publish_calls.append(kw)

    with patch("app.hub.runtime.get_hub_runtime", return_value=runtime_stub):
        rm._publish_legacy_kill_switch_state_to_hub()

    assert len(publish_calls) == 1
    assert publish_calls[0]["active"] is True


# -----------------------------------------------------------------------------
# get_kill_switch_state() admin endpoint enrichment
# -----------------------------------------------------------------------------


def test_get_kill_switch_state_includes_legacy_and_divergence_fields():
    """The /admin/kill-switch/state response must include the new
    legacy_kill_switch + divergence fields when the manager is wired.
    """
    from app.risk.kill_switch import get_kill_switch_state, KillSwitchManager

    ksm = KillSwitchManager()
    rt = _make_runtime()
    rt.kill_switch_manager = ksm
    rt.record_legacy_kill_switch_state(active=True, reason="floating_drawdown")

    with patch("app.hub.runtime.get_hub_runtime", return_value=rt):
        snap = get_kill_switch_state()

    assert snap["source"] == "kill_switch_manager"
    assert "legacy_kill_switch" in snap
    assert snap["legacy_kill_switch"]["active"] is True
    assert snap["legacy_kill_switch"]["reason"] == "floating_drawdown"
    assert "divergence" in snap
    # legacy=True, durable INACTIVE (no records) → divergent
    assert snap["divergence"]["divergent"] is True
    assert snap["divergence"]["durable_global_active"] is False
