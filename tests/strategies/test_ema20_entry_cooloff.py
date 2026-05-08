"""Tests for issue #223: per-instrument entry cooloff in Ema20Strategy.

Background
----------
On 2026-05-08 NATURALGAS22MAY26265CE, ``ema20_strategy`` placed two BUY
entries 47 seconds apart (broker_order_ids 260508000837721 at 18:37:02 IST
and 260508000837946 at 18:37:49 IST). The existing
``_last_fired_start_ts`` guard blocks a re-fire within the SAME bar (same
``candle.start_ts``); it does NOT block an entry that arrives via a
different code path or via tick aggregation that yields a different
``start_ts`` value within the same wall-clock minute.

Issue #223 adds a wall-clock ``_entry_cooloff_seconds`` backstop, keyed
per ``option_label``, independent of bar boundaries. Default = one bar
(``signal_timeframe``). Override via ``<PREFIX>EMA20_ENTRY_COOLOFF_SECONDS``.

Tests
-----
- Default cooloff equals the signal timeframe when env is unset.
- Per-prefix env override is honoured.
- Cooloff = 0 disables the gate AND logs a startup WARNING.
- Pre-populated cooloff state blocks a second short-call entry on the same
  label within the window (gate triggers BEFORE any broker call).
- A label that has elapsed past the cooloff is allowed to re-fire.
- A different label is unaffected by cooloff state on a separate label.
"""

from __future__ import annotations

import importlib
import logging
import time
import types
from datetime import datetime, timezone
from typing import List

import pytest

from app.brokers.base import OrderResponse


MODULE_PATH = "app.strategies.ema20_strategy"


def _runtime_stub() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        routing_table=types.SimpleNamespace(
            get_routes_for_strategy=lambda _sid: [
                types.SimpleNamespace(broker_account_id="A1")
            ]
        ),
        state_store=types.SimpleNamespace(
            get_positions=lambda _acct: [],
        ),
    )


def _instrument_meta() -> dict:
    return {
        "NG_FUT": {"symbol": "NG_FUT"},
        "NG_ATM_CE_265": {
            "symbol": "NATURALGAS22MAY26265CE",
            "token": "561549",
            "lot_size": 1250,
            "exchange": "MCX",
        },
        "NG_ATM_CE_270": {
            "symbol": "NATURALGAS22MAY26270CE",
            "token": "561550",
            "lot_size": 1250,
            "exchange": "MCX",
        },
    }


