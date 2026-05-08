from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.brokers.base import (
    OrderPurpose,
    OrderRequest,
    OrderSide,
    OrderType,
    ProductType,
    TimeInForce,
)
from app.core.identifiers import StrategyId
from app.orders.strategy_bridge import place_order_via_bridge
from scripts.replay.execution_models import ExecutionConfig
from scripts.replay.mock_execution import MockExecutionRecorder
from scripts.replay.optimizer import walk_forward_validate
from scripts.replay.pnl_tracker import PnLTracker
from scripts.replay.replay_engine import BarRow, ReplayConfig, ReplayEngine, bar_to_indicators
from scripts.replay.schema import ReplayTableSchema
import scripts.replay.optimizer_runtime as optimizer_runtime_mod
import scripts.replay.replay_runtime as replay_runtime_mod
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


def test_non_force_end_policies_reset_option_price_book_when_flat_at_session_boundary(monkeypatch):
    """A flat strategy must get a fresh synthetic ATM option anchor on the
    next session even when non-force policies preserve indicator history."""
    base_ts = datetime(2026, 3, 2, 15, 10, tzinfo=timezone.utc)
    bars = [_bar(base_ts, close=100.0), _bar(base_ts, close=120.0, day_offset=1)]

    monkeypatch.setattr(replay_runtime_mod, "load_bars_from_postgres", lambda **kwargs: list(bars))
    monkeypatch.setitem(
        replay_runtime_mod.STRATEGY_BUILDERS,
        "ema20_strategy",
        lambda *_args: _SecondSessionOptionEntry(),
    )

    for end_policy in ("carry_over", "daily_mtm"):
        recorder = ReplayEngine(
            ReplayConfig(
                dsn="postgresql://ignored",
                strategy_id="ema20_strategy",
                underlying_key="NIFTY",
                strategy_params={},
                timeframes=[300],
                execution=ExecutionConfig(end_policy=end_policy),
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


def test_walk_forward_validate_uses_out_of_sample_windows_only(monkeypatch):
    observed = []

    def _fake_run_single_replay(**kwargs):
        observed.append((kwargs["start_date"], kwargs["end_date"]))
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
    )

    assert score == 0.0
    assert fold_metrics
    for start_date, end_date in observed:
        assert (end_date - start_date).days == 10


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
