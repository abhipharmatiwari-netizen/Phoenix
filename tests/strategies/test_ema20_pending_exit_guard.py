"""Regression tests for the per-label pending-exit guard in
``Ema20Strategy._exit_position``.

Background
----------
On 2026-05-07 the live A1 NG short fired *two* stop-loss exit orders
(broker_order_ids 260507001449079 and 260507001449159) ~11s apart from
the same managed position. The first exit was emitted by the tick-level
SL signal handler; the second was emitted by the state-reconciliation
handler, which re-checked the broker's position quantity (via
``state_store.get_positions``) and saw the position still open because
the fill from exit #1 had not yet propagated back to ``state_store``.
The duplicate exit flipped the position from a 1-lot SHORT to a fresh
1-lot LONG (qty -1250 + +2500 = +1250) and parked
``internal_position_records`` in
``RECOVERY_PENDING / illegal_transition_blocked`` for the rest of the
session.

Root cause: the global ``_exit_in_flight`` flag was set True before
``place_order_via_bridge`` and immediately cleared in the surrounding
``finally:`` block — so it only protected against synchronous re-entry
during the bridge call itself (a few hundred milliseconds). It did not
prevent a second ``_exit_position`` invocation after the broker had
ACKed the first order but before the fill had been observed.

Fix: a per-label guard ``self._pending_exit_by_label: Dict[str, float]``
on ``Ema20Strategy``. Set when the exit is submitted; cleared on
failure paths so retries can fire; left set on success so the next
``_exit_position`` call on the same label is suppressed for up to
``EMA20_PENDING_EXIT_MAX_SECONDS`` (default 60s). Watchdog auto-clears
the guard after the timeout so a stuck flag cannot permanently block
exits.

Critically, the guard is *strategy-level keyed by ``option_label``*,
not a field on ``Ema20Position`` — because ``sync_position_from_risk_
_manager`` can replace the in-memory Position object while an exit is
in flight, and a per-position field would not survive that.
"""

from __future__ import annotations

import importlib
import time
import types
from datetime import datetime, timezone
from typing import List


from app.brokers.base import OrderResponse


MODULE_PATH = "app.strategies.ema20_strategy"


def _runtime_stub() -> types.SimpleNamespace:
    """Minimal hub runtime stub so place_order_via_bridge routing works."""
    return types.SimpleNamespace(
        routing_table=types.SimpleNamespace(
            get_routes_for_strategy=lambda _sid: [
                types.SimpleNamespace(broker_account_id="A1")
            ]
        ),
        state_store=types.SimpleNamespace(
            get_positions=lambda _acct: [
                types.SimpleNamespace(
                    symbol="NATURALGAS22MAY26255CE",
                    quantity=-1250,  # 1 lot SHORT — pre-fill state
                )
            ]
        ),
    )


def _make_strategy(monkeypatch):
    monkeypatch.setenv("NG_EMA20_REQUIRE_RSI_FALLING", "1")
    mod = importlib.import_module(MODULE_PATH)
    instrument_meta = {
        "NG_FUT": {"symbol": "NG_FUT"},
        "NG_ATM_CE_255": {
            "symbol": "NATURALGAS22MAY26255CE",
            "token": "561551",
            "lot_size": 1250,
            "exchange": "MCX",
        },
    }
    strategy = mod.Ema20Strategy(
        instrument_meta,
        env_prefix="NG_",
        underlying_label="NG_FUT",
        signal_timeframe=300,
        ema_period=20,
        min_atr=1.0,
        require_rsi_falling=False,
    )
    return mod, strategy


def _seed_short_position(mod, strategy, *, label: str = "NG_ATM_CE_255"):
    """Place a 1-lot SHORT NG ATM CE managed position into the strategy."""
    pos = mod.Ema20Position(
        option_label=label,
        broker_symbol="NATURALGAS22MAY26255CE",
        exchange="MCX",
        symbol_token="561551",
        qty=1,
        requested_lots=1,
        lot_size=1250,
        broker_qty=1250,
        entry_price=10.70,
        sl_price=12.85,  # 0.20 above entry — short hit SL when LTP >= sl_price
        tp_price=8.55,
        best_price=10.70,
        trail_active=False,
        entry_time=datetime(2026, 5, 7, 13, 10, tzinfo=timezone.utc),
    )
    strategy.position = pos
    strategy._managed_positions[label] = pos
    return pos


