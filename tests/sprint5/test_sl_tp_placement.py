"""Sprint 5 EXIT-5.2: SL/TP placement tests."""

from __future__ import annotations

import pytest

from app.orders.sl_tp_placement import (
    SLOrderType,
    SLTPConfig,
    compute_fallback_sl,
    compute_sl_tp,
    round_to_tick,
)


class TestRoundToTick:
    def test_exact_tick(self):
        assert round_to_tick(100.05, 0.05) == 100.05

    def test_rounds_up(self):
        assert round_to_tick(100.03, 0.05) == 100.05

    def test_rounds_down(self):
        assert round_to_tick(100.01, 0.05) == 100.00

    def test_zero_tick_size(self):
        assert round_to_tick(100.03, 0) == 100.03


class TestComputeSLTP:
    def test_buy_entry_sl_below_tp_above(self):
        params = compute_sl_tp(100.0, "BUY", sl_pct=1.0, tp_pct=0.5, tick_size=0.05)
        assert params.stoploss_trigger < params.entry_price
        assert params.target_price > params.entry_price
        assert params.stoploss_trigger == pytest.approx(99.0, abs=0.05)
        assert params.target_price == pytest.approx(100.5, abs=0.05)

    def test_sell_entry_sl_above_tp_below(self):
        params = compute_sl_tp(200.0, "SELL", sl_pct=1.0, tp_pct=0.5, tick_size=0.05)
        assert params.stoploss_trigger > params.entry_price
        assert params.target_price < params.entry_price

    def test_default_config(self):
        params = compute_sl_tp(500.0, "BUY")
        assert params.sl_order_type == SLOrderType.SLM
        assert params.stoploss_limit is None  # SL-M has no limit price

    def test_risk_reward_ratio(self):
        params = compute_sl_tp(100.0, "BUY", sl_pct=1.0, tp_pct=2.0, tick_size=0.05)
        assert params.risk_reward_ratio == pytest.approx(2.0, abs=0.1)


class TestFallbackSL:
    def test_slm_falls_back_to_sll(self):
        params = compute_sl_tp(
            100.0, "BUY", sl_pct=1.0,
            config=SLTPConfig(preferred_sl_type=SLOrderType.SLM),
        )
        assert params.sl_order_type == SLOrderType.SLM
        assert params.stoploss_limit is None

        fallback = compute_fallback_sl(params)
        assert fallback.sl_order_type == SLOrderType.SLL
        assert fallback.stoploss_limit is not None
        assert fallback.stoploss_limit < fallback.stoploss_trigger

    def test_sll_has_no_further_fallback(self):
        params = compute_sl_tp(
            100.0, "BUY", sl_pct=1.0,
            config=SLTPConfig(preferred_sl_type=SLOrderType.SLL, sl_limit_offset_pct=0.5),
        )
        fallback = compute_fallback_sl(params)
        # Already on SL-L, no change
        assert fallback.sl_order_type == SLOrderType.SLL
