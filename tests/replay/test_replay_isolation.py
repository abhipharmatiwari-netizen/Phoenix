import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone

import google.auth

from app.brokers.base import (
    OrderPurpose,
    OrderRequest,
    OrderSide,
    OrderType,
    ProductType,
    TimeInForce,
)
from app.core.identifiers import StrategyId
from app.orders.replay_context import REPLAY_ISOLATED_EXECUTION_ENV
from app.orders.strategy_bridge import place_order_via_bridge
from scripts.replay.mock_execution import MockExecutionRecorder
from scripts.replay.pnl_tracker import PnLTracker
from scripts.replay.report import write_full_report
from scripts.replay.replay_engine import (
    BarRow,
    ReplayConfig,
    ReplayEngine,
    build_mock_instrument_meta,
)
import scripts.replay.replay_engine as replay_engine_mod
import scripts.replay.replay_runtime as replay_runtime_mod
import scripts.replay.run_replay as run_replay_mod


def _sample_bar(close: float = 100.0) -> BarRow:
    ts = datetime(2026, 3, 2, 9, 15, tzinfo=timezone.utc)
    return BarRow(
        ts_start=ts,
        ts_end=ts + timedelta(minutes=5),
        label="NIFTY_IDX",
        timeframe_seconds=300,
        o=close,
        h=close + 1.0,
        l=close - 1.0,
        c=close,
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


class _ReplayOrderStrategy:
    def __init__(self) -> None:
        self._submitted = False

    def on_bar(self, label, timeframe_seconds, candle, indicators) -> None:
        del label, timeframe_seconds, candle, indicators
        if self._submitted:
            return
        response = place_order_via_bridge(
            strategy_id=StrategyId("ema20_strategy"),
            order_req=OrderRequest(
                symbol="NIFTY_TEST",
                quantity=2,
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                product_type=ProductType.INTRADAY,
                time_in_force=TimeInForce.DAY,
                purpose=OrderPurpose.ENTRY,
                tag="REPLAY_ENTRY",
            ),
        )
        assert response.execution_mode == "replay_local"
        self._submitted = True

    def on_tick(self, label, price) -> None:
        del label, price


class _ReplayOrderStrategyWithForceClose:
    def __init__(self) -> None:
        self._submitted = False
        self._open = False

    def on_bar(self, label, timeframe_seconds, candle, indicators) -> None:
        del label, timeframe_seconds, candle, indicators
        if self._submitted:
            return
        response = place_order_via_bridge(
            strategy_id=StrategyId("ema20_strategy"),
            order_req=OrderRequest(
                symbol="NIFTY_TEST",
                quantity=1,
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                product_type=ProductType.INTRADAY,
                time_in_force=TimeInForce.DAY,
                purpose=OrderPurpose.ENTRY,
                tag="REPLAY_ENTRY",
            ),
        )
        assert response.execution_mode == "replay_local"
        self._submitted = True
        self._open = True

    def on_tick(self, label, price) -> None:
        del label, price

    def force_exit_all(self, reason: str = "REPLAY_EOD", *, submit_orders: bool = True) -> None:
        assert submit_orders is True
        if not self._open:
            return
        response = place_order_via_bridge(
            strategy_id=StrategyId("ema20_strategy"),
            order_req=OrderRequest(
                symbol="NIFTY_TEST",
                quantity=1,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                product_type=ProductType.INTRADAY,
                time_in_force=TimeInForce.DAY,
                purpose=OrderPurpose.EXIT,
                tag=f"EMA20_EXIT_{reason}",
            ),
        )
        assert response.execution_mode == "replay_local"
        self._open = False


class _ReplayGateStrategy:
    def __init__(self) -> None:
        self._listener = None
        self._emitted = False

    def set_debug_gate_listener(self, listener) -> None:
        self._listener = listener

    def on_bar(self, label, timeframe_seconds, candle, indicators) -> None:
        del label, indicators
        if self._emitted or not callable(self._listener):
            return
        self._listener(
            {
                "underlying": "NIFTY_IDX",
                "gate": "trend",
                "passed": False,
                "reason": "trend_filter_not_met",
                "timeframe_seconds": timeframe_seconds,
                "bar_start_ts": candle.start_ts.isoformat(),
            }
        )
        self._listener(
            {
                "underlying": "NIFTY_IDX",
                "gate": "trend",
                "passed": False,
                "reason": "trend_filter_not_met",
                "timeframe_seconds": timeframe_seconds,
                "bar_start_ts": candle.start_ts.isoformat(),
            }
        )
        self._listener(
            {
                "underlying": "NIFTY_IDX",
                "gate": "entry",
                "passed": True,
                "reason": "candidate_entry",
                "timeframe_seconds": timeframe_seconds,
                "bar_start_ts": candle.start_ts.isoformat(),
            }
        )
        self._emitted = True

    def on_tick(self, label, price) -> None:
        del label, price


def _filled_recorder(strategy_id: str = "ema20_strategy", underlying: str = "NIFTY"):
    recorder = MockExecutionRecorder()
    recorder.set_db_bar_counts({300: 42})
    entry_ts = datetime(2026, 3, 2, 9, 15, tzinfo=timezone.utc)
    exit_ts = entry_ts + timedelta(minutes=5)
    recorder.set_replay_window(
        trading_dates=[date(2026, 3, 2), date(2026, 3, 3)]
    )

    recorder.set_bar_context(close=100.0, ts=entry_ts, underlying=underlying)
    recorder.record_order(
        strategy_id=strategy_id,
        order_req=OrderRequest(
            symbol="NIFTY_TEST",
            quantity=1,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
            time_in_force=TimeInForce.DAY,
            purpose=OrderPurpose.ENTRY,
            tag="ENTRY",
        ),
    )

    recorder.set_bar_context(close=95.0, ts=exit_ts, underlying=underlying)
    recorder.record_order(
        strategy_id=strategy_id,
        order_req=OrderRequest(
            symbol="NIFTY_TEST",
            quantity=1,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
            time_in_force=TimeInForce.DAY,
            purpose=OrderPurpose.EXIT,
            tag="EXIT",
        ),
    )
    return recorder


def test_replay_engine_keeps_order_flow_local(monkeypatch):
    bar = _sample_bar(close=123.0)

    monkeypatch.setattr(
        replay_runtime_mod,
        "load_bars_from_postgres",
        lambda **_kwargs: [bar],
    )
    monkeypatch.setitem(
        replay_runtime_mod.STRATEGY_BUILDERS,
        "ema20_strategy",
        lambda _underlying, _params: _ReplayOrderStrategy(),
    )
    monkeypatch.setattr(
        "app.orders.strategy_bridge.get_hub_runtime",
        lambda: (_ for _ in ()).throw(
            AssertionError("Replay must not call get_hub_runtime")
        ),
    )
    monkeypatch.setattr(
        "app.tenants.firestore_client.get_active_broker_accounts",
        lambda: (_ for _ in ()).throw(
            AssertionError("Replay must not load active broker accounts")
        ),
    )
    monkeypatch.setattr(
        google.auth,
        "default",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Replay must not request ADC")
        ),
    )

    recorder = ReplayEngine(
        ReplayConfig(
            dsn="postgresql://ignored",
            strategy_id="ema20_strategy",
            underlying_key="NIFTY",
            strategy_params={},
            timeframes=[300],
            table="indicator_bars",
        )
    ).run()

    assert len(recorder.fills) == 1
    assert recorder.db_bar_count == 1
    fill = recorder.fills[0]
    assert fill.fill_price == 123.0
    assert fill.quantity == 2
    assert fill.underlying == "NIFTY"