def _patch_bridge_success(monkeypatch, mod, calls: List[dict]):
    def fake_place(**kwargs):
        calls.append(kwargs)
        return OrderResponse(
            broker_order_id=f"260507001449{len(calls):03d}",
            status="SUCCESS",
            message="ok",
        )
    monkeypatch.setattr(mod, "place_order_via_bridge", fake_place)
    monkeypatch.setattr(mod, "_get_hub_runtime", lambda: _runtime_stub())


# ---- tests --------------------------------------------------------------

def test_first_exit_call_fires_and_arms_pending_guard(monkeypatch):
    """Path A: first exit submission. Verify the bridge is called and the
    per-label guard is armed for future _exit_position invocations."""
    mod, strategy = _make_strategy(monkeypatch)
    _seed_short_position(mod, strategy)
    calls: List[dict] = []
    _patch_bridge_success(monkeypatch, mod, calls)

    strategy._exit_position(reason="SL")

    assert len(calls) == 1, "first exit must reach the bridge"
    assert "NG_ATM_CE_255" in strategy._pending_exit_by_label, (
        "pending-exit guard must be armed for the label after the bridge "
        "call returns SUCCESS"
    )
    # Note: the success branch of _exit_position also calls
    # _drop_managed_position, so _managed_positions becomes empty in this
    # test — but the per-label guard survives so a re-restored position
    # cannot fire a duplicate exit.
    assert "NG_ATM_CE_255" not in strategy._managed_positions


def test_re_restored_position_cannot_fire_duplicate_exit(monkeypatch):
    """The actual 2026-05-07 NG scenario: Path A submits exit -> position
    drops -> sync_position_from_risk_manager re-restores the position
    (because state_store hasn't reflected the fill yet) -> Path B's
    _exit_position call sees a fresh position and would normally fire.
    The per-label guard MUST suppress it."""
    mod, strategy = _make_strategy(monkeypatch)
    _seed_short_position(mod, strategy)
    calls: List[dict] = []
    _patch_bridge_success(monkeypatch, mod, calls)

    # Path A: tick-level SL signal handler.
    strategy._exit_position(reason="SL")
    assert len(calls) == 1
    # Success path drops the managed position.
    assert "NG_ATM_CE_255" not in strategy._managed_positions
    # ...but the per-label guard remains armed.
    assert "NG_ATM_CE_255" in strategy._pending_exit_by_label

    # Simulate sync_position_from_risk_manager re-restoring the position
    # because state_store still shows -1250 (broker fill not yet propagated).
    _seed_short_position(mod, strategy)
    assert "NG_ATM_CE_255" in strategy._managed_positions

    # Path B: state-reconciliation handler fires _exit_position on the
    # re-restored position. WITHOUT the guard this would call the bridge a
    # second time -> duplicate exit -> position flip.
    strategy._exit_position(reason="SL")

    assert len(calls) == 1, (
        "second _exit_position call on a re-restored position must be "
        "suppressed while the prior exit's guard is still armed"
    )


def test_watchdog_auto_clears_stuck_guard_after_max_seconds(monkeypatch):
    """If the guard has been set for longer than
    EMA20_PENDING_EXIT_MAX_SECONDS (default 60), the next _exit_position
    call WARNs and proceeds anyway. Prevents a stuck guard from
    permanently blocking exits if fill observation is broken upstream."""
    mod, strategy = _make_strategy(monkeypatch)
    _seed_short_position(mod, strategy)
    calls: List[dict] = []
    _patch_bridge_success(monkeypatch, mod, calls)

    strategy._exit_position(reason="SL")
    assert len(calls) == 1

    # Backdate the guard to look like it has been pending for 2x the max age.
    strategy._pending_exit_by_label["NG_ATM_CE_255"] = (
        time.monotonic() - 2.0 * strategy._pending_exit_max_seconds
    )
    # Re-restore so we have a position to exit again.
    _seed_short_position(mod, strategy)

    strategy._exit_position(reason="SL")

    assert len(calls) == 2, (
        "watchdog must auto-clear a stuck guard older than max-age and "
        "allow the retry to fire"
    )


