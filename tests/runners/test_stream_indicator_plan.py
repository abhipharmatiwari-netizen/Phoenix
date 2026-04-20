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


# ---------------------------------------------------------------------------
# Issue #24: Postgres-first indicator seeding priority
# ---------------------------------------------------------------------------

def test_indicator_seed_priority_postgres_before_csv():
    """When INDICATOR_POSTGRES_ENABLED=true, Postgres is tried before CSV (priority logic)."""
    postgres_calls = []
    csv_calls = []

    def _fake_postgres(dsn, table, *, allowed_timeframes, allowed_labels, max_bars_per_series):
        postgres_calls.append(True)
        return [{"label": "NIFTY", "timeframe": 300, "close": 22000, "timestamp": "2026-04-20T09:00:00"}]

    def _fake_csv(path, *, allowed_labels, allowed_timeframes, max_bars_per_series):
        csv_calls.append(True)
        return []

    # Simulate the seeding block with postgres DSN set
    seed_csv_path = "indicator_bars.csv"
    indicator_postgres_dsn = "postgresql://user:pass@host/db"
    indicator_postgres_table = "indicator_bars"
    seed_bars = 10

    seeded = 0
    seeded_history_source = "none"
    seeded_history_bars = []
    allowed_seed_labels = {"NIFTY"}

    class _FakeEngine:
        timeframes = [300]
        def seed_from_closed_bars(self, bars):
            return len(bars)

    engine = _FakeEngine()

    if indicator_postgres_dsn:
        bars = _fake_postgres(indicator_postgres_dsn, indicator_postgres_table,
                              allowed_timeframes=set(engine.timeframes),
                              allowed_labels=allowed_seed_labels,
                              max_bars_per_series=seed_bars)
        seeded = engine.seed_from_closed_bars(bars)
        if seeded:
            seeded_history_source = "postgres"

    if seeded == 0 and seed_csv_path:
        bars = _fake_csv(seed_csv_path, allowed_labels=allowed_seed_labels,
                         allowed_timeframes=set(engine.timeframes),
                         max_bars_per_series=seed_bars)
        seeded = engine.seed_from_closed_bars(bars)
        if seeded:
            seeded_history_source = "csv"

    assert seeded == 1
    assert seeded_history_source == "postgres"
    assert len(postgres_calls) == 1
    # CSV was not called because Postgres succeeded
    assert len(csv_calls) == 0


def test_indicator_seed_falls_back_to_csv_when_postgres_returns_empty():
    """When Postgres returns 0 bars, CSV fallback is used (priority logic)."""
    def _fake_postgres_empty(dsn, table, **kwargs):
        return []

    def _fake_csv_nonempty(path, **kwargs):
        return [{"label": "NIFTY", "timeframe": 300, "close": 22000, "timestamp": "2026-04-20T09:00:00"}]

    seed_csv_path = "indicator_bars.csv"
    indicator_postgres_dsn = "postgresql://user:pass@host/db"
    indicator_postgres_table = "indicator_bars"
    seed_bars = 10
    seeded = 0
    seeded_history_source = "none"
    allowed_seed_labels = {"NIFTY"}

    class _FakeEngine:
        timeframes = [300]
        def seed_from_closed_bars(self, bars):
            return len(bars)

    engine = _FakeEngine()

    if indicator_postgres_dsn:
        bars = _fake_postgres_empty(indicator_postgres_dsn, indicator_postgres_table,
                                     allowed_timeframes=set(engine.timeframes),
                                     allowed_labels=allowed_seed_labels,
                                     max_bars_per_series=seed_bars)
        seeded = engine.seed_from_closed_bars(bars)
        if seeded:
            seeded_history_source = "postgres"

    if seeded == 0 and seed_csv_path:
        bars = _fake_csv_nonempty(seed_csv_path, allowed_labels=allowed_seed_labels,
                                   allowed_timeframes=set(engine.timeframes),
                                   max_bars_per_series=seed_bars)
        seeded = engine.seed_from_closed_bars(bars)
        if seeded:
            seeded_history_source = "csv"

    assert seeded == 1
    assert seeded_history_source == "csv"