def test_replay_engine_force_closes_open_positions_at_end(monkeypatch):
    bar = _sample_bar(close=123.0)

    monkeypatch.setattr(
        replay_runtime_mod,
        "load_bars_from_postgres",
        lambda **_kwargs: [bar],
    )
    monkeypatch.setitem(
        replay_runtime_mod.STRATEGY_BUILDERS,
        "ema20_strategy",
        lambda _underlying, _params: _ReplayOrderStrategyWithForceClose(),
    )

    recorder = ReplayEngine(
        ReplayConfig(
            dsn="postgresql://ignored",
            strategy_id="ema20_strategy",
            underlying_key="NIFTY",
            strategy_params={},
            timeframes=[300],
            table="indicator_bars",
        )
    ).run()

    assert recorder.db_bar_count == 1
    assert len(recorder.fills) == 2
    tracker = PnLTracker()
    trades = tracker.process_fills(recorder.fills)
    assert len(trades) == 1
    assert trades[0].exit_reason == "REPLAY_EOD"
    assert trades[0].exit_price == 123.0


def test_replay_engine_records_gate_decisions_for_report(monkeypatch):
    bar = _sample_bar(close=123.0)

    monkeypatch.setattr(
        replay_runtime_mod,
        "load_bars_from_postgres",
        lambda **_kwargs: [bar],
    )
    monkeypatch.setitem(
        replay_runtime_mod.STRATEGY_BUILDERS,
        "ema20_strategy",
        lambda _underlying, _params: _ReplayGateStrategy(),
    )

    recorder = ReplayEngine(
        ReplayConfig(
            dsn="postgresql://ignored",
            strategy_id="ema20_strategy",
            underlying_key="NIFTY",
            strategy_params={},
            timeframes=[300],
            table="indicator_bars",
        )
    ).run()

    rows = recorder.gate_summary_rows()
    assert rows == [
        {
            "gate": "trend",
            "reason": "trend_filter_not_met",
            "passed": False,
            "timeframe_seconds": 300,
            "count": 2,
        },
        {
            "gate": "entry",
            "reason": "candidate_entry",
            "passed": True,
            "timeframe_seconds": 300,
            "count": 1,
        },
    ]


