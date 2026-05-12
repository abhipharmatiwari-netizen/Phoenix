from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from app.brokers.base import (
    OrderPurpose,
    OrderResponse,
    OrderRequest,
    OrderSide,
    OrderType,
    ProductType,
    TimeInForce,
)
from app.core.identifiers import StrategyId
from app.orders.strategy_bridge import place_order_via_bridge
from scripts.replay.execution_models import ExecutionConfig
from scripts.replay.mock_execution import MockExecutionRecorder, ReplayFill, ReplayMarketContext
from scripts.replay.optimizer import walk_forward_validate
from scripts.replay.pnl_tracker import PnLTracker
from scripts.replay.replay_engine import BarRow, ReplayConfig, ReplayEngine, bar_to_indicators
from app.strategies.nifty_weekly_credit_spreads import NiftyWeeklyCreditSpreadStrategy
from scripts.replay.schema import ReplayTableSchema
import scripts.replay.optimizer_runtime as optimizer_runtime_mod
import scripts.replay.replay_runtime as replay_runtime_mod
import scripts.replay.run_replay as run_replay_mod
import scripts.replay.report as report_mod


def _bar(ts: datetime, *, close: float, open_: float | None = None, day_offset: int = 0) -> BarRow:
    start = ts + timedelta(days=day_offset)
    return BarRow(
        ts_start=start,
        ts_end=start + timedelta(minutes=5),
        label="NIFTY_IDX",
        timeframe_seconds=300,
        o=float(open_ if open_ is not None else close),
        h=float(max(close, open_ if open_ is not None else close) + 1.0),
        l=float(min(close, open_ if open_ is not None else close) - 1.0),
        c=float(close),
        atr=2.0,
        rsi=45.0,
        macd=0.1,
        macd_signal=0.0,
        macd_hist=0.1,
        ema_20=101.0,
        ema_30=102.0,
        ema_50=103.0,
        exclusive_nifty_ce_buy_ema20_30s=None,
        adx=20.0,
        plus_di=10.0,
        minus_di=20.0,
        di_spread=10.0,
    )


class _EntryOnFirstBar:
    def __init__(self) -> None:
        self._sent = False

    def on_tick(self, label, price) -> None:
        del label, price

    def on_bar(self, label, timeframe_seconds, candle, indicators) -> None:
        del timeframe_seconds, candle, indicators
        if self._sent:
            return
        self._sent = True
        place_order_via_bridge(
            strategy_id=StrategyId("ema20_strategy"),
            order_req=OrderRequest(
                symbol=label,
                quantity=1,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                product_type=ProductType.INTRADAY,
                time_in_force=TimeInForce.DAY,
                purpose=OrderPurpose.ENTRY,
                tag="ENTRY",
            ),
        )


class _EntryThenBoundaryExit:
    def __init__(self) -> None:
        self._open = False
        self._sent = False

    def on_tick(self, label, price) -> None:
        del label, price

    def on_bar(self, label, timeframe_seconds, candle, indicators) -> None:
        del timeframe_seconds, candle, indicators
        if self._sent:
            return
        self._sent = True
        place_order_via_bridge(
            strategy_id=StrategyId("ema20_strategy"),
            order_req=OrderRequest(
                symbol=label,
                quantity=1,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                product_type=ProductType.INTRADAY,
                time_in_force=TimeInForce.DAY,
                purpose=OrderPurpose.ENTRY,
                tag="ENTRY",
            ),
        )
        self._open = True

    def force_exit_all(self, reason: str = "REPLAY_SESSION_BOUNDARY", *, submit_orders: bool = True) -> None:
        assert submit_orders is True
        if not self._open:
            return
        self._open = False
        place_order_via_bridge(
            strategy_id=StrategyId("ema20_strategy"),
            order_req=OrderRequest(
                symbol="NIFTY_IDX",
                quantity=1,
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                product_type=ProductType.INTRADAY,
                time_in_force=TimeInForce.DAY,
                purpose=OrderPurpose.EXIT,
                tag=f"EMA20_EXIT_{reason}",
            ),
        )


class _SecondSessionOptionEntry:
    def __init__(self) -> None:
        self.last_price = {}
        self._bars_seen = 0

    def on_tick(self, label, price) -> None:
        del label, price

    def on_bar(self, label, timeframe_seconds, candle, indicators) -> None:
        del label, timeframe_seconds, candle, indicators
        self._bars_seen += 1
        if self._bars_seen != 2:
            return
        place_order_via_bridge(
            strategy_id=StrategyId("ema20_strategy"),
            order_req=OrderRequest(
                symbol="NIFTY_ATM_CE",
                quantity=1,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                product_type=ProductType.INTRADAY,
                time_in_force=TimeInForce.DAY,
                purpose=OrderPurpose.ENTRY,
                tag="SECOND_SESSION_OPTION_ENTRY",
            ),
        )


def test_bar_to_indicators_uses_private_exclusive_ema_when_present():
    ts = datetime(2026, 3, 2, 9, 15, tzinfo=timezone.utc)
    bar = _bar(ts, close=100.0)
    bar.ema_20 = None
    bar.exclusive_nifty_ce_buy_ema20_30s = 123.45

    indicators = bar_to_indicators(bar, strategy_id="exclusive_nifty_ce_buy")

    assert indicators["exclusive_nifty_ce_buy_ema20_30s"] == 123.45
    assert indicators["ema_20"] == 123.45


def test_exclusive_ce_private_ema_backfill_uses_deterministic_iir():
    ts = datetime(2026, 3, 2, 9, 15, tzinfo=timezone.utc)
    rows = [
        BarRow(
            ts_start=ts + timedelta(seconds=30 * idx),
            ts_end=ts + timedelta(seconds=30 * (idx + 1)),
            label="NIFTY_IDX",
            timeframe_seconds=30,
            o=100.0 + idx,
            h=101.0 + idx,
            l=99.0 + idx,
            c=100.0 + idx,
            atr=1.0,
            rsi=60.0,
            macd=0.0,
            macd_signal=0.0,
            macd_hist=0.0,
            ema_20=None,
            ema_30=None,
            ema_50=None,
            exclusive_nifty_ce_buy_ema20_30s=None,
            adx=30.0,
            plus_di=25.0,
            minus_di=10.0,
            di_spread=15.0,
            series_index=idx,
        )
        for idx in range(25)
    ]

    counts = replay_runtime_mod._backfill_exclusive_ce_ema(rows)

    alpha = 2.0 / 21.0
    ema = 100.0
    expected = None
    for idx in range(25):
        close = 100.0 + idx
        ema = close if idx == 0 else (close * alpha) + (ema * (1.0 - alpha))
        if idx == 19:
            expected = ema
    assert counts["exclusive_nifty_ce_buy_ema20_30s"] == 6
    assert rows[18].exclusive_nifty_ce_buy_ema20_30s is None
    assert rows[19].exclusive_nifty_ce_buy_ema20_30s == expected
    assert rows[19].ema_20 == expected


