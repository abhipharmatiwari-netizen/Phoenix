from datetime import datetime, timezone

from app.core.indicators_engine import MultiInstrumentIndicatorEngine


def test_session_rollover_resets_state_and_skips_callback():
    engine = MultiInstrumentIndicatorEngine(timeframes=[300], max_candles=100)
    callbacks = []
    engine.on_bar = lambda label, tf, candle, indicators: callbacks.append(
        (label, tf, candle.start_ts, candle.end_ts)
    )

    # Build one pre-close candle, then jump across session date boundary.
    engine.update_from_tick(
        "NG_FUT",
        300.0,
        timestamp=datetime(2026, 2, 10, 17, 57, tzinfo=timezone.utc),
    )
    engine.update_from_tick(
        "NG_FUT",
        301.0,
        timestamp=datetime(2026, 2, 11, 3, 30, tzinfo=timezone.utc),
    )

    tf_state = engine.states["NG_FUT"].timeframes[300]
    assert callbacks == []
    assert len(tf_state.candles) == 0
    assert tf_state.current_candle is not None
    assert tf_state.current_candle.start_ts == datetime(
        2026, 2, 11, 3, 30, tzinfo=timezone.utc
    )
    assert tf_state.atr is None
    assert tf_state.rsi is None
    assert tf_state.macd is None
    assert tf_state.adx is None
    assert tf_state.plus_di is None
    assert tf_state.minus_di is None


def test_large_gap_same_day_resets_state_and_skips_callback():
    engine = MultiInstrumentIndicatorEngine(timeframes=[300], max_candles=100)
    callbacks = []
    engine.on_bar = lambda label, tf, candle, indicators: callbacks.append(
        (label, tf, candle.start_ts, candle.end_ts)
    )

    # 30-minute gap on same day is > 3 * 5-minute timeframe => stale reset.
    engine.update_from_tick(
        "NG_FUT",
        300.0,
        timestamp=datetime(2026, 2, 11, 4, 0, tzinfo=timezone.utc),
    )
    engine.update_from_tick(
        "NG_FUT",
        301.0,
        timestamp=datetime(2026, 2, 11, 4, 30, tzinfo=timezone.utc),
    )

    tf_state = engine.states["NG_FUT"].timeframes[300]
    assert callbacks == []
    assert len(tf_state.candles) == 0
    assert tf_state.current_candle is not None
    assert tf_state.current_candle.start_ts == datetime(
        2026, 2, 11, 4, 30, tzinfo=timezone.utc
    )
    assert tf_state.adx is None
    assert tf_state.plus_di is None
    assert tf_state.minus_di is None


def test_large_gap_same_day_uses_backfill_when_callback_covers_gap():
    engine = MultiInstrumentIndicatorEngine(timeframes=[300], max_candles=100)
    requests = []

    def _backfill(request):
        requests.append(request)
        return [
            {
                "ts_start": datetime(2026, 2, 11, 4, 5, tzinfo=timezone.utc),
                "ts_end": datetime(2026, 2, 11, 4, 10, tzinfo=timezone.utc),
                "label": "NG_FUT",
                "timeframe_seconds": 300,
                "o": 300.0,
                "h": 300.5,
                "l": 299.5,
                "c": 300.1,
            },
            {
                "ts_start": datetime(2026, 2, 11, 4, 10, tzinfo=timezone.utc),
                "ts_end": datetime(2026, 2, 11, 4, 15, tzinfo=timezone.utc),
                "label": "NG_FUT",
                "timeframe_seconds": 300,
                "o": 300.1,
                "h": 300.6,
                "l": 300.0,
                "c": 300.2,
            },
            {
                "ts_start": datetime(2026, 2, 11, 4, 15, tzinfo=timezone.utc),
                "ts_end": datetime(2026, 2, 11, 4, 20, tzinfo=timezone.utc),
                "label": "NG_FUT",
                "timeframe_seconds": 300,
                "o": 300.2,
                "h": 300.7,
                "l": 300.1,
                "c": 300.3,
            },
            {
                "ts_start": datetime(2026, 2, 11, 4, 20, tzinfo=timezone.utc),
                "ts_end": datetime(2026, 2, 11, 4, 25, tzinfo=timezone.utc),
                "label": "NG_FUT",
                "timeframe_seconds": 300,
                "o": 300.3,
                "h": 300.8,
                "l": 300.2,
                "c": 300.4,
            },
            {
                "ts_start": datetime(2026, 2, 11, 4, 25, tzinfo=timezone.utc),
                "ts_end": datetime(2026, 2, 11, 4, 30, tzinfo=timezone.utc),
                "label": "NG_FUT",
                "timeframe_seconds": 300,
                "o": 300.4,
                "h": 300.9,
                "l": 300.3,
                "c": 300.5,
            },
        ]

    engine.on_gap_backfill = _backfill
    engine.update_from_tick(
        "NG_FUT",
        300.0,
        timestamp=datetime(2026, 2, 11, 4, 0, tzinfo=timezone.utc),
    )
    engine.update_from_tick(
        "NG_FUT",
        301.0,
        timestamp=datetime(2026, 2, 11, 4, 30, tzinfo=timezone.utc),
    )

    tf_state = engine.states["NG_FUT"].timeframes[300]
    assert len(requests) == 1
    assert requests[0].gap_start_ts == datetime(2026, 2, 11, 4, 5, tzinfo=timezone.utc)
    assert len(tf_state.candles) == 6
    assert tf_state.current_candle is not None
    assert tf_state.current_candle.start_ts == datetime(
        2026, 2, 11, 4, 30, tzinfo=timezone.utc
    )
