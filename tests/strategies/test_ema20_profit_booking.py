"""Tests for EMA20 profit-booking enhancements (PHX#182, #183, #184, #186)."""

from __future__ import annotations

import importlib
import json
import types
from datetime import datetime, timezone

import pytest

from app.brokers.base import OrderResponse

MODULE_PATH = "app.strategies.ema20_strategy"


def _make_strategy(monkeypatch, **kwargs):
    monkeypatch.setenv("NG_EMA20_REQUIRE_RSI_FALLING", "0")
    mod = importlib.import_module(MODULE_PATH)
    instrument_meta = {
        "NG_FUT": {"symbol": "NG_FUT"},
        "NG_ATM_CE_100": {
            "symbol": "NATURALGAS20FEB26100CE",
            "token": "123",
            "lot_size": 1,
            "exchange": "MCX",
        },
    }
    return mod, mod.Ema20Strategy(
        instrument_meta,
        env_prefix="NG_",
        underlying_label="NG_FUT",
        signal_timeframe=300,
        ema_period=20,
        require_rsi_falling=False,
        **kwargs,
    )


def _patch_bridge(monkeypatch, mod):
    calls = []

    def fake_place_order_via_bridge(**kwargs):
        calls.append(kwargs)
        return OrderResponse(
            broker_order_id=f"order-{len(calls)}",
            status="SUCCESS",
            message="ok",
        )

    monkeypatch.setattr(mod, "place_order_via_bridge", fake_place_order_via_bridge)
    return calls


def _make_open_position(mod, *, qty: int = 4, entry_price: float = 100.0):
    """Build a tracked Ema20Position with the new profit-booking fields populated."""
    return mod.Ema20Position(
        option_label="NG_ATM_CE_100",
        broker_symbol="NATURALGAS20FEB26100CE",
        exchange="MCX",
        symbol_token="123",
        qty=qty,
        requested_lots=qty,
        lot_size=1,
        broker_qty=qty,
        entry_price=entry_price,
        sl_price=entry_price * 1.30,
        tp_price=entry_price * 0.70,
        best_price=entry_price,
        trail_active=False,
        entry_time=datetime.now(timezone.utc),
        original_qty=qty,
    )


# ── PHX#182: TP1 partial booking ─────────────────────────────────────────────


def test_tp1_partial_book_reduces_qty_and_keeps_position(monkeypatch):
    mod, strategy = _make_strategy(monkeypatch, tp1_pct=0.20, tp1_qty_pct=0.5)
    calls = _patch_bridge(monkeypatch, mod)

    pos = _make_open_position(mod, qty=4, entry_price=100.0)
    strategy.position = pos
    strategy._managed_positions[pos.option_label] = pos

    # ltp=80 → -20% from entry, hits tp1_price (100 * 0.8 = 80).
    strategy.on_tick("NG_ATM_CE_100", 80.0)

    assert len(calls) == 1
    order_req = calls[0]["order_req"]
    assert order_req.tag == "EMA20_EXIT_TP1"
    # 50% of 4 = 2 lots booked.
    assert order_req.quantity == 2
    # Position kept open with residual 2 lots and tp1_filled flag set.
    assert strategy.position is not None
    assert strategy.position.qty == 2
    assert strategy.position.tp1_filled is True


def test_tp1_only_fires_once_then_falls_through_to_tp(monkeypatch):
    mod, strategy = _make_strategy(monkeypatch, tp1_pct=0.20, tp1_qty_pct=0.5)
    calls = _patch_bridge(monkeypatch, mod)

    pos = _make_open_position(mod, qty=4, entry_price=100.0)
    strategy.position = pos
    strategy._managed_positions[pos.option_label] = pos

    # First tick: hit TP1 → partial book.
    strategy.on_tick("NG_ATM_CE_100", 80.0)
    # Second tick at same price — TP1 already filled, must NOT re-fire.
    strategy.on_tick("NG_ATM_CE_100", 80.0)
    assert len(calls) == 1

    # Third tick: drops to TP target (70). Should fire full TP on residual qty=2.
    strategy.on_tick("NG_ATM_CE_100", 70.0)
    assert len(calls) == 2
    assert calls[1]["order_req"].tag == "EMA20_EXIT_TP"
    assert calls[1]["order_req"].quantity == 2
    assert strategy.position is None


def test_tp1_with_qty_rounding_to_full_position_falls_back_to_tp(monkeypatch):
    # tp1_qty_pct=1.0 → entire position; we should NOT fire TP1 (must leave one lot).
    # With qty=2 and pct=1.0, target is 2 lots, capped to qty-1=1; so partial of 1 fires.
    # With qty=1 and pct=anything, no partial possible → falls through to TP.
    mod, strategy = _make_strategy(monkeypatch, tp1_pct=0.20, tp1_qty_pct=1.0)
    calls = _patch_bridge(monkeypatch, mod)

    pos = _make_open_position(mod, qty=1, entry_price=100.0)
    strategy.position = pos
    strategy._managed_positions[pos.option_label] = pos

    # Hits tp1_price (80) but qty=1 → no partial possible. Should NOT fire TP1.
    # ltp=80 isn't past tp_price (70), so no exit at all on this tick.
    strategy.on_tick("NG_ATM_CE_100", 80.0)
    assert calls == []
    assert strategy.position is not None
    assert strategy.position.tp1_filled is False