def test_load_bars_from_postgres_tolerates_missing_optional_columns(monkeypatch):
    ts = datetime(2026, 3, 2, 9, 15, tzinfo=timezone.utc)
    row = (
        ts,
        ts + timedelta(minutes=5),
        "NIFTY_IDX",
        300,
        100.0,
        101.0,
        99.0,
        100.5,
        None,
        45.0,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    )

    class _Cursor:
        def execute(self, query, params):
            self.query = query
            self.params = params

        def fetchmany(self, size):
            del size
            if getattr(self, "_done", False):
                return []
            self._done = True
            return [row]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class _Conn:
        def cursor(self):
            return _Cursor()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        replay_runtime_mod,
        "inspect_table_schema",
        lambda *args, **kwargs: ReplayTableSchema(
            normalized_table="indicator_bars",
            available_columns=("ts_start", "ts_end", "label", "timeframe_seconds", "o", "h", "l", "c", "rsi"),
            required_columns=("ts_start", "ts_end", "label", "timeframe_seconds", "o", "h", "l", "c"),
            optional_columns=(
                "atr",
                "rsi",
                "macd",
                "macd_signal",
                "macd_hist",
                "ema_20",
                "ema_30",
                "ema_50",
                "exclusive_nifty_ce_buy_ema20_30s",
                "adx",
                "plus_di",
                "minus_di",
                "di_spread",
            ),
        ),
    )
    monkeypatch.setattr(replay_runtime_mod.psycopg, "connect", lambda *args, **kwargs: _Conn())

    batch = replay_runtime_mod.load_bars_from_postgres("postgresql://ignored", "NIFTY_IDX", 300)

    assert len(batch) == 1
    assert batch[0].atr is None
    assert "atr" in batch.replay_profile["missing_optional_columns"]
    assert batch.replay_profile["bars_loaded"] == 1


def test_load_bars_from_postgres_ignores_missing_regime_sidecar(monkeypatch):
    ts = datetime(2026, 3, 2, 9, 15, tzinfo=timezone.utc)
    row = (
        ts,
        ts + timedelta(minutes=5),
        "NIFTY_IDX",
        300,
        100.0,
        101.0,
        99.0,
        100.5,
        1.0,
        45.0,
        None,
        None,
        None,
        100.0,
        None,
        None,
        None,
        20.0,
        10.0,
        8.0,
        2.0,
        None,
    )
    executed = []

    class _Cursor:
        description = ()

        def execute(self, query, params=None):
            text = str(query)
            executed.append(text)
            self.query = text
            self.params = params
            if "to_regclass" in text:
                self._fetchone = (None,)

        def fetchone(self):
            return getattr(self, "_fetchone", None)

        def fetchmany(self, size):
            del size
            if "FROM indicator_bars" not in getattr(self, "query", ""):
                return []
            if getattr(self, "_done", False):
                return []
            self._done = True
            return [row]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class _Conn:
        def cursor(self):
            return _Cursor()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        replay_runtime_mod,
        "inspect_table_schema",
        lambda *args, **kwargs: ReplayTableSchema(
            normalized_table="indicator_bars",
            available_columns=(
                "id",
                "ts_start",
                "ts_end",
                "label",
                "timeframe_seconds",
                "o",
                "h",
                "l",
                "c",
                "atr",
                "rsi",
                "ema_20",
                "adx",
                "plus_di",
                "minus_di",
                "di_spread",
            ),
            required_columns=("ts_start", "ts_end", "label", "timeframe_seconds", "o", "h", "l", "c"),
            optional_columns=replay_runtime_mod._OPTIONAL_COLUMNS,
        ),
    )
    monkeypatch.setattr(replay_runtime_mod.psycopg, "connect", lambda *args, **kwargs: _Conn())

    batch = replay_runtime_mod.load_bars_from_postgres("postgresql://ignored", "NIFTY_IDX", 300)

    data_query = next(query for query in executed if "FROM indicator_bars" in query)
    assert "bar_regime" not in data_query
    assert "NULL::TEXT AS replay_regime" in data_query
    assert len(batch) == 1
    assert batch.replay_profile["regime_sidecar_available"] is False


def test_regime_sidecar_table_is_scoped_to_source_table():
    assert replay_runtime_mod.regime_sidecar_table_name("indicator_bars")[0] == "bar_regime"
    assert (
        replay_runtime_mod.regime_sidecar_table_name("custom_indicator_bars")[0]
        == "custom_indicator_bars_bar_regime"
    )
    assert (
        replay_runtime_mod.regime_sidecar_table_name("archive.indicator_bars")[0]
        == "archive.bar_regime"
    )


def test_load_bars_from_postgres_uses_custom_regime_sidecar(monkeypatch):
    ts = datetime(2026, 3, 2, 9, 15, tzinfo=timezone.utc)
    row = (
        ts,
        ts + timedelta(minutes=5),
        "NIFTY_IDX",
        300,
        100.0,
        101.0,
        99.0,
        100.5,
        1.0,
        45.0,
        None,
        None,
        None,
        100.0,
        None,
        None,
        None,
        20.0,
        10.0,
        8.0,
        2.0,
        "TRENDING_UP",
    )
    executed = []

    class _Column:
        def __init__(self, name):
            self.name = name

    class _Cursor:
        description = ()

        def execute(self, query, params=None):
            text = str(query)
            executed.append(text)
            self.query = text
            self.params = params
            if "to_regclass" in text:
                self._fetchone = ("custom_indicator_bars_bar_regime",)
            elif "WHERE FALSE" in text:
                self.description = [
                    _Column("bar_id"),
                    _Column("regime"),
                    _Column("classifier_version"),
                ]

        def fetchone(self):
            return getattr(self, "_fetchone", None)

        def fetchmany(self, size):
            del size
            if "FROM custom_indicator_bars" not in getattr(self, "query", ""):
                return []
            if "WHERE FALSE" in getattr(self, "query", ""):
                return []
            if getattr(self, "_done", False):
                return []
            self._done = True
            return [row]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class _Conn:
        def cursor(self):
            return _Cursor()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        replay_runtime_mod,
        "inspect_table_schema",
        lambda *args, **kwargs: ReplayTableSchema(
            normalized_table="custom_indicator_bars",
            available_columns=(
                "id",
                "ts_start",
                "ts_end",
                "label",
                "timeframe_seconds",
                "o",
                "h",
                "l",
                "c",
                "atr",
                "rsi",
                "ema_20",
                "adx",
                "plus_di",
                "minus_di",
                "di_spread",
            ),
            required_columns=("ts_start", "ts_end", "label", "timeframe_seconds", "o", "h", "l", "c"),
            optional_columns=replay_runtime_mod._OPTIONAL_COLUMNS,
        ),
    )
    monkeypatch.setattr(replay_runtime_mod.psycopg, "connect", lambda *args, **kwargs: _Conn())

    batch = replay_runtime_mod.load_bars_from_postgres(
        "postgresql://ignored",
        "NIFTY_IDX",
        300,
        table="custom_indicator_bars",
    )

    data_query = next(
        query
        for query in executed
        if "SELECT" in query and "FROM custom_indicator_bars" in query and "WHERE FALSE" not in query
    )
    assert "custom_indicator_bars_bar_regime" in data_query
    assert batch[0].regime == "TRENDING_UP"
    assert batch.replay_profile["regime_sidecar_available"] is True


def test_replay_regime_versions_prefer_dynamic_policy_hash():
    versions = replay_runtime_mod._replay_regime_classifier_versions(
        strategy_id="ema20_strategy",
        strategy_params={
            "dynamic_policy": {
                "enabled": True,
                "policy_id": "ema20_custom",
                "thresholds": {"adx_trend": 30.0},
            }
        },
    )

    assert versions[0].startswith(f"{replay_runtime_mod.CLASSIFIER_VERSION}:")
    assert versions[0].endswith(":ema20")
    assert versions[-1] == replay_runtime_mod.CLASSIFIER_VERSION