def test_build_mock_instrument_meta_supports_ng_replay_option_prefixes():
    meta = build_mock_instrument_meta("NG_FUT", lot_size=1250)

    assert "NG_FUT" in meta
    assert "NG_ATM_CE" in meta
    assert "NG_ATM_PE" in meta


def test_run_replay_main_evaluate_enables_isolated_flag(monkeypatch, tmp_path, caplog):
    observed = {"flag_values": []}

    def _fake_run_single_replay(**_kwargs):
        observed["flag_values"].append(os.getenv(REPLAY_ISOLATED_EXECUTION_ENV))
        return _filled_recorder()

    monkeypatch.setattr(run_replay_mod, "run_single_replay", _fake_run_single_replay)
    monkeypatch.setattr(
        run_replay_mod,
        "write_full_report",
        lambda **_kwargs: str(tmp_path / "replay_summary.txt"),
    )
    monkeypatch.setattr(
        "app.orders.strategy_bridge.get_hub_runtime",
        lambda: (_ for _ in ()).throw(
            AssertionError("Replay CLI must not bootstrap HubRuntime")
        ),
    )
    monkeypatch.setattr(
        "app.tenants.firestore_client.get_active_broker_accounts",
        lambda: (_ for _ in ()).throw(
            AssertionError("Replay CLI must not touch active account loading")
        ),
    )
    monkeypatch.setattr(
        google.auth,
        "default",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Replay CLI must not request ADC")
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_replay.py",
            "--dsn",
            "postgresql://ignored",
            "--mode",
            "evaluate",
            "--strategy",
            "ema20_strategy",
            "--underlying",
            "NIFTY",
            "--output-dir",
            str(tmp_path),
        ],
    )

    with caplog.at_level(logging.INFO):
        run_replay_mod.main()

    assert observed["flag_values"] == ["1"]
    assert os.getenv(REPLAY_ISOLATED_EXECUTION_ENV) is None
    assert any(
        "Replay startup summary" in rec.message
        and "execution_mode=isolated_local" in rec.message
        for rec in caplog.records
    )
    assert any(
        "Replay combo summary" in rec.message
        and "strategy=ema20_strategy" in rec.message
        and "underlying=NIFTY" in rec.message
        for rec in caplog.records
    )