def test_tp1_disabled_by_default(monkeypatch):
    mod, strategy = _make_strategy(monkeypatch)  # no tp1 params → disabled
    calls = _patch_bridge(monkeypatch, mod)

    pos = _make_open_position(mod, qty=4, entry_price=100.0)
    strategy.position = pos
    strategy._managed_positions[pos.option_label] = pos

    # Tick at -20%, no TP1 should fire.
    strategy.on_tick("NG_ATM_CE_100", 80.0)
    assert calls == []
    assert strategy.position is not None
    assert strategy.position.tp1_filled is False


# ── PHX#183: Give-back guardrail ─────────────────────────────────────────────


def test_giveback_arms_at_threshold_and_fires_on_retrace(monkeypatch):
    mod, strategy = _make_strategy(
        monkeypatch,
        giveback_pct=0.5,       # exit when profit retraces 50% from peak
        giveback_arm_pct=0.10,  # arm at 10% favourable move
    )
    calls = _patch_bridge(monkeypatch, mod)

    pos = _make_open_position(mod, qty=2, entry_price=100.0)
    strategy.position = pos
    strategy._managed_positions[pos.option_label] = pos

    # Move to 80 → peak = 20% profit, give-back armed. No exit yet.
    strategy.on_tick("NG_ATM_CE_100", 80.0)
    assert calls == []
    assert strategy.position is not None
    assert strategy.position.giveback_armed is True
    assert strategy.position.max_favorable_pct == pytest.approx(0.20)

    # Retrace to 92 → current profit = 8%, peak 20%, floor = 20% * 0.5 = 10%.
    # 8% < 10% → exit.
    strategy.on_tick("NG_ATM_CE_100", 92.0)
    assert len(calls) == 1
    assert calls[0]["order_req"].tag == "EMA20_EXIT_GIVEBACK"
    assert strategy.position is None


def test_giveback_does_not_arm_below_threshold(monkeypatch):
    mod, strategy = _make_strategy(
        monkeypatch,
        giveback_pct=0.5,
        giveback_arm_pct=0.20,  # require 20% favourable move to arm
    )
    calls = _patch_bridge(monkeypatch, mod)

    pos = _make_open_position(mod, qty=2, entry_price=100.0)
    strategy.position = pos
    strategy._managed_positions[pos.option_label] = pos

    # Best move only 5% (price 95). Below arm threshold.
    strategy.on_tick("NG_ATM_CE_100", 95.0)
    assert calls == []
    assert strategy.position is not None
    assert strategy.position.giveback_armed is False


def test_giveback_disabled_by_default(monkeypatch):
    mod, strategy = _make_strategy(monkeypatch)
    calls = _patch_bridge(monkeypatch, mod)

    pos = _make_open_position(mod, qty=2, entry_price=100.0)
    strategy.position = pos
    strategy._managed_positions[pos.option_label] = pos

    strategy.on_tick("NG_ATM_CE_100", 80.0)
    strategy.on_tick("NG_ATM_CE_100", 92.0)
    assert calls == []  # no give-back exit
    assert strategy.position is not None


# ── PHX#184: Time-decay accelerator ──────────────────────────────────────────


def test_decay_window_tightens_tp_price_once(monkeypatch):
    mod, strategy = _make_strategy(
        monkeypatch,
        square_off_time="15:00",
        decay_tighten_minutes_before_eod=60,  # 60 minutes before square-off
        decay_tp_multiplier=0.5,              # halve tp_pct
        decay_trail_buffer_multiplier=1.0,
    )
    calls = _patch_bridge(monkeypatch, mod)

    pos = _make_open_position(mod, qty=2, entry_price=100.0)
    # Seed strategy_context with the decay params (entry path normally does this).
    pos.strategy_context = {
        "decay_tighten_minutes_before_eod": 60,
        "decay_tp_multiplier": 0.5,
        "decay_trail_buffer_multiplier": 1.0,
    }
    strategy.position = pos
    strategy._managed_positions[pos.option_label] = pos
    original_tp_price = pos.tp_price  # 70 (entry 100, tp_pct 30)

    # Force time to 14:30 IST → 30 minutes to square-off, inside the 60-min window.
    strategy._now_ist = lambda: datetime(2025, 1, 1, 14, 30, tzinfo=mod.IST)

    # Tick at 95 — no exit, but should tighten tp_price (50% of 30% = 15% target).
    strategy.on_tick("NG_ATM_CE_100", 95.0)
    assert calls == []
    assert strategy.position is not None
    assert strategy.position.decay_window_applied is True
    # New tp_pct=0.15 → tp_price = 100 * 0.85 = 85, which is tighter (higher) than 70.
    assert strategy.position.tp_price == pytest.approx(85.0)
    assert strategy.position.tp_price > original_tp_price

    # Second tick should NOT recompute (decay_window_applied is True).
    strategy.on_tick("NG_ATM_CE_100", 90.0)
    assert strategy.position.tp_price == pytest.approx(85.0)