def test_replay_regime_versions_include_context_builder_signature():
    versions = replay_runtime_mod._replay_regime_classifier_versions(
        strategy_id="put_momentum_scalper",
        strategy_params={
            "dynamic_policy": {
                "enabled": True,
                "policy_id": "put_custom",
                "thresholds": {"adx_trend": 18.0},
            }
        },
    )

    assert ":ema20:ctx" in versions[0]
    assert versions[1].endswith(":ema20")
    assert versions[-1] == replay_runtime_mod.CLASSIFIER_VERSION


def test_load_bars_from_postgres_orders_all_regime_version_fallbacks(monkeypatch):
    ts = datetime(2026, 3, 2, 9, 15, tzinfo=timezone.utc)
    row = (
        ts,
        ts + timedelta(minutes=5),
        "NIFTY_IDX",
        300,
        100.0,
        101.0,
        99.0,
        100.5,
        1.0,
        45.0,
        None,
        None,
        None,
        100.0,
        None,
        None,
        None,
        20.0,
        10.0,
        8.0,
        2.0,
        "TRENDING_UP",
    )
    executed = []

    class _Column:
        def __init__(self, name):
            self.name = name

    class _Cursor:
        description = ()

        def execute(self, query, params=None):
            text = str(query)
            executed.append((text, tuple(params or ())))
            self.query = text
            if "to_regclass" in text:
                self._fetchone = ("bar_regime",)
            elif "WHERE FALSE" in text:
                self.description = [
                    _Column("bar_id"),
                    _Column("regime"),
                    _Column("classifier_version"),
                ]

        def fetchone(self):
            return getattr(self, "_fetchone", None)

        def fetchmany(self, size):
            del size
            if "FROM indicator_bars" not in getattr(self, "query", ""):
                return []
            if "WHERE FALSE" in getattr(self, "query", ""):
                return []
            if getattr(self, "_done", False):
                return []
            self._done = True
            return [row]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class _Conn:
        def cursor(self):
            return _Cursor()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        replay_runtime_mod,
        "inspect_table_schema",
        lambda *args, **kwargs: ReplayTableSchema(
            normalized_table="indicator_bars",
            available_columns=(
                "id",
                "ts_start",
                "ts_end",
                "label",
                "timeframe_seconds",
                "o",
                "h",
                "l",
                "c",
                "atr",
                "rsi",
                "ema_20",
                "adx",
                "plus_di",
                "minus_di",
                "di_spread",
            ),
            required_columns=("ts_start", "ts_end", "label", "timeframe_seconds", "o", "h", "l", "c"),
            optional_columns=replay_runtime_mod._OPTIONAL_COLUMNS,
        ),
    )
    monkeypatch.setattr(replay_runtime_mod.psycopg, "connect", lambda *args, **kwargs: _Conn())

    replay_runtime_mod.load_bars_from_postgres(
        "postgresql://ignored",
        "NIFTY_IDX",
        300,
        strategy_id="ema20_strategy",
        strategy_params={
            "dynamic_policy": {
                "enabled": True,
                "policy_id": "ema20_custom",
                "thresholds": {"adx_trend": 30.0},
            }
        },
    )

    data_query, params = next(
        (query, params)
        for query, params in executed
        if "SELECT" in query and "FROM indicator_bars" in query and "WHERE FALSE" not in query
    )
    assert "WHEN %s THEN 0 WHEN %s THEN 1 WHEN %s THEN 2" in data_query
    assert params[0:3] == params[3:6]


def test_synthetic_regimes_use_replay_policy_thresholds():
    base_ts = datetime(2026, 3, 2, 9, 15, tzinfo=timezone.utc)
    rows = []
    for idx in range(14):
        row = _bar(base_ts + timedelta(minutes=5 * idx), close=110.0 + idx)
        row.adx = 20.0
        row.plus_di = 30.0
        row.minus_di = 10.0
        row.di_spread = 20.0
        row.ema_20 = 100.0 + idx
        rows.append(row)

    replay_runtime_mod._attach_synthetic_regimes(
        rows,
        strategy_id="put_momentum_scalper",
        strategy_params={
            "dynamic_policy": {
                "enabled": True,
                "policy_id": "put_custom",
                "hold_bars": 1,
                "thresholds": {
                    "adx_trend": 15.0,
                    "di_spread_trend": 5.0,
                    "ema_slope_trend_min": 0.0,
                },
            }
        },
    )

    assert rows[-1].regime == "TRENDING_UP"


def test_next_bar_open_fill_uses_following_bar_open(monkeypatch):
    base_ts = datetime(2026, 3, 2, 9, 15, tzinfo=timezone.utc)
    bars = [_bar(base_ts, close=100.0, open_=99.0), _bar(base_ts + timedelta(minutes=5), close=106.0, open_=105.0)]

    monkeypatch.setattr(replay_runtime_mod, "load_bars_from_postgres", lambda **kwargs: list(bars))
    monkeypatch.setitem(replay_runtime_mod.STRATEGY_BUILDERS, "ema20_strategy", lambda *_args: _EntryOnFirstBar())

    recorder = ReplayEngine(
        ReplayConfig(
            dsn="postgresql://ignored",
            strategy_id="ema20_strategy",
            underlying_key="NIFTY",
            strategy_params={},
            timeframes=[300],
            execution=ExecutionConfig(fill_mode="next_bar_open_fill", tick_model="open_close"),
        )
    ).run()

    assert len(recorder.fills) == 1
    assert recorder.fills[0].fill_price == 105.0
    assert recorder.fills[0].fill_note == "next_bar_open"


def test_next_bar_open_fill_uses_chronological_bar_after_regime_filter(monkeypatch):
    base_ts = datetime(2026, 3, 2, 9, 15, tzinfo=timezone.utc)
    bars = [
        _bar(base_ts, close=99.0, open_=98.0),
        _bar(base_ts + timedelta(minutes=5), close=100.0, open_=100.0),
        _bar(base_ts + timedelta(minutes=10), close=101.0, open_=101.0),
        _bar(base_ts + timedelta(minutes=15), close=106.0, open_=105.0),
    ]
    for idx, bar in enumerate(bars):
        bar.series_index = idx
        bar.regime = "TRENDING_UP" if idx in {1, 3} else "NORMAL"

    monkeypatch.setattr(replay_runtime_mod, "load_bars_from_postgres", lambda **kwargs: list(bars))
    monkeypatch.setitem(replay_runtime_mod.STRATEGY_BUILDERS, "ema20_strategy", lambda *_args: _EntryOnFirstBar())

    recorder = ReplayEngine(
        ReplayConfig(
            dsn="postgresql://ignored",
            strategy_id="ema20_strategy",
            underlying_key="NIFTY",
            strategy_params={},
            timeframes=[300],
            filter_regime="TRENDING_UP",
            execution=ExecutionConfig(fill_mode="next_bar_open_fill", tick_model="open_close"),
        )
    ).run()

    assert len(recorder.fills) == 1
    assert recorder.fills[0].fill_price == 101.0
    assert recorder.fills[0].fill_note == "next_bar_open"


def test_derived_indicators_are_precomputed_from_unfiltered_history():
    base_ts = datetime(2026, 3, 2, 9, 15, tzinfo=timezone.utc)
    rows = [
        _bar(base_ts + timedelta(minutes=5 * idx), close=100.0 + idx)
        for idx in range(3)
    ]
    for row in rows:
        row.ema_20 = None

    replay_runtime_mod._precompute_derived_indicator_history(
        rows,
        strategy_id="ema20_strategy",
        strategy_params={"ema_period": 3},
    )

    assert rows[2].derived_indicators["ema_3"] == 101.25


