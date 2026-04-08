from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core.indicators_engine import MultiInstrumentIndicatorEngine
from app.runners import multi_instrument_stream as mis

LABEL = "NIFTY_IDX"
TIMEFRAME_SECONDS = 300


def _make_recorded_bars(count: int = 36) -> list[dict]:
    start = datetime(2026, 3, 4, 4, 0, tzinfo=timezone.utc)
    bars: list[dict] = []
    price = 220.0
    for index in range(count):
        ts_start = start + timedelta(seconds=TIMEFRAME_SECONDS * index)
        slope = 0.65 + (index % 3) * 0.05
        open_price = price
        high_price = open_price + 0.45 + (index % 2) * 0.1
        low_price = open_price - 0.75 - ((index + 1) % 2) * 0.05
        close_price = open_price - slope
        bars.append(
            {
                "label": LABEL,
                "timeframe_seconds": TIMEFRAME_SECONDS,
                "ts_start": ts_start,
                "ts_end": ts_start + timedelta(seconds=TIMEFRAME_SECONDS),
                "o": round(open_price, 6),
                "h": round(high_price, 6),
                "l": round(low_price, 6),
                "c": round(close_price, 6),
            }
        )
        price = close_price - 0.2
    return bars


def _recorded_tick_stream(bars: list[dict]) -> list[dict]:
    events: list[dict] = []
    for index, bar in enumerate(bars):
        ts_start = bar["ts_start"]
        for offset_seconds, price in (
            (5, bar["o"]),
            (90, bar["h"]),
            (180, bar["l"]),
            (295, bar["c"]),
        ):
            events.append(
                {
                    "bar_index": index,
                    "timestamp": ts_start + timedelta(seconds=offset_seconds),
                    "price": float(price),
                }
            )
    return events


def _decision_snapshot(candle, indicators: dict) -> dict:
    ema_20 = indicators.get("ema_20")
    atr = indicators.get("atr")
    adx = indicators.get("adx")
    plus_di = indicators.get("plus_di")
    minus_di = indicators.get("minus_di")
    should_enter = (
        ema_20 is not None
        and atr is not None
        and adx is not None
        and plus_di is not None
        and minus_di is not None
        and candle.c < ema_20
        and atr > 0.0
        and adx > 0.0
        and minus_di > plus_di
    )
    return {
        "start_ts": candle.start_ts,
        "decision": bool(should_enter),
        "ema_20": round(float(ema_20), 6) if ema_20 is not None else None,
        "atr": round(float(atr), 6) if atr is not None else None,
        "adx": round(float(adx), 6) if adx is not None else None,
    }


def _latest_indicator_snapshot(engine: MultiInstrumentIndicatorEngine) -> dict:
    latest = engine.get_latest_indicators(LABEL, TIMEFRAME_SECONDS)
    assert latest is not None
    return {
        "ema_20": round(float(latest["ema_20"]), 6)
        if latest.get("ema_20") is not None
        else None,
        "atr": round(float(latest["atr"]), 6) if latest.get("atr") is not None else None,
        "adx": round(float(latest["adx"]), 6) if latest.get("adx") is not None else None,
    }


def _run_recorded_tick_stream(
    bars: list[dict],
    *,
    skipped_bar_indexes: set[int] | None = None,
    backfill_bars: list[dict] | None = None,
) -> tuple[MultiInstrumentIndicatorEngine, list[dict]]:
    engine = MultiInstrumentIndicatorEngine(
        timeframes=[TIMEFRAME_SECONDS],
        atr_period=14,
        rsi_period=14,
        adx_period=14,
        ema_periods_by_timeframe={TIMEFRAME_SECONDS: [20]},
        max_candles=200,
    )
    decisions: list[dict] = []
    engine.on_bar = (
        lambda _label, _timeframe_seconds, candle, indicators: decisions.append(
            _decision_snapshot(candle, indicators)
        )
    )
    skipped = set(skipped_bar_indexes or set())
    if backfill_bars:
        engine.on_gap_backfill = (
            lambda request: [
                dict(bar)
                for bar in backfill_bars
                if request.gap_start_ts <= bar["ts_start"] < request.next_bucket_start_ts
            ]
        )

    for event in _recorded_tick_stream(bars):
        if int(event["bar_index"]) in skipped:
            continue
        engine.update_from_tick(
            LABEL,
            float(event["price"]),
            timestamp=event["timestamp"],
        )

    last_bar = bars[-1]
    engine.update_from_tick(
        LABEL,
        float(last_bar["c"]),
        timestamp=last_bar["ts_end"] + timedelta(seconds=5),
    )
    return engine, decisions


def test_recorded_tick_replay_with_backfill_matches_continuous_indicators():
    bars = _make_recorded_bars()
    skipped_bar_indexes = {18, 19, 20}
    baseline_engine, baseline_decisions = _run_recorded_tick_stream(bars)
    flap_engine, flap_decisions = _run_recorded_tick_stream(
        bars,
        skipped_bar_indexes=skipped_bar_indexes,
        backfill_bars=[bars[index] for index in sorted(skipped_bar_indexes)],
    )

    resume_ts = bars[max(skipped_bar_indexes) + 1]["ts_start"]
    baseline_by_ts = {
        row["start_ts"]: row for row in baseline_decisions if row["start_ts"] >= resume_ts
    }
    flap_by_ts = {
        row["start_ts"]: row for row in flap_decisions if row["start_ts"] >= resume_ts
    }

    assert flap_by_ts
    assert flap_by_ts == {
        start_ts: baseline_by_ts[start_ts] for start_ts in sorted(flap_by_ts.keys())
    }
    assert _latest_indicator_snapshot(flap_engine) == _latest_indicator_snapshot(
        baseline_engine
    )


def test_indicator_bar_replay_is_deterministic_for_decision_sequence():
    bars = _make_recorded_bars()

    def _capture() -> tuple[int, list[dict]]:
        decisions: list[dict] = []
        replayed = mis._replay_indicator_seed_bars(
            seed_bars=bars,
            timeframes=[TIMEFRAME_SECONDS],
            ema_periods_by_timeframe={TIMEFRAME_SECONDS: [20]},
            allowed_labels={LABEL},
            on_bar=lambda _label, _timeframe_seconds, candle, indicators: decisions.append(
                _decision_snapshot(candle, indicators)
            ),
            atr_period=14,
            rsi_period=14,
            adx_period=14,
            max_candles=200,
        )
        return replayed, decisions

    replayed_one, decisions_one = _capture()
    replayed_two, decisions_two = _capture()

    assert replayed_one == len(bars)
    assert replayed_two == len(bars)
    assert decisions_one == decisions_two
    assert decisions_one[-1]["ema_20"] is not None
    assert decisions_one[-1]["atr"] is not None
    assert decisions_one[-1]["adx"] is not None
    assert any(row["decision"] for row in decisions_one[20:])