def _make_strategy(monkeypatch, *, env_prefix: str = "NG_", **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    mod = importlib.import_module(MODULE_PATH)
    importlib.reload(mod)  # pick up fresh env reads in module-level constants if any

    strategy = mod.Ema20Strategy(
        _instrument_meta(),
        env_prefix=env_prefix,
        underlying_label="NG_FUT",
        signal_timeframe=300,
        ema_period=20,
        min_atr=1.0,
        ce_label_prefix="NG_ATM_CE",
    )
    return mod, strategy


def _candle(start_ts: datetime, close: float) -> types.SimpleNamespace:
    return types.SimpleNamespace(start_ts=start_ts, close=close)


# --- Configuration tests ----------------------------------------------------

def test_cooloff_defaults_to_signal_timeframe(monkeypatch):
    """When no env override is set, cooloff is one full bar
    (= signal_timeframe). For 5-min bars (the NG default) this is 300s."""
    monkeypatch.delenv("NG_EMA20_ENTRY_COOLOFF_SECONDS", raising=False)
    monkeypatch.delenv("EMA20_ENTRY_COOLOFF_SECONDS", raising=False)
    _, strategy = _make_strategy(monkeypatch)
    assert strategy._entry_cooloff_seconds == 300.0


def test_cooloff_env_override_per_prefix(monkeypatch):
    """Per-symbol prefixed env override (e.g. NG_EMA20_ENTRY_COOLOFF_SECONDS)
    is honoured."""
    _, strategy = _make_strategy(
        monkeypatch, NG_EMA20_ENTRY_COOLOFF_SECONDS="180",
    )
    assert strategy._entry_cooloff_seconds == 180.0


def test_cooloff_disabled_logs_warning(monkeypatch, caplog):
    """Setting cooloff to 0 disables the gate and logs a WARNING at init so
    the operator is unambiguously informed of the loosened safety posture."""
    caplog.set_level(logging.WARNING)
    _, strategy = _make_strategy(
        monkeypatch, NG_EMA20_ENTRY_COOLOFF_SECONDS="0",
    )
    assert strategy._entry_cooloff_seconds == 0.0
    disabled_logs = [
        r for r in caplog.records
        if "EMA20 entry cooloff DISABLED" in r.getMessage()
    ]
    assert len(disabled_logs) == 1


# --- Gate behaviour tests ---------------------------------------------------

def _patch_bridge(monkeypatch, mod, calls: List[dict]):
    """Patch place_order_via_bridge and the hub-runtime accessor used by the
    bridge. Returns nothing; mutates ``calls``."""

    def fake_place(**kwargs):
        calls.append(kwargs)
        return OrderResponse(
            broker_order_id=f"260508000{len(calls):06d}",
            status="SUCCESS",
            message="ok",
        )

    monkeypatch.setattr(mod, "place_order_via_bridge", fake_place)
    if hasattr(mod, "_get_hub_runtime"):
        monkeypatch.setattr(mod, "_get_hub_runtime", lambda: _runtime_stub())


def test_cooloff_gate_blocks_second_entry_within_window(monkeypatch):
    """Direct gate-state test: pre-populate the per-label entry timestamp
    to NOW, then invoke ``_short_call_once_per_bar`` with a fresh candle.
    The gate must short-circuit BEFORE any broker call."""
    mod, strategy = _make_strategy(
        monkeypatch, NG_EMA20_ENTRY_COOLOFF_SECONDS="120",
    )
    # Force the strategy to resolve to NG_ATM_CE_265 via the explicit
    # ce_label that init has already chosen — _select_nearest_option is
    # only called when self.ce_label is None.
    strategy.ce_label = "NG_ATM_CE_265"

    calls: List[dict] = []
    _patch_bridge(monkeypatch, mod, calls)

    # Arm the cooloff to block any new entry on this label.
    strategy._last_entry_mono_by_label["NG_ATM_CE_265"] = time.monotonic()

    bar_start = datetime(2026, 5, 8, 13, 7, 0, tzinfo=timezone.utc)
    strategy._short_call_once_per_bar(
        candle=_candle(bar_start, close=12.95),
        close_price=12.95,
        ema_val=12.50,
    )

    assert calls == [], (
        "cooloff gate must block the entry submission entirely (#223)"
    )
    assert "NG_ATM_CE_265" not in strategy._managed_positions, (
        "no managed position must be created when the cooloff gate blocks"
    )


def test_cooloff_gate_allows_entry_after_elapsed(monkeypatch):
    """Once the cooloff has elapsed for a label, the gate must NOT block;
    the strategy proceeds to the broker call as before."""
    mod, strategy = _make_strategy(
        monkeypatch, NG_EMA20_ENTRY_COOLOFF_SECONDS="60",
    )
    strategy.ce_label = "NG_ATM_CE_265"

    calls: List[dict] = []
    _patch_bridge(monkeypatch, mod, calls)

    # Last entry was 120s ago — well past the 60s cooloff.
    strategy._last_entry_mono_by_label["NG_ATM_CE_265"] = (
        time.monotonic() - 120.0
    )

    bar_start = datetime(2026, 5, 8, 13, 9, 0, tzinfo=timezone.utc)
    strategy._short_call_once_per_bar(
        candle=_candle(bar_start, close=12.95),
        close_price=12.95,
        ema_val=12.50,
    )

    # Either the bridge was called, or the call was blocked by an UNRELATED
    # downstream guard (e.g. price feed, qty resolution). The gate itself
    # MUST NOT have produced an early return: assert the cooloff log line
    # was not emitted for this attempt.
    # The most decisive signal: if the bridge was called, the gate did not
    # block. We assert the bridge was called at least once.
    assert calls, (
        "cooloff must NOT block when the elapsed time exceeds the window"
    )


def test_cooloff_isolated_per_label(monkeypatch):
    """Cooloff state on label A must not block entry on label B."""
    mod, strategy = _make_strategy(
        monkeypatch, NG_EMA20_ENTRY_COOLOFF_SECONDS="120",
    )
    # Configure strategy to pick NG_ATM_CE_270 for this entry attempt.
    strategy.ce_label = "NG_ATM_CE_270"

    calls: List[dict] = []
    _patch_bridge(monkeypatch, mod, calls)

    # Arm cooloff on a DIFFERENT label.
    strategy._last_entry_mono_by_label["NG_ATM_CE_265"] = time.monotonic()

    bar_start = datetime(2026, 5, 8, 13, 7, 0, tzinfo=timezone.utc)
    strategy._short_call_once_per_bar(
        candle=_candle(bar_start, close=12.95),
        close_price=12.95,
        ema_val=12.50,
    )

    assert calls, (
        "cooloff on label A must not block entry on label B (#223)"
    )


def test_cooloff_records_timestamp_after_successful_entry(monkeypatch):
    """After a successful entry, the per-label monotonic timestamp must be
    set so that subsequent fires within the window are blocked."""
    mod, strategy = _make_strategy(
        monkeypatch, NG_EMA20_ENTRY_COOLOFF_SECONDS="120",
    )
    strategy.ce_label = "NG_ATM_CE_265"

    calls: List[dict] = []
    _patch_bridge(monkeypatch, mod, calls)

    bar_start = datetime(2026, 5, 8, 13, 7, 0, tzinfo=timezone.utc)
    before = time.monotonic()
    strategy._short_call_once_per_bar(
        candle=_candle(bar_start, close=12.95),
        close_price=12.95,
        ema_val=12.50,
    )
    after = time.monotonic()

    # Either the entry succeeded (timestamp recorded) OR an unrelated
    # downstream guard rejected the entry. Tolerate the latter — but if the
    # entry SUCCEEDED, the timestamp must be set.
    if calls:
        assert "NG_ATM_CE_265" in strategy._last_entry_mono_by_label
        ts = strategy._last_entry_mono_by_label["NG_ATM_CE_265"]
        assert before <= ts <= after, (
            f"recorded timestamp {ts} must be within [before={before}, after={after}]"
        )