def test_pnl_tracker_pairs_ema20_exit_without_position_label():
    ts = datetime(2026, 3, 2, 9, 15, tzinfo=timezone.utc)
    fills = [
        ReplayFill(
            timestamp=ts,
            strategy_id="ema20_strategy",
            underlying="NIFTY",
            side="BUY",
            purpose="ENTRY",
            tag="EMA20_ENTRY",
            quantity=1,
            fill_price=100.0,
            strategy_context={"_replay": {"position_label": "NIFTY_ATM_CE"}},
            symbol="NIFTY26MAR24000CE",
        ),
        ReplayFill(
            timestamp=ts + timedelta(minutes=5),
            strategy_id="ema20_strategy",
            underlying="NIFTY",
            side="SELL",
            purpose="EXIT",
            tag="EMA20_EXIT_TARGET",
            quantity=1,
            fill_price=110.0,
            strategy_context={"_replay": {}},
            symbol="NIFTY26MAR24000CE",
        ),
    ]

    trades = PnLTracker(fee_model=False).process_fills(fills)

    assert len(trades) == 1
    assert trades[0].gross_pnl == 10.0
    assert trades[0].exit_reason == "TARGET"


def test_pnl_tracker_pairs_overlapping_same_label_fifo():
    ts = datetime(2026, 3, 2, 9, 15, tzinfo=timezone.utc)
    fills = [
        ReplayFill(
            timestamp=ts,
            strategy_id="nifty_weekly_credit_spreads",
            underlying="NIFTY",
            side="SELL",
            purpose="ENTRY",
            tag="PUT_SPREAD_SHORT",
            quantity=1,
            fill_price=100.0,
            strategy_context={"_replay": {"position_label": "NIFTY_PE_19800"}},
            symbol="NIFTY_PE_19800",
        ),
        ReplayFill(
            timestamp=ts + timedelta(minutes=1),
            strategy_id="nifty_weekly_credit_spreads",
            underlying="NIFTY",
            side="SELL",
            purpose="ENTRY",
            tag="PUT_SPREAD_SHORT",
            quantity=1,
            fill_price=105.0,
            strategy_context={"_replay": {"position_label": "NIFTY_PE_19800"}},
            symbol="NIFTY_PE_19800",
        ),
        ReplayFill(
            timestamp=ts + timedelta(minutes=2),
            strategy_id="nifty_weekly_credit_spreads",
            underlying="NIFTY",
            side="BUY",
            purpose="EXIT",
            tag="EXIT",
            quantity=1,
            fill_price=90.0,
            strategy_context={"_replay": {"position_label": "NIFTY_PE_19800"}},
            symbol="NIFTY_PE_19800",
        ),
        ReplayFill(
            timestamp=ts + timedelta(minutes=3),
            strategy_id="nifty_weekly_credit_spreads",
            underlying="NIFTY",
            side="BUY",
            purpose="EXIT",
            tag="EXIT",
            quantity=1,
            fill_price=95.0,
            strategy_context={"_replay": {"position_label": "NIFTY_PE_19800"}},
            symbol="NIFTY_PE_19800",
        ),
    ]

    trades = PnLTracker(fee_model=False).process_fills(fills)

    assert [trade.gross_pnl for trade in trades] == [10.0, 10.0]
    assert trades[0].entry_price == 100.0
    assert trades[1].entry_price == 105.0


def test_pnl_tracker_groups_credit_spread_legs_by_spread_id():
    ts = datetime(2026, 3, 2, 9, 15, tzinfo=timezone.utc)
    spread_context = {"spread_id": "spread-1", "spread_type": "PUT_CREDIT"}
    fills = [
        ReplayFill(
            timestamp=ts,
            strategy_id="nifty_weekly_credit_spreads",
            underlying="NIFTY",
            side="SELL",
            purpose="ENTRY",
            tag="PUT_SPREAD_SHORT",
            quantity=1,
            fill_price=100.0,
            strategy_context={**spread_context, "_replay": {"position_label": "NIFTY_PE_19800"}},
            symbol="NIFTY_PE_19800",
        ),
        ReplayFill(
            timestamp=ts,
            strategy_id="nifty_weekly_credit_spreads",
            underlying="NIFTY",
            side="BUY",
            purpose="ENTRY",
            tag="PUT_SPREAD_LONG",
            quantity=1,
            fill_price=30.0,
            strategy_context={**spread_context, "_replay": {"position_label": "NIFTY_PE_19700"}},
            symbol="NIFTY_PE_19700",
        ),
        ReplayFill(
            timestamp=ts + timedelta(minutes=5),
            strategy_id="nifty_weekly_credit_spreads",
            underlying="NIFTY",
            side="BUY",
            purpose="EXIT",
            tag="EXIT",
            quantity=1,
            fill_price=80.0,
            strategy_context={"_replay": {"position_label": "NIFTY_PE_19800"}},
            symbol="NIFTY_PE_19800",
        ),
        ReplayFill(
            timestamp=ts + timedelta(minutes=5),
            strategy_id="nifty_weekly_credit_spreads",
            underlying="NIFTY",
            side="SELL",
            purpose="EXIT",
            tag="EXIT",
            quantity=1,
            fill_price=20.0,
            strategy_context={"_replay": {"position_label": "NIFTY_PE_19700"}},
            symbol="NIFTY_PE_19700",
        ),
    ]

    trades = PnLTracker(fee_model=False).process_fills(fills)

    assert len(trades) == 1
    assert trades[0].strategy_context["spread_id"] == "spread-1"
    assert trades[0].gross_pnl == 10.0
    assert trades[0].net_pnl == 10.0


def test_next_bar_open_option_entry_keeps_option_proxy_price():
    ts = datetime(2026, 3, 2, 9, 15, tzinfo=timezone.utc)
    recorder = MockExecutionRecorder(
        execution_config=ExecutionConfig(fill_mode="next_bar_open_fill")
    )
    recorder.set_market_context(
        ReplayMarketContext(
            timestamp=ts,
            underlying="NIFTY",
            current_price=20000.0,
            phase="bar_close",
            next_open_price=20100.0,
            next_open_ts=ts + timedelta(minutes=5),
            instrument_prices={"NIFTY_CE_20000": 123.0},
            next_instrument_prices={"NIFTY_CE_20000": 150.0},
        )
    )

    recorder.record_order(
        strategy_id="nifty_weekly_credit_spreads",
        underlying="NIFTY",
        order_req=OrderRequest(
            symbol="NIFTY_CE_20000",
            quantity=1,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
            time_in_force=TimeInForce.DAY,
            purpose=OrderPurpose.ENTRY,
            tag="ENTRY",
            position_label="NIFTY_CE_20000",
        ),
    )

    assert recorder.fills[0].timestamp == ts + timedelta(minutes=5)
    assert recorder.fills[0].fill_price == 150.0
    assert recorder.fills[0].fill_note == "next_bar_open_option_proxy"


def test_nifty_weekly_expiry_switches_to_tuesday_from_september_2025():
    assert replay_runtime_mod._next_weekly_expiry(date(2025, 8, 28)) == date(2025, 8, 28)
    assert replay_runtime_mod._next_weekly_expiry(date(2025, 8, 29)) == date(2025, 9, 2)
    assert replay_runtime_mod._next_weekly_expiry(date(2025, 9, 1)) == date(2025, 9, 2)
    assert replay_runtime_mod._next_weekly_expiry(date(2026, 3, 2)) == date(2026, 3, 2)