def test_failure_response_clears_guard_so_retry_cooldown_can_fire(monkeypatch):
    """When the broker REJECTs the exit, the guard must be cleared so the
    existing retry cooldown (_next_exit_retry_mono) can drive the retry.
    Otherwise a single broker hiccup would deadlock all future exits on
    that label for 60s."""
    mod, strategy = _make_strategy(monkeypatch)
    _seed_short_position(mod, strategy)
    calls: List[dict] = []

    def fake_place(**kwargs):
        calls.append(kwargs)
        return OrderResponse(
            broker_order_id="rejected-1",
            status="REJECTED",
            message="liquidity",
        )

    monkeypatch.setattr(mod, "place_order_via_bridge", fake_place)
    monkeypatch.setattr(mod, "_get_hub_runtime", lambda: _runtime_stub())

    strategy._exit_position(reason="SL")
    assert len(calls) == 1
    assert "NG_ATM_CE_255" not in strategy._pending_exit_by_label, (
        "guard must be cleared after broker REJECTED so retry cooldown "
        "can drive the next attempt"
    )


def test_guard_is_per_label_independent_positions_unaffected(monkeypatch):
    """A pending exit on one label must NOT block exits on a different
    label (e.g., NIFTY ATM CE under a different option). The guard is
    per-label, not strategy-global."""
    mod, strategy = _make_strategy(monkeypatch)
    # Seed two positions with different labels by extending instrument_meta.
    strategy.instrument_meta["NG_ATM_CE_260"] = {
        "symbol": "NATURALGAS22MAY26260CE",
        "token": "561552",
        "lot_size": 1250,
        "exchange": "MCX",
    }
    pos_a = _seed_short_position(mod, strategy, label="NG_ATM_CE_255")
    pos_b = mod.Ema20Position(
        option_label="NG_ATM_CE_260",
        broker_symbol="NATURALGAS22MAY26260CE",
        exchange="MCX",
        symbol_token="561552",
        qty=1,
        requested_lots=1,
        lot_size=1250,
        broker_qty=1250,
        entry_price=12.0,
        sl_price=14.4,
        tp_price=9.6,
        best_price=12.0,
        trail_active=False,
        entry_time=datetime(2026, 5, 7, 13, 10, tzinfo=timezone.utc),
    )
    strategy._managed_positions["NG_ATM_CE_260"] = pos_b
    calls: List[dict] = []
    _patch_bridge_success(monkeypatch, mod, calls)

    strategy._exit_position(reason="SL", position=pos_a)
    assert len(calls) == 1
    # 260 label has no guard yet, so its exit must fire.
    strategy._exit_position(reason="SL", position=pos_b)
    assert len(calls) == 2


def test_successful_partial_exit_clears_guard_so_residual_can_exit(monkeypatch):
    """A TP1 partial-exit success keeps the position alive with reduced qty.
    The next exit (e.g. TP or SL on the residual) MUST be allowed to fire.
    Without clearing the guard at the end of the partial branch, a partial
    would deadlock further exits on the same label for 60 s."""
    mod, strategy = _make_strategy(monkeypatch)
    pos = _seed_short_position(mod, strategy)
    # Pretend the position has 4 lots so a 50% partial leaves 2 residual.
    pos.qty = 4
    pos.original_qty = 4
    calls: List[dict] = []
    _patch_bridge_success(monkeypatch, mod, calls)

    # Path A: explicit partial of 2 lots (TP1) succeeds.
    strategy._exit_position(reason="TP1", partial_lots=2)
    assert len(calls) == 1
    assert pos.qty == 2, "partial must leave residual qty"
    assert pos.tp1_filled is True
    # Guard must have been cleared at the partial-exit branch — the next
    # full exit on the residual should fire.
    assert "NG_ATM_CE_255" not in strategy._pending_exit_by_label

    # Path B: full exit on residual.
    strategy._exit_position(reason="TP")
    assert len(calls) == 2, (
        "full exit on residual must fire after a successful partial"
    )


