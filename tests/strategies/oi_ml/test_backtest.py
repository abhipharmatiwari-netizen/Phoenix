from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.data.option_chain_provider import OptionQuote
from app.strategies.oi_ml.backtest import (
    BearCallSpreadExitReason,
    BearCallSpreadLabelConfig,
    label_bear_call_spread_intraday,
)


DECISION_TS = datetime(2026, 5, 19, 4, 30, tzinfo=timezone.utc)  # 10:00 IST
EXPIRY = date(2026, 5, 21)


def _quote(
    minute: int,
    strike: int,
    *,
    bid: float,
    ask: float,
    oi: int = 1000,
    spot: float = 25100.0,
    vix: float = 16.0,
) -> OptionQuote:
    ts = DECISION_TS + timedelta(minutes=minute)
    return OptionQuote(
        snapshot_ts=ts,
        source_ts=ts - timedelta(seconds=5),
        ingested_at=ts + timedelta(seconds=1),
        underlying="NIFTY",
        expiry=EXPIRY,
        strike=strike,
        option_type="CE",
        trading_symbol=f"NIFTY21MAY26{strike}CE",
        exchange="NFO",
        symbol_token=strike,
        provider="unit",
        oi=oi,
        volume=1000,
        iv="12.0",
        bid=bid,
        ask=ask,
        ltp=(bid + ask) / 2,
        underlying_ltp=spot,
        vix=vix,
    )


def test_bear_call_spread_label_marks_actual_spread_take_profit():
    label = label_bear_call_spread_intraday(
        [
            _quote(0, 25200, bid=100.0, ask=101.0),
            _quote(0, 25400, bid=20.0, ask=21.0),
            _quote(5, 25200, bid=45.0, ask=46.0),
            _quote(5, 25400, bid=18.0, ask=19.0),
        ],
        decision_ts=DECISION_TS,
        short_strike=25200,
        long_strike=25400,
        config=BearCallSpreadLabelConfig(lot_size=65, fees_per_lot=0.0),
    )

    assert label.exit_reason == BearCallSpreadExitReason.TAKE_PROFIT
    assert label.primary_label == 1
    assert label.pnl_per_lot == pytest.approx((79.0 - 28.0) * 65)
    assert label.max_source_ts is not None
    assert label.max_ingested_at is not None


def test_bear_call_spread_label_covers_loss_spot_oi_vol_and_time_stops():
    base = [
        _quote(0, 25200, bid=100.0, ask=101.0),
        _quote(0, 25400, bid=20.0, ask=21.0),
    ]
    cases = [
        (
            [_quote(5, 25200, bid=160.0, ask=170.0), _quote(5, 25400, bid=18.0, ask=19.0)],
            BearCallSpreadExitReason.LOSS_STOP,
        ),
        (
            [_quote(5, 25200, bid=105.0, ask=106.0, spot=25210.0), _quote(5, 25400, bid=20.0, ask=21.0)],
            BearCallSpreadExitReason.SPOT_STOP,
        ),
        (
            [_quote(5, 25200, bid=105.0, ask=106.0, oi=500), _quote(5, 25400, bid=20.0, ask=21.0)],
            BearCallSpreadExitReason.OI_INVALIDATION,
        ),
        (
            [_quote(5, 25200, bid=105.0, ask=106.0, vix=24.0), _quote(5, 25400, bid=20.0, ask=21.0)],
            BearCallSpreadExitReason.VOL_STOP,
        ),
        (
            [_quote(295, 25200, bid=105.0, ask=106.0), _quote(295, 25400, bid=20.0, ask=21.0)],
            BearCallSpreadExitReason.TIME_STOP,
        ),
    ]

    for path, reason in cases:
        label = label_bear_call_spread_intraday(
            base + path,
            decision_ts=DECISION_TS,
            short_strike=25200,
            long_strike=25400,
            config=BearCallSpreadLabelConfig(
                lot_size=65,
                fees_per_lot=0.0,
                spot_stop_buffer_points=0.0,
                max_vix=22.0,
            ),
        )
        assert label.exit_reason == reason


def test_bear_call_spread_label_rejects_rows_after_eod_cap():
    with pytest.raises(ValueError, match="EOD cap"):
        label_bear_call_spread_intraday(
            [
                _quote(0, 25200, bid=100.0, ask=101.0),
                _quote(0, 25400, bid=20.0, ask=21.0),
                _quote(400, 25200, bid=90.0, ask=91.0),
                _quote(400, 25400, bid=19.0, ask=20.0),
            ],
            decision_ts=DECISION_TS,
            short_strike=25200,
            long_strike=25400,
        )