def test_nifty_spread_replay_clock_drives_entry_ids(monkeypatch):
    fixed_now = datetime(2026, 3, 3, 10, 15, tzinfo=replay_runtime_mod.IST)
    meta = replay_runtime_mod.build_nifty_spread_instrument_meta(
        "NIFTY_IDX",
        65,
        min_strike=19600,
        max_strike=19800,
    )
    strategy = NiftyWeeklyCreditSpreadStrategy(
        meta,
        order_client=None,
        risk_manager=None,
        env_prefix="NIFTY_",
        underlying_label="NIFTY_IDX",
        params={"account_equity": 100000.0, "lot_size": 65},
    )
    strategy._now_ist = lambda: fixed_now
    strategy.last_price.update(
        {
            "NIFTY_PE_19800": 100.0,
            "NIFTY_PE_19600": 70.0,
        }
    )

    def _accepted(**kwargs):
        del kwargs
        return OrderResponse(
            broker_order_id="REPLAY",
            status="COMPLETE",
            message="ok",
            filled_quantity=65,
            average_price=1.0,
        )

    monkeypatch.setattr(strategy, "_place_order", _accepted)

    assert strategy._enter_spread(
        "PUT_SPREAD",
        "NIFTY_PE_19800",
        "NIFTY_PE_19600",
        credit=30.0,
        width_pts=200.0,
        expiry=fixed_now.date(),
    )
    assert strategy._enter_spread(
        "PUT_SPREAD",
        "NIFTY_PE_19800",
        "NIFTY_PE_19600",
        credit=30.0,
        width_pts=200.0,
        expiry=fixed_now.date(),
    )

    spread_ids = sorted(strategy.open_spreads)
    assert len(spread_ids) == 2
    assert str(int(fixed_now.timestamp() * 1000)) in spread_ids[0]
    assert spread_ids[0] != spread_ids[1]


def test_synthetic_regimes_feed_persisted_rows_into_classifier_state(monkeypatch):
    base_ts = datetime(2026, 3, 2, 9, 15, tzinfo=timezone.utc)
    rows = [
        _bar(base_ts, close=100.0),
        _bar(base_ts + timedelta(minutes=5), close=101.0),
    ]
    rows[0].regime = "TRENDING_UP"
    rows[1].regime = None
    built = []

    class _Builder:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def build(self, *, candle, indicators):
            built.append((candle.start_ts, dict(indicators)))
            return SimpleNamespace()

    class _Classifier:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def update(self, context):
            del context
            return SimpleNamespace(value=f"SYNTH_{len(built)}")

    monkeypatch.setattr(replay_runtime_mod, "MarketContextBuilder", _Builder)
    monkeypatch.setattr(replay_runtime_mod, "RegimeClassifier", _Classifier)

    counts = replay_runtime_mod._attach_synthetic_regimes(rows)

    assert len(built) == 2
    assert rows[0].regime == "TRENDING_UP"
    assert rows[1].regime == "SYNTH_2"
    assert counts == {"regime": 1}


def test_gate_summary_groups_by_regime():
    recorder = MockExecutionRecorder()
    recorder.set_market_context(
        replay_runtime_mod.ReplayMarketContext(
            timestamp=datetime(2026, 3, 2, 9, 15, tzinfo=timezone.utc),
            underlying="NIFTY",
            current_price=100.0,
            phase="bar_close",
            indicators={"regime": "TRENDING_UP"},
        )
    )

    recorder.record_gate_decision(
        strategy_id="exclusive_nifty_ce_buy",
        payload={"gate": "entry", "passed": False, "reason": "rsi_band"},
    )

    rows = recorder.gate_summary_rows()
    assert rows[0]["regime"] == "TRENDING_UP"
    assert rows[0]["count"] == 1


def test_nifty_weekly_credit_spreads_replay_builder_runs(monkeypatch):
    base_ts = datetime(2026, 3, 2, 9, 15, tzinfo=timezone.utc)
    bars = [
        _bar(base_ts, close=24000.0, open_=23980.0),
        _bar(base_ts + timedelta(minutes=5), close=24020.0, open_=24000.0),
    ]
    for row in bars:
        row.ema_20 = 23900.0
        row.rsi = 60.0
        row.macd_hist = 1.0
        row.atr = 50.0

    monkeypatch.setattr(replay_runtime_mod, "load_bars_from_postgres", lambda **kwargs: list(bars))

    recorder = ReplayEngine(
        ReplayConfig(
            dsn="postgresql://ignored",
            strategy_id="nifty_weekly_credit_spreads",
            underlying_key="NIFTY",
            strategy_params={},
            timeframes=[300],
        )
    ).run()

    assert all(fill.strategy_id == "nifty_weekly_credit_spreads" for fill in recorder.fills)


def test_session_boundary_finalizes_open_position_under_force_exit(monkeypatch):
    """Legacy --end-policy=force_exit closes any open position at the
    session boundary (REPLAY_SESSION_BOUNDARY exit reason)."""
    base_ts = datetime(2026, 3, 2, 15, 10, tzinfo=timezone.utc)
    bars = [_bar(base_ts, close=100.0), _bar(base_ts, close=101.0, day_offset=1)]

    monkeypatch.setattr(replay_runtime_mod, "load_bars_from_postgres", lambda **kwargs: list(bars))
    monkeypatch.setitem(replay_runtime_mod.STRATEGY_BUILDERS, "ema20_strategy", lambda *_args: _EntryThenBoundaryExit())

    recorder = ReplayEngine(
        ReplayConfig(
            dsn="postgresql://ignored",
            strategy_id="ema20_strategy",
            underlying_key="NIFTY",
            strategy_params={},
            timeframes=[300],
            execution=ExecutionConfig(end_policy="force_exit"),
        )
    ).run()

    tracker = PnLTracker()
    trades = tracker.process_fills(recorder.fills)
    assert len(trades) == 1
    assert trades[0].exit_reason == "REPLAY_SESSION_BOUNDARY"
    assert recorder.finalization_events[0]["reason"] == "REPLAY_SESSION_BOUNDARY"
    assert recorder.finalization_events[0]["realized"] is True


def test_carry_over_does_not_finalize_at_session_boundary(monkeypatch):
    """Issue #216: the default --end-policy=carry_over does NOT close
    open positions at session boundaries -- the position carries across
    days and the replay window end is recorded as an unrealised mark, not
    an EXIT fill folded into realized metrics."""
    base_ts = datetime(2026, 3, 2, 15, 10, tzinfo=timezone.utc)
    bars = [_bar(base_ts, close=100.0), _bar(base_ts, close=101.0, day_offset=1)]

    monkeypatch.setattr(replay_runtime_mod, "load_bars_from_postgres", lambda **kwargs: list(bars))
    monkeypatch.setitem(replay_runtime_mod.STRATEGY_BUILDERS, "ema20_strategy", lambda *_args: _EntryThenBoundaryExit())

    recorder = ReplayEngine(
        ReplayConfig(
            dsn="postgresql://ignored",
            strategy_id="ema20_strategy",
            underlying_key="NIFTY",
            strategy_params={},
            timeframes=[300],
            # Default execution = carry_over (no explicit end_policy override).
        )
    ).run()

    tracker = PnLTracker()
    trades = tracker.process_fills(recorder.fills)
    # No realized exit was emitted: window-end marks must not affect realized
    # trade count, win/loss stats, or net PnL.
    assert len(trades) == 0
    metrics = tracker.compute_metrics("ema20_strategy", "NIFTY")
    assert metrics.total_trades == 0
    assert metrics.net_pnl == 0.0
    # No session-boundary finalization event was recorded.
    finalize_reasons = [e.get("reason") for e in recorder.finalization_events]
    assert "REPLAY_SESSION_BOUNDARY" not in finalize_reasons, (
        f"carry_over must not finalize at session boundary; got {finalize_reasons!r}"
    )
    assert "REPLAY_WINDOW_END_FORCED" in finalize_reasons
    window_marks = [
        e for e in recorder.finalization_events
        if e.get("reason") == "REPLAY_WINDOW_END_FORCED"
    ]
    assert window_marks and window_marks[0].get("realized") is False


