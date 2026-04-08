"""Sprint 5 RISK-5.3: Exposure limiter tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.risk.exposure_limiter import (
    ExposureLimits,
    ExposureSnapshot,
    check_exposure_limits,
    compute_exposure_snapshot,
)


class TestComputeExposureSnapshot:
    def test_empty_positions(self):
        snap = compute_exposure_snapshot([])
        assert snap.open_position_count == 0
        assert snap.per_underlying_notional == {}
        assert snap.short_options_legs == 0

    def test_counts_open_positions(self):
        positions = [
            SimpleNamespace(quantity=10, underlying="NIFTY", ltp=25000.0, product_type="INTRADAY", side="BUY", instrument_type=""),
            SimpleNamespace(quantity=-5, underlying="BANKNIFTY", ltp=50000.0, product_type="INTRADAY", side="SELL", instrument_type=""),
        ]
        snap = compute_exposure_snapshot(positions)
        assert snap.open_position_count == 2

    def test_aggregates_notional_by_underlying(self):
        positions = [
            SimpleNamespace(quantity=10, underlying="NIFTY", ltp=100.0, product_type="", side="BUY", instrument_type=""),
            SimpleNamespace(quantity=20, underlying="NIFTY", ltp=100.0, product_type="", side="BUY", instrument_type=""),
        ]
        snap = compute_exposure_snapshot(positions)
        assert snap.per_underlying_notional["NIFTY"] == 3000.0

    def test_detects_short_options(self):
        positions = [
            SimpleNamespace(quantity=-1, underlying="NIFTY", ltp=200.0, product_type="OPTIDX", side="SELL", instrument_type="OPTIDX"),
        ]
        snap = compute_exposure_snapshot(positions)
        assert snap.short_options_legs == 1

    def test_ignores_zero_quantity(self):
        positions = [
            SimpleNamespace(quantity=0, underlying="NIFTY", ltp=100.0, product_type="", side="BUY", instrument_type=""),
        ]
        snap = compute_exposure_snapshot(positions)
        assert snap.open_position_count == 0


class TestCheckExposureLimits:
    def test_allows_within_limits(self):
        snap = ExposureSnapshot(open_position_count=5, per_underlying_notional={"NIFTY": 100_000}, short_options_legs=2)
        limits = ExposureLimits(max_open_positions=20)
        ok, _, _ = check_exposure_limits(snap, limits)
        assert ok

    def test_blocks_max_positions(self):
        snap = ExposureSnapshot(open_position_count=20, per_underlying_notional={}, short_options_legs=0)
        limits = ExposureLimits(max_open_positions=20)
        ok, code, msg = check_exposure_limits(snap, limits)
        assert not ok
        assert code == "EXPOSURE_MAX_POSITIONS"

    def test_blocks_max_notional(self):
        snap = ExposureSnapshot(open_position_count=1, per_underlying_notional={"NIFTY": 4_500_000}, short_options_legs=0)
        limits = ExposureLimits(max_per_underlying_notional=5_000_000)
        ok, code, _ = check_exposure_limits(
            snap, limits,
            new_order_underlying="NIFTY",
            new_order_notional=600_000,
        )
        assert not ok
        assert code == "EXPOSURE_MAX_NOTIONAL"

    def test_blocks_max_short_options(self):
        snap = ExposureSnapshot(open_position_count=5, per_underlying_notional={}, short_options_legs=10)
        limits = ExposureLimits(max_short_options_legs=10)
        ok, code, _ = check_exposure_limits(snap, limits, is_new_short_option=True)
        assert not ok
        assert code == "EXPOSURE_MAX_SHORT_OPTIONS"

    def test_disabled_limits_allow_all(self):
        snap = ExposureSnapshot(open_position_count=100, per_underlying_notional={}, short_options_legs=50)
        limits = ExposureLimits(enabled=False)
        ok, _, _ = check_exposure_limits(snap, limits)
        assert ok