def test_global_exit_in_flight_flag_does_not_replace_per_label_guard(monkeypatch):
    """Sanity: the per-label guard's behaviour is independent of the
    global _exit_in_flight flag — it keys off
    self._pending_exit_by_label[label], not the global flag."""
    mod, strategy = _make_strategy(monkeypatch)
    _seed_short_position(mod, strategy)
    calls: List[dict] = []
    _patch_bridge_success(monkeypatch, mod, calls)

    strategy._exit_position(reason="SL")
    assert len(calls) == 1
    # _exit_in_flight is reset in finally: — it should be False right now.
    assert strategy._exit_in_flight is False
    # But the per-label guard should still be set.
    assert "NG_ATM_CE_255" in strategy._pending_exit_by_label


def test_replay_flag_bypasses_pending_exit_guard(monkeypatch):
    """Replay-mode regression: ``_pending_exit_by_label`` uses
    ``time.monotonic()`` which does not advance through replayed bars in a
    backtest. Without a replay bypass, the first exit on a label arms the
    guard with the current wall-clock and every subsequent exit attempt on
    the SAME label within the 60-second window is suppressed — producing
    <1 closed trade per multi-day replay across all parameter combos.

    The fix mirrors the existing entry-cooloff bypass (issue #232 / PR
    #232 line 1656): detect replay context via
    ``app.orders.replay_context.get_replay_flag`` and skip the wall-clock
    guard CHECK there. The guard is still ARMED on every bridge
    submission, so the PHX#199 LIVE protection is preserved when the
    replay flag is off.
    """
    from app.orders.replay_context import isolated_replay_flag

    mod, strategy = _make_strategy(monkeypatch)
    _seed_short_position(mod, strategy)
    calls: List[dict] = []
    _patch_bridge_success(monkeypatch, mod, calls)

    with isolated_replay_flag(True):
        # First exit fires and arms the guard.
        strategy._exit_position(reason="SL")
        assert len(calls) == 1
        assert "NG_ATM_CE_255" in strategy._pending_exit_by_label, (
            "guard must still be ARMED after success in replay — only the "
            "CHECK is bypassed; the guard itself is unchanged so LIVE "
            "PHX#199 protection is identical when replay flag is off"
        )

        # Re-seed the same label so a SECOND exit can be attempted; in
        # replay, multiple session boundaries fire force_exit_all on the
        # same label across days within microseconds of real wall-clock.
        _seed_short_position(mod, strategy)

        # Second exit on the same label must fire in replay even though
        # ``_pending_exit_by_label`` still has a recent wall-clock entry.
        strategy._exit_position(reason="SL")
        assert len(calls) == 2, (
            "Second same-label exit must fire in replay; the wall-clock "
            "guard is bypassed via get_replay_flag(). Pre-fix this would "
            "be suppressed with pending_age<60s, producing <1 trade per "
            "multi-day replay across all parameter sweeps."
        )

    # Sanity: with the replay flag CLEARED, the guard suppresses the
    # second exit (preserves PHX#199 LIVE protection).
    calls.clear()
    mod_b, strategy_b = _make_strategy(monkeypatch)
    _seed_short_position(mod_b, strategy_b)
    _patch_bridge_success(monkeypatch, mod_b, calls)
    strategy_b._exit_position(reason="SL")
    assert len(calls) == 1
    _seed_short_position(mod_b, strategy_b)
    strategy_b._exit_position(reason="SL")
    assert len(calls) == 1, (
        "Without replay flag, the per-label guard MUST suppress the "
        "duplicate exit attempt within the 60s wall-clock window — "
        "preserves PHX#199 protection."
    )