def test_carry_over_resets_option_price_book_when_flat_at_session_boundary(monkeypatch):
    """A flat strategy must get a fresh synthetic ATM option anchor on the
    next session even though carry_over preserves indicator history."""
    base_ts = datetime(2026, 3, 2, 15, 10, tzinfo=timezone.utc)
    bars = [_bar(base_ts, close=100.0), _bar(base_ts, close=120.0, day_offset=1)]

    monkeypatch.setattr(replay_runtime_mod, "load_bars_from_postgres", lambda **kwargs: list(bars))
    monkeypatch.setitem(
        replay_runtime_mod.STRATEGY_BUILDERS,
        "ema20_strategy",
        lambda *_args: _SecondSessionOptionEntry(),
    )

    recorder = ReplayEngine(
        ReplayConfig(
            dsn="postgresql://ignored",
            strategy_id="ema20_strategy",
            underlying_key="NIFTY",
            strategy_params={},
            timeframes=[300],
            # Default execution = carry_over. The strategy is flat at the
            # boundary, so the option proxy should re-anchor on day two.
        )
    ).run()

    entry_fills = [fill for fill in recorder.fills if fill.tag == "SECOND_SESSION_OPTION_ENTRY"]
    assert len(entry_fills) == 1
    assert entry_fills[0].symbol == "NIFTY_ATM_CE"
    # With a fresh day-two anchor, base premium is max(ATR * 2, 2% underlying)
    # = max(4.0, 2.4). If the day-one 100.0 anchor leaks, this fill is 11.0.
    assert entry_fills[0].fill_price == 4.0


def test_daily_mtm_records_snapshot_event_at_session_boundary(monkeypatch):
    """daily_mtm policy: position carries over (like carry_over) but a
    daily_mtm_snapshot session_event is recorded at each boundary so
    downstream reporting can inspect unrealised marks separately."""
    base_ts = datetime(2026, 3, 2, 15, 10, tzinfo=timezone.utc)
    bars = [_bar(base_ts, close=100.0), _bar(base_ts, close=101.0, day_offset=1)]

    monkeypatch.setattr(replay_runtime_mod, "load_bars_from_postgres", lambda **kwargs: list(bars))
    monkeypatch.setitem(replay_runtime_mod.STRATEGY_BUILDERS, "ema20_strategy", lambda *_args: _EntryThenBoundaryExit())

    recorder = ReplayEngine(
        ReplayConfig(
            dsn="postgresql://ignored",
            strategy_id="ema20_strategy",
            underlying_key="NIFTY",
            strategy_params={},
            timeframes=[300],
            execution=ExecutionConfig(end_policy="daily_mtm"),
        )
    ).run()

    tracker = PnLTracker()
    trades = tracker.process_fills(recorder.fills)
    # No session-boundary close and no realized window-end exit; position is
    # only represented by an unrealised finalization event.
    assert len(trades) == 0
    metrics = tracker.compute_metrics("ema20_strategy", "NIFTY")
    assert metrics.total_trades == 0
    assert metrics.net_pnl == 0.0
    # daily_mtm_snapshot was recorded.
    session_events = [e for e in recorder.session_events if e.get("event") == "daily_mtm_snapshot"]
    assert len(session_events) == 1, (
        f"Expected one daily_mtm_snapshot event, got {recorder.session_events!r}"
    )
    assert session_events[0].get("close_price") == 100.0
    window_marks = [
        e for e in recorder.finalization_events
        if e.get("reason") == "REPLAY_WINDOW_END_FORCED"
    ]
    assert window_marks and window_marks[0].get("realized") is False


def test_invalid_end_policy_raises_at_config_time():
    """normalize_execution_config validates the end_policy choice up
    front; a typo can't silently fall through to default behaviour."""
    import pytest as _pt

    from scripts.replay.execution_models import normalize_execution_config

    with _pt.raises(ValueError, match="end_policy"):
        normalize_execution_config(end_policy="forced")  # typo of force_exit


def test_run_grid_search_threads_filter_regime(monkeypatch):
    observed = []

    def _fake_run_single_replay(**kwargs):
        observed.append(kwargs.get("filter_regime"))
        return MockExecutionRecorder()

    monkeypatch.setattr(optimizer_runtime_mod, "run_single_replay", _fake_run_single_replay)

    optimizer_runtime_mod.run_grid_search(
        dsn="postgresql://ignored",
        strategy_id="ema20_strategy",
        underlying_key="NIFTY",
        max_combos=1,
        filter_regime="TRENDING_UP",
    )

    assert observed
    assert set(observed) == {"TRENDING_UP"}


def test_run_optimize_threads_filter_regime(monkeypatch):
    observed = []

    def _fake_run_grid_search(**kwargs):
        observed.append(kwargs.get("filter_regime"))
        return []

    monkeypatch.setattr(run_replay_mod, "run_grid_search", _fake_run_grid_search)

    run_replay_mod.run_optimize(
        dsn="postgresql://ignored",
        combos=[("ema20_strategy", "NIFTY")],
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 31),
        table="indicator_bars",
        max_combos=1,
        walk_forward=False,
        filter_regime="TRENDING_UP",
    )

    assert observed == ["TRENDING_UP"]


def test_walk_forward_validate_uses_out_of_sample_windows_only(monkeypatch):
    observed = []

    def _fake_run_single_replay(**kwargs):
        observed.append((kwargs["start_date"], kwargs["end_date"], kwargs.get("filter_regime")))
        return MockExecutionRecorder()

    monkeypatch.setattr(optimizer_runtime_mod, "run_single_replay", _fake_run_single_replay)

    score, fold_metrics = walk_forward_validate(
        dsn="postgresql://ignored",
        strategy_id="ema20_strategy",
        underlying_key="NIFTY",
        candidate_params={"ema_period": 20},
        start_date=date(2026, 1, 1),
        end_date=date(2026, 5, 1),
        train_days=30,
        test_days=10,
        step_days=10,
        filter_regime="TRENDING_DOWN",
    )

    assert score == 0.0
    assert fold_metrics
    for start_date, end_date, filter_regime in observed:
        assert (end_date - start_date).days == 10
        assert filter_regime == "TRENDING_DOWN"


