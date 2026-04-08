from __future__ import annotations

from app.runners.stream_indicator_plan import (
    build_indicator_engine_plan,
    parse_timeframe_to_seconds,
)


def test_parse_timeframe_to_seconds_handles_common_formats():
    assert parse_timeframe_to_seconds(60) == 60
    assert parse_timeframe_to_seconds("5m") == 300
    assert parse_timeframe_to_seconds("30s") == 30
    assert parse_timeframe_to_seconds("1h") == 3600
    assert parse_timeframe_to_seconds("invalid") is None


def test_build_indicator_engine_plan_adds_multi_timeframe_ema_requirements(monkeypatch):
    monkeypatch.delenv("EMA_5M_PERIODS", raising=False)

    plan = build_indicator_engine_plan(
        {
            "strategies": [
                {
                    "name": "put_momentum_scalper",
                    "params": {
                        "timeframe_seconds_5m": 300,
                        "timeframe_seconds_15m": 900,
                    },
                }
            ]
        }
    )

    assert plan.engine_timeframes == [60, 300, 900]
    assert plan.ema_periods_by_timeframe[300] == [20, 50]
    assert plan.ema_periods_by_timeframe[900] == [20]
    assert plan.adx_period == 14


def test_build_indicator_engine_plan_uses_smallest_valid_adx_period():
    plan = build_indicator_engine_plan(
        {
            "strategies": [
                {
                    "name": "ema20_strategy",
                    "params": {
                        "timeframe_seconds": 300,
                        "adx_period": 18,
                    },
                },
                {
                    "name": "ema20_strategy",
                    "params": {
                        "timeframe_seconds": 900,
                        "adx_period": 10,
                    },
                },
            ]
        }
    )

    assert plan.adx_period == 10
    assert 300 in plan.engine_timeframes
    assert 900 in plan.engine_timeframes