def test_run_evaluate_reports_actual_db_bar_count(monkeypatch):
    def _fake_run_single_replay(**_kwargs):
        recorder = MockExecutionRecorder()
        recorder.set_db_bar_counts({300: 42, 900: 7})
        recorder.set_replay_window(
            trading_dates=[date(2026, 3, 2), date(2026, 3, 3)]
        )
        return recorder

    monkeypatch.setattr(run_replay_mod, "run_single_replay", _fake_run_single_replay)

    metrics, trackers, bars_loaded, gate_summaries, missing_indicators = run_replay_mod.run_evaluate(
        dsn="postgresql://ignored",
        combos=[("ema20_strategy", "NIFTY")],
        start_date=None,
        end_date=None,
        table="indicator_bars",
    )

    assert metrics["ema20_strategy/NIFTY"].total_trades == 0
    assert metrics["ema20_strategy/NIFTY"].first_date == date(2026, 3, 2)
    assert metrics["ema20_strategy/NIFTY"].last_date == date(2026, 3, 3)
    assert metrics["ema20_strategy/NIFTY"].trading_days == 2
    assert "ema20_strategy/NIFTY" in trackers
    assert bars_loaded["ema20_strategy/NIFTY"] == 49
    assert gate_summaries == {"ema20_strategy/NIFTY": []}
    assert missing_indicators == {}


def test_write_full_report_writes_raw_fills_and_db_bar_counts(tmp_path):
    tracker = PnLTracker()
    recorder = _filled_recorder()
    tracker.process_fills(recorder.fills)

    summary_path = write_full_report(
        output_dir=str(tmp_path),
        all_metrics={
            "ema20_strategy/NIFTY": tracker.compute_metrics(
                "ema20_strategy",
                "NIFTY",
                replay_first_date=recorder.replay_first_date,
                replay_last_date=recorder.replay_last_date,
                replay_trading_days=recorder.replay_trading_days,
            )
        },
        all_trades={"ema20_strategy/NIFTY": tracker},
        optimization_results={},
        recommendations={},
        bars_loaded={"ema20_strategy/NIFTY": recorder.db_bar_count},
        gate_summaries={
            "ema20_strategy/NIFTY": [
                {
                    "gate": "trend",
                    "reason": "trend_filter_not_met",
                    "passed": False,
                    "timeframe_seconds": 300,
                    "count": 4,
                },
                {
                    "gate": "entry",
                    "reason": "candidate_entry",
                    "passed": True,
                    "timeframe_seconds": 300,
                    "count": 1,
                },
            ]
        },
        missing_indicators={},
    )

    summary_text = (tmp_path / "replay_summary.txt").read_text()
    fills_text = (tmp_path / "fills_ema20_strategy_NIFTY.csv").read_text()
    trades_text = (tmp_path / "trades_ema20_strategy_NIFTY.csv").read_text()
    gate_summary_text = (tmp_path / "gate_summary_ema20_strategy_NIFTY.csv").read_text()

    assert str(summary_path).endswith("replay_summary.txt")
    assert "Period:              2026-03-02 to 2026-03-03" in summary_text
    assert "Trading days:        2" in summary_text
    assert "ema20_strategy/NIFTY: 42 DB bars loaded" in summary_text
    assert "GATE DIAGNOSTICS" in summary_text
    assert "trend_filter_not_met (gate=trend, tf=300s): 4" in summary_text
    assert "SESSION RESETS / FINALIZATION" in summary_text
    assert fills_text.splitlines()[0].startswith("timestamp,strategy_id,underlying")
    assert len(fills_text.splitlines()) == 3
    assert len(trades_text.splitlines()) == 2
    assert gate_summary_text.splitlines()[0] == "gate,reason,passed,timeframe_seconds,count"
    assert "trend,trend_filter_not_met,false,300,4" in gate_summary_text