def test_write_full_report_emits_json_and_yaml_artifacts(tmp_path):
    tracker = PnLTracker()
    recorder = MockExecutionRecorder()
    summary_path = report_mod.write_full_report(
        output_dir=str(tmp_path),
        all_metrics={"ema20_strategy/NIFTY": tracker.compute_metrics("ema20_strategy", "NIFTY")},
        all_trades={"ema20_strategy/NIFTY": tracker},
        optimization_results={},
        recommendations={},
        bars_loaded={"ema20_strategy/NIFTY": 0},
        gate_summaries={},
        missing_indicators={},
        combo_analyses={"ema20_strategy/NIFTY": {"summary": {}, "data_profile": getattr(recorder, "data_profile", {})}},
        parameter_sensitivity={},
        recommendation_payloads={"ema20_strategy/NIFTY": {"strategy_id": "ema20_strategy", "underlying_key": "NIFTY"}},
    )

    assert summary_path.endswith("replay_summary.txt")
    assert (tmp_path / "replay_report.md").exists()
    assert (tmp_path / "replay_results.json").exists()
    assert (tmp_path / "recommendations.yaml").exists()


def test_ema20_exit_sets_position_label_for_stable_replay_pairing(monkeypatch):
    """Issue #253: EMA20 exit must carry position_label so replay PnL
    pairs entries and exits by option_label (e.g. NIFTY_ATM_CE) without
    depending on the single-candidate fallback in PnLTracker."""

    import app.strategies.ema20_strategy as ema20_mod
    from app.strategies.ema20_strategy import Ema20Position

    strategy = replay_runtime_mod.build_ema20_strategy("NIFTY", {})
    strategy._strategy_id = "ema20_strategy"
    pos = Ema20Position(
        option_label="NIFTY_ATM_CE",
        broker_symbol="NIFTYCEXXXXXX",
        exchange="NFO",
        symbol_token="88888",
        qty=1,
        requested_lots=1,
        lot_size=1,
        broker_qty=1,
        entry_price=100.0,
        sl_price=85.0,
        tp_price=130.0,
        best_price=100.0,
        trail_active=False,
        entry_time=datetime(2026, 3, 2, 9, 15, tzinfo=timezone.utc),
        strategy_context={},
        original_qty=1,
    )
    strategy._managed_positions = {"NIFTY_ATM_CE": pos}
    strategy.last_price = {"NIFTY_ATM_CE": 110.0}

    captured: dict[str, Any] = {}

    def _fake_bridge(*, strategy_id, order_req, **kwargs):
        del strategy_id, kwargs
        captured["order_req"] = order_req
        return OrderResponse(
            broker_order_id="REPLAY",
            status="COMPLETE",
            message="ok",
            filled_quantity=int(order_req.quantity),
            average_price=110.0,
            requested_quantity=int(order_req.quantity),
        )

    monkeypatch.setattr(ema20_mod, "place_order_via_bridge", _fake_bridge)

    strategy._exit_position(reason="TARGET", position=pos)

    order_req = captured.get("order_req")
    assert order_req is not None, "EMA20 _exit_position did not call the bridge"
    assert order_req.position_label == "NIFTY_ATM_CE", (
        f"EMA20 exit must set position_label=NIFTY_ATM_CE, got "
        f"{order_req.position_label!r}"
    )


def test_pnl_tracker_pairs_multiple_simultaneous_labels_when_exits_have_position_label():
    """Issue #253: with the strategy-side fix (EMA20 exit carries
    position_label), the PnL tracker pairs entries+exits across multiple
    simultaneously-open labels without needing _fallback_exit_key (which
    only works when exactly one candidate is open)."""
    ts = datetime(2026, 3, 2, 9, 15, tzinfo=timezone.utc)
    fills = [
        ReplayFill(
            timestamp=ts,
            strategy_id="ema20_strategy",
            underlying="NIFTY",
            side="BUY",
            purpose="ENTRY",
            tag="EMA20_ENTRY",
            quantity=1,
            fill_price=100.0,
            strategy_context={"_replay": {"position_label": "NIFTY_ATM_CE"}},
            symbol="NIFTYCEXXXXXX",
        ),
        ReplayFill(
            timestamp=ts + timedelta(minutes=1),
            strategy_id="ema20_strategy",
            underlying="NIFTY",
            side="BUY",
            purpose="ENTRY",
            tag="EMA20_ENTRY",
            quantity=1,
            fill_price=50.0,
            strategy_context={"_replay": {"position_label": "NIFTY_ATM_PE"}},
            symbol="NIFTYPEXXXXXX",
        ),
        ReplayFill(
            timestamp=ts + timedelta(minutes=5),
            strategy_id="ema20_strategy",
            underlying="NIFTY",
            side="SELL",
            purpose="EXIT",
            tag="EMA20_EXIT_TP",
            quantity=1,
            fill_price=120.0,
            # Issue #253 fix: exit fills carry the same position_label as
            # their entries, allowing PnL pairing to work even when more
            # than one position is open simultaneously.
            strategy_context={"_replay": {"position_label": "NIFTY_ATM_CE"}},
            symbol="NIFTYCEXXXXXX",
        ),
        ReplayFill(
            timestamp=ts + timedelta(minutes=6),
            strategy_id="ema20_strategy",
            underlying="NIFTY",
            side="SELL",
            purpose="EXIT",
            tag="EMA20_EXIT_TP",
            quantity=1,
            fill_price=60.0,
            strategy_context={"_replay": {"position_label": "NIFTY_ATM_PE"}},
            symbol="NIFTYPEXXXXXX",
        ),
    ]

    trades = PnLTracker(fee_model=False).process_fills(fills)

    assert len(trades) == 2
    # The CE trade should be paired with the CE exit (price 120, entry 100)
    # and the PE trade with the PE exit (price 60, entry 50). If the keys
    # mismatched, the trade count would be 0 or the prices would be swapped.
    pairs = sorted((t.entry_price, t.exit_price) for t in trades)
    assert pairs == [(50.0, 60.0), (100.0, 120.0)]