def test_decay_window_does_not_loosen_tp_when_multiplier_above_one(monkeypatch):
    mod, strategy = _make_strategy(
        monkeypatch,
        square_off_time="15:00",
        decay_tighten_minutes_before_eod=60,
        decay_tp_multiplier=2.0,  # would loosen — must be ignored
        decay_trail_buffer_multiplier=1.0,
    )
    _patch_bridge(monkeypatch, mod)
    pos = _make_open_position(mod, qty=2, entry_price=100.0)
    pos.strategy_context = {
        "decay_tighten_minutes_before_eod": 60,
        "decay_tp_multiplier": 2.0,
        "decay_trail_buffer_multiplier": 1.0,
    }
    strategy.position = pos
    strategy._managed_positions[pos.option_label] = pos
    original_tp = pos.tp_price

    strategy._now_ist = lambda: datetime(2025, 1, 1, 14, 30, tzinfo=mod.IST)
    strategy.on_tick("NG_ATM_CE_100", 95.0)

    # tp_price must NOT have been loosened (moved away from entry).
    assert strategy.position.tp_price == pytest.approx(original_tp)


def test_decay_disabled_when_minutes_is_none(monkeypatch):
    mod, strategy = _make_strategy(
        monkeypatch,
        square_off_time="15:00",
        # No decay_tighten_minutes_before_eod → disabled.
    )
    _patch_bridge(monkeypatch, mod)
    pos = _make_open_position(mod, qty=2, entry_price=100.0)
    strategy.position = pos
    strategy._managed_positions[pos.option_label] = pos
    original_tp = pos.tp_price

    strategy._now_ist = lambda: datetime(2025, 1, 1, 14, 30, tzinfo=mod.IST)
    strategy.on_tick("NG_ATM_CE_100", 95.0)

    assert strategy.position.tp_price == pytest.approx(original_tp)
    assert strategy.position.decay_window_applied is False


# ── PHX#186: Exit attribution telemetry ──────────────────────────────────────


def test_exit_attribution_emitted_on_tp(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_LOG_DIR", str(tmp_path))
    mod, strategy = _make_strategy(monkeypatch)
    _patch_bridge(monkeypatch, mod)

    pos = _make_open_position(mod, qty=2, entry_price=100.0)
    strategy.position = pos
    strategy._managed_positions[pos.option_label] = pos

    # Move to 70 — TP fires.
    strategy.on_tick("NG_ATM_CE_100", 70.0)
    assert strategy.position is None

    # Attribution JSONL file should exist with one record.
    attribution_file = tmp_path / "exit_attribution.jsonl"
    assert attribution_file.exists()
    lines = attribution_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["v"] == 1
    assert record["schema"] == "ema20.exit_attribution"
    assert record["reason"] == "TP"
    assert record["final"] is True
    assert record["entry_price"] == pytest.approx(100.0)
    assert record["exit_price"] == pytest.approx(70.0)
    assert record["final_profit_pct"] == pytest.approx(0.30)
    assert record["exit_lots"] == 2
    assert record["original_qty"] == 2
    assert record["tp1_filled"] is False


def test_exit_attribution_for_partial_tp1_marks_final_false(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_LOG_DIR", str(tmp_path))
    mod, strategy = _make_strategy(monkeypatch, tp1_pct=0.20, tp1_qty_pct=0.5)
    _patch_bridge(monkeypatch, mod)

    pos = _make_open_position(mod, qty=4, entry_price=100.0)
    strategy.position = pos
    strategy._managed_positions[pos.option_label] = pos

    strategy.on_tick("NG_ATM_CE_100", 80.0)  # TP1 partial

    attribution_file = tmp_path / "exit_attribution.jsonl"
    lines = attribution_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["reason"] == "TP1"
    assert record["final"] is False
    assert record["exit_lots"] == 2
    assert record["remaining_qty"] == 2
    assert record["tp1_filled"] is True


def test_exit_attribution_records_peak_favorable_pct(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_LOG_DIR", str(tmp_path))
    mod, strategy = _make_strategy(monkeypatch)
    _patch_bridge(monkeypatch, mod)

    pos = _make_open_position(mod, qty=2, entry_price=100.0)
    strategy.position = pos
    strategy._managed_positions[pos.option_label] = pos

    # Touch 75 (peak 25% favourable), then drop to 70 (TP).
    strategy.on_tick("NG_ATM_CE_100", 75.0)
    strategy.on_tick("NG_ATM_CE_100", 70.0)

    attribution_file = tmp_path / "exit_attribution.jsonl"
    lines = attribution_file.read_text(encoding="utf-8").strip().splitlines()
    record = json.loads(lines[-1])
    assert record["peak_favorable_pct"] == pytest.approx(0.30)  # best=70 → (100-70)/100