def test_nifty_spread_replay_iron_condor_rollback_emits_put_unwind_fills(monkeypatch):
    """Issue #213 acceptance: when an iron-condor CALL-side _enter_spread
    call fails after the PUT-side succeeds, the strategy must roll back
    the PUT legs via _force_exit_leg. This pins the rollback path so a
    regression cannot silently leave PUT legs naked-short in replay."""

    fixed_now = datetime(2026, 3, 3, 10, 15, tzinfo=replay_runtime_mod.IST)
    meta = replay_runtime_mod.build_nifty_spread_instrument_meta(
        "NIFTY_IDX",
        65,
        min_strike=19000,
        max_strike=21000,
    )
    strategy = NiftyWeeklyCreditSpreadStrategy(
        meta,
        order_client=None,
        risk_manager=None,
        env_prefix="NIFTY_",
        underlying_label="NIFTY_IDX",
        params={
            "account_equity": 10_000_000.0,
            "lot_size": 65,
            # Loosen credit gates so the synthetic prices below pass.
            "min_credit_pct_of_width": 0.05,
            "min_condor_total_credit_pct": 0.05,
            "risk_per_trade_pct": 1.0,
            "max_total_risk_pct": 1.0,
            "max_open_spreads": 10,
        },
    )
    strategy._now_ist = lambda: fixed_now
    # Force a deterministic strike resolver so the test does not depend on
    # the strategy's option-chain lookup helpers.
    strategy._find_option_by_strike = lambda strike, side, expiry: f"NIFTY_{side}_{int(strike)}"
    # Set leg prices so credit-of-width gates pass on both sides.
    strategy.last_price.update(
        {
            "NIFTY_PE_19550": 200.0,
            "NIFTY_PE_19250": 50.0,
            "NIFTY_CE_20050": 200.0,
            "NIFTY_CE_20350": 50.0,
        }
    )

    placed: list[Any] = []

    def _capture_place_order(*, label, side, qty, price, tag, purpose, **kwargs):
        del kwargs
        purpose_text = (
            purpose.value if hasattr(purpose, "value") else str(purpose)
        )
        placed.append(
            {
                "label": label,
                "side": side,
                "qty": qty,
                "price": price,
                "tag": tag,
                "purpose": purpose_text,
            }
        )
        # The CALL ENTRY legs fail so the iron-condor rollback path
        # fires. PUT entries and any subsequent EXIT (rollback) orders all
        # succeed. _enter_spread checks resp.status in {ACCEPTED, FILLED,
        # COMPLETE} so anything else is treated as a rejection.
        if purpose_text == "ENTRY" and "CE" in str(label):
            return OrderResponse(
                broker_order_id="",
                status="REJECTED",
                message="simulated_call_leg_failure",
                filled_quantity=0,
                average_price=None,
                requested_quantity=int(qty),
            )
        return OrderResponse(
            broker_order_id=f"REPLAY_{len(placed)}",
            status="COMPLETE",
            message="ok",
            filled_quantity=int(qty),
            average_price=float(price or 0.0),
            requested_quantity=int(qty),
        )

    monkeypatch.setattr(strategy, "_place_order", _capture_place_order)

    strategy._try_iron_condor(spot=19800.0, expiry=fixed_now.date())

    purposes = [entry["purpose"] for entry in placed]
    assert "ENTRY" in purposes, f"expected ENTRY orders to be placed; got {placed!r}"
    assert "EXIT" in purposes, (
        "iron-condor rollback must emit EXIT orders to unwind PUT legs "
        f"after CALL-side failure; got placed={placed!r}"
    )
    # Both PUT legs must be unwound (one BUY-to-close on the short leg,
    # one SELL-to-close on the long leg). This is the rollback signature
    # specified by issue #213 (matches live _try_iron_condor behaviour).
    exit_orders = [entry for entry in placed if entry["purpose"] == "EXIT"]
    exit_pairs = {(entry["label"], entry["side"]) for entry in exit_orders}
    assert ("NIFTY_PE_19550", "BUY") in exit_pairs, (
        f"PUT-short leg must be bought back to close; got exits={exit_orders!r}"
    )
    assert ("NIFTY_PE_19250", "SELL") in exit_pairs, (
        f"PUT-long leg must be sold to close; got exits={exit_orders!r}"
    )
    # Issue #262: the rollback must ALSO remove the orphaned PUT-spread
    # shell from ``self.open_spreads``. Before the fix, the PUT spread
    # entry remained in the dict after its legs were force-exited, counting
    # toward ``max_open_spreads`` and producing stale state in subsequent
    # bar iterations.
    assert strategy.open_spreads == {}, (
        "iron-condor rollback must remove the PUT-spread shell from "
        f"open_spreads (#262); got {list(strategy.open_spreads.keys())!r}"
    )


def test_nifty_spread_replay_iron_condor_partial_rollback_keeps_spread_tracked(monkeypatch):
    """Issue #262 round-2: when the iron-condor CALL-side fails AND the
    broker REJECTS one (or both) of the rollback EXITs on the PUT legs,
    the spread MUST remain tracked in ``self.open_spreads`` so the
    normal exit-management path can retry. Without this guard, the live
    PUT leg would be orphaned (no spread state) and exposure would go
    unmanaged."""

    fixed_now = datetime(2026, 3, 3, 10, 15, tzinfo=replay_runtime_mod.IST)
    meta = replay_runtime_mod.build_nifty_spread_instrument_meta(
        "NIFTY_IDX",
        65,
        min_strike=19000,
        max_strike=21000,
    )
    strategy = NiftyWeeklyCreditSpreadStrategy(
        meta,
        order_client=None,
        risk_manager=None,
        env_prefix="NIFTY_",
        underlying_label="NIFTY_IDX",
        params={
            "account_equity": 10_000_000.0,
            "lot_size": 65,
            "min_credit_pct_of_width": 0.05,
            "min_condor_total_credit_pct": 0.05,
            "risk_per_trade_pct": 1.0,
            "max_total_risk_pct": 1.0,
            "max_open_spreads": 10,
        },
    )
    strategy._now_ist = lambda: fixed_now
    strategy._find_option_by_strike = lambda strike, side, expiry: f"NIFTY_{side}_{int(strike)}"
    strategy.last_price.update(
        {
            "NIFTY_PE_19550": 200.0,
            "NIFTY_PE_19250": 50.0,
            "NIFTY_CE_20050": 200.0,
            "NIFTY_CE_20350": 50.0,
        }
    )

    placed: list[Any] = []

    def _capture_place_order(*, label, side, qty, price, tag, purpose, **kwargs):
        del kwargs
        purpose_text = (
            purpose.value if hasattr(purpose, "value") else str(purpose)
        )
        placed.append(
            {
                "label": label,
                "side": side,
                "qty": qty,
                "price": price,
                "tag": tag,
                "purpose": purpose_text,
            }
        )
        # CALL ENTRY legs fail (triggers the iron-condor rollback path).
        if purpose_text == "ENTRY" and "CE" in str(label):
            return OrderResponse(
                broker_order_id="",
                status="REJECTED",
                message="simulated_call_leg_failure",
                filled_quantity=0,
                average_price=None,
                requested_quantity=int(qty),
            )
        # On the rollback path: PUT-short EXIT (BUY) is rejected by the
        # broker; PUT-long EXIT (SELL) is accepted. This produces the
        # PARTIAL-ROLLBACK scenario that round-2 must keep tracked.
        if purpose_text == "EXIT" and label == "NIFTY_PE_19550" and side == "BUY":
            return OrderResponse(
                broker_order_id="",
                status="REJECTED",
                message="simulated_broker_rejected_put_short_exit",
                filled_quantity=0,
                average_price=None,
                requested_quantity=int(qty),
            )
        return OrderResponse(
            broker_order_id=f"REPLAY_{len(placed)}",
            status="COMPLETE",
            message="ok",
            filled_quantity=int(qty),
            average_price=float(price or 0.0),
            requested_quantity=int(qty),
        )

    monkeypatch.setattr(strategy, "_place_order", _capture_place_order)

    strategy._try_iron_condor(spot=19800.0, expiry=fixed_now.date())

    # The spread MUST still be tracked because at least one rollback EXIT
    # was broker-rejected. ``_maybe_manage_exits`` needs the spread state
    # to retry.
    assert len(strategy.open_spreads) == 1, (
        "iron-condor partial-rollback must keep the spread tracked when "
        f"any rollback EXIT is broker-rejected (#262 round-2); "
        f"got {list(strategy.open_spreads.keys())!r}"
    )
    put_spread = next(iter(strategy.open_spreads.values()))
    # The successful PUT-long EXIT (SELL) must flip long_exit_done=True
    # so the management path doesn't re-submit it.
    assert put_spread.long_exit_done is True, (
        "PUT-long EXIT was broker-accepted; long_exit_done must be set "
        "so _maybe_manage_exits doesn't double-submit"
    )
    # The rejected PUT-short EXIT (BUY) must leave short_exit_done=False
    # so the management path retries it on the next bar.
    assert put_spread.short_exit_done is False, (
        "PUT-short EXIT was broker-rejected; short_exit_done must remain "
        "False so _maybe_manage_exits retries"
    )
