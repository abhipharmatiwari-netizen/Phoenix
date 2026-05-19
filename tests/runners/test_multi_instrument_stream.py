from datetime import datetime, timezone, timedelta

import asyncio
import importlib
import json
import logging
import threading
from types import SimpleNamespace

import pytest

from app.core.indicators_engine import Candle, GapBackfillRequest
from app.core.maintenance import MaintenanceManager
from app.core.signal_metrics import get_labeled_metrics
from app.runners import multi_instrument_stream as mis
from app.core.indicators_engine import MultiInstrumentIndicatorEngine
from app.core.instrument_control import InstrumentController


def test_import_module_succeeds():
    mod = importlib.import_module("app.runners.multi_instrument_stream")
    assert hasattr(mod, "_parse_timeframe_to_seconds")


def test_parse_timeframe_to_seconds_handles_formats():
    assert mis._parse_timeframe_to_seconds(60) == 60
    assert mis._parse_timeframe_to_seconds("5m") == 300
    assert mis._parse_timeframe_to_seconds("30s") == 30
    assert mis._parse_timeframe_to_seconds("1h") == 3600
    assert mis._parse_timeframe_to_seconds("invalid") is None


def test_default_env_prefix_known_and_fallback():
    assert mis._default_env_prefix("NG_FUT") == "NG_"
    assert mis._default_env_prefix("BANKNIFTY_IDX") == "BANKNIFTY_"
    assert mis._default_env_prefix("CUSTOM_TOKEN") == "CUSTOM_"


def test_is_true_env_interprets_common_values(monkeypatch):
    monkeypatch.setenv("FLAG_ONE", "1")
    monkeypatch.setenv("FLAG_TWO", "false")

    assert mis._is_true_env("FLAG_ONE") is True
    assert mis._is_true_env("FLAG_TWO") is False
    assert mis._is_true_env("MISSING", default=False) is False


def test_is_in_trading_window_equity_and_mcx():
    # 09:30 IST => inside equity and MCX windows
    ts_equity = datetime(2024, 1, 1, 4, 0, tzinfo=timezone.utc)
    assert mis._is_in_trading_window({"exchange": "NSE"}, ts_equity) is True

    # 16:00 IST => outside equity, inside MCX
    ts_after_equity = datetime(2024, 1, 1, 10, 30, tzinfo=timezone.utc)
    assert mis._is_in_trading_window({"exchange": "NSE"}, ts_after_equity) is False
    assert mis._is_in_trading_window({"exchange": "MCX"}, ts_after_equity) is True

    # 00:30 IST next day => outside both
    ts_outside = datetime(2024, 1, 1, 19, 0, tzinfo=timezone.utc)
    assert mis._is_in_trading_window({"exchange": "NSE"}, ts_outside) is False
    assert mis._is_in_trading_window({"exchange": "MCX"}, ts_outside) is False


def test_is_in_trading_window_respects_holiday_config(monkeypatch, tmp_path):
    holiday_path = tmp_path / "market_holidays.json"
    holiday_path.write_text(
        json.dumps({"holidays": {"NSE": ["2026-01-26"]}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("MARKET_HOLIDAY_CONFIG_PATH", str(holiday_path))

    ts = datetime(2026, 1, 26, 4, 0, tzinfo=timezone.utc)  # 09:30 IST
    assert mis._is_in_trading_window({"exchange": "NSE"}, ts) is False


def test_set_tick_flow_metrics_records_lag_and_throttle_state():
    metrics = get_labeled_metrics()
    metrics.reset_for_tests()

    class _Queue:
        def pending_count(self):
            return 7

    class _Dispatcher:
        def active_key_count(self):
            return 3

        def is_throttling(self):
            return True

    mis._set_tick_flow_metrics(
        metrics,
        event_queue=_Queue(),
        tick_dispatcher=_Dispatcher(),
        processing_lag_ms=12.5,
    )

    labels = {"queue": "stream"}
    assert metrics.gauge_value("stream_tick_processing_lag_ms", labels=labels) == 12.5
    assert metrics.gauge_value("stream_tick_queue_pending", labels=labels) == 7.0
    assert metrics.gauge_value("stream_tick_coalesced_keys", labels=labels) == 3.0
    assert metrics.gauge_value("stream_tick_throttle_active", labels=labels) == 1.0


def test_record_tick_throttle_action_tracks_merged_and_dropped_only():
    metrics = get_labeled_metrics()
    metrics.reset_for_tests()

    mis._record_tick_throttle_action(metrics, "merged")
    mis._record_tick_throttle_action(metrics, "dropped")
    mis._record_tick_throttle_action(metrics, "queued")

    assert metrics.counter_value(
        "stream_tick_throttle_total",
        labels={"action": "merged"},
    ) == 1.0
    assert metrics.counter_value(
        "stream_tick_throttle_total",
        labels={"action": "dropped"},
    ) == 1.0
    assert metrics.counter_value(
        "stream_tick_throttle_total",
        labels={"action": "queued"},
    ) == 0.0


def test_position_trailing_lock_tick_submit_is_disabled_by_default():
    def _unexpected_runtime():
        raise AssertionError("runtime should not be resolved when tick mode is disabled")

    assert (
        mis._submit_position_trailing_lock_tick(
            settings=SimpleNamespace(position_trailing_lock_tick_enabled=False),
            label="NG_ATM_CE_295",
            ltp=6.05,
            token="555295",
            symbol="NATURALGAS22MAY26295CE",
            runtime_getter=_unexpected_runtime,
        )
        is False
    )


def test_position_trailing_lock_tick_submit_routes_to_engine(monkeypatch):
    class _Engine:
        def __init__(self):
            self.calls = []

        async def evaluate_tick(self, runners, *, tick_label, tick_symbol, tick_token, price):
            self.calls.append(
                {
                    "runners": list(runners),
                    "tick_label": tick_label,
                    "tick_symbol": tick_symbol,
                    "tick_token": tick_token,
                    "price": price,
                }
            )

    class _BridgeLoop:
        def __init__(self):
            self.timeout_s = None

        def submit(self, coro, *, timeout_s):
            self.timeout_s = timeout_s
            asyncio.run(coro)

    engine = _Engine()
    runner_a = SimpleNamespace(broker_account_id="A1-shadow", runtime_mode="SHADOW")
    runner_b = SimpleNamespace(broker_account_id="A2-shadow", runtime_mode="SHADOW")
    runners_by_id = {
        "A1-shadow": runner_a,
        "A2-shadow": runner_b,
    }
    runtime = SimpleNamespace(
        position_trailing_lock_engine=engine,
        hub=SimpleNamespace(
            list_runner_ids=lambda: ["A1-shadow", "A2-shadow"],
            get_runner=lambda broker_account_id: runners_by_id[broker_account_id],
        ),
    )
    bridge = _BridgeLoop()
    monkeypatch.setattr(
        mis,
        "load_stability_feature_flags",
        lambda: SimpleNamespace(
            strategy_bridge_max_inflight=3,
            strategy_bridge_timeout_s=2.5,
        ),
    )
    monkeypatch.setattr(
        mis,
        "get_shared_bridge_loop",
        lambda *, max_inflight: bridge,
    )

    submitted = mis._submit_position_trailing_lock_tick(
        settings=SimpleNamespace(position_trailing_lock_tick_enabled=True),
        label="NG_ATM_CE_295",
        ltp=6.05,
        token="555295",
        symbol="NATURALGAS22MAY26295CE",
        runtime_getter=lambda: runtime,
    )

    assert submitted is True
    assert bridge.timeout_s == 2.5
    assert engine.calls == [
        {
            "runners": [runner_a, runner_b],
            "tick_label": "NG_ATM_CE_295",
            "tick_symbol": "NATURALGAS22MAY26295CE",
            "tick_token": "555295",
            "price": 6.05,
        }
    ]


def test_filter_stream_universe_keeps_enabled_preopen_equity_labels():
    ts_preopen = datetime(2024, 1, 1, 3, 20, tzinfo=timezone.utc)
    assert mis._is_in_trading_window({"exchange": "NSE"}, ts_preopen) is False

    instrument_meta = {
        "NIFTY_IDX": {
            "exchange": "NSE",
            "exchangeType": 1,
            "kind": "UNDERLYING",
            "underlying": "NIFTY",
            "token": "101",
        },
        "NIFTY24JAN24000CE": {
            "exchange": "NSE",
            "exchangeType": 2,
            "kind": "OPTION",
            "underlying": "NIFTY",
            "token": "102",
        },
        "NG_FUT": {
            "exchange": "MCX",
            "exchangeType": 5,
            "kind": "UNDERLYING",
            "underlying": "NG",
            "token": "201",
        },
    }
    instruments = [
        {"label": "NIFTY_IDX"},
        {"label": "NIFTY24JAN24000CE"},
        {"label": "NG_FUT"},
    ]
    token_labels = {
        "101": "NIFTY_IDX",
        "102": "NIFTY24JAN24000CE",
        "201": "NG_FUT",
    }
    token_list = [
        {"exchangeType": 1, "tokens": ["101"]},
        {"exchangeType": 2, "tokens": ["102"]},
        {"exchangeType": 5, "tokens": ["201"]},
    ]
    controller = InstrumentController(
        {
            "NIFTY_IDX": {"enabled": True},
            "NG_FUT": {"enabled": False},
        }
    )
    underlying_name_to_label = {
        meta.get("underlying"): label
        for label, meta in instrument_meta.items()
        if meta.get("kind") == "UNDERLYING"
    }

    def _resolve_underlying(label: str) -> str:
        meta = instrument_meta.get(label, {})
        if meta.get("kind") == "UNDERLYING":
            return label
        return underlying_name_to_label.get(meta.get("underlying"), label)

    (
        filtered_instruments,
        filtered_token_labels,
        filtered_token_list,
        filtered_instrument_meta,
        allowed_labels,
    ) = mis._filter_stream_universe_to_enabled_labels(
        instruments,
        token_labels,
        token_list,
        instrument_meta,
        instrument_controller=controller,
        underlying_label_for_label=_resolve_underlying,
    )

    assert allowed_labels == {"NIFTY_IDX", "NIFTY24JAN24000CE"}
    assert [item["label"] for item in filtered_instruments] == [
        "NIFTY_IDX",
        "NIFTY24JAN24000CE",
    ]
    assert set(filtered_instrument_meta.keys()) == {
        "NIFTY_IDX",
        "NIFTY24JAN24000CE",
    }
    assert filtered_token_labels == {
        "101": "NIFTY_IDX",
        "102": "NIFTY24JAN24000CE",
    }
    assert filtered_token_list == [
        {"exchangeType": 1, "tokens": ["101"]},
        {"exchangeType": 2, "tokens": ["102"]},
    ]


def test_to_ist_iso_formats_datetime():
    ts = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
    out = mis._to_ist_iso(ts)
    assert "2024-01-01T05:30:00" in out


def test_refresh_daily_levels_cache_calls_refresh(monkeypatch):
    calls: list[str] = []

    class DummyCache:
        def refresh_for_instruments(self, instruments):
            calls.append(",".join(instruments))

    mis._daily_levels_state["cache"] = DummyCache()
    mis._daily_levels_state["instruments"] = ["A", "B"]

    assert mis.refresh_daily_levels_cache() is True
    assert calls == ["A,B"]

    mis._daily_levels_state["cache"] = None
    assert mis.refresh_daily_levels_cache() is False


def test_index_feed_monitor_required_labels_is_defensive_copy():
    class _Runner:
        def subscribe_tokens(self, _tokens):
            return None

        def request_reconnect(self):
            return None

    monitor = mis.IndexFeedMonitor(
        runner=_Runner(),
        instrument_meta={"NIFTY_IDX": {"exchangeType": 1, "token": "99926000"}},
        required_labels={"NIFTY_IDX"},
        warn_seconds=120,
    )
    labels = monitor.required_labels()
    labels.add("EXTRA")
    assert monitor.required_labels() == {"NIFTY_IDX"}


def test_log_instrument_policy_snapshot_handles_empty_and_explicit_allow_lists(caplog):
    ctrl = InstrumentController(
        {
            "NIFTY_IDX": {"enabled": True, "allowed_strategies": ["ema20_strategy"]},
            "BANKNIFTY_IDX": {"enabled": True, "allowed_strategies": []},
        }
    )
    with caplog.at_level(logging.INFO):
        mis._log_instrument_policy_snapshot(ctrl)

    text = "\n".join(rec.message for rec in caplog.records)
    assert "underlying=BANKNIFTY_IDX" in text
    assert "allowed=ALL (empty allow-list)" in text
    assert "underlying=NIFTY_IDX" in text
    assert "allowed=ema20_strategy" in text


def test_strategy_id_for_instance_returns_none_when_missing():
    assert mis._strategy_id_for_instance(object()) is None
    inst = type("S", (), {"_strategy_id": "ema20-trend"})()
    assert mis._strategy_id_for_instance(inst) == "ema20-trend"


def test_filter_strategies_with_missing_routes_drops_unrouted_entries():
    class _RoutingTable:
        def get_routes_for_strategy(self, strategy_id):
            if str(strategy_id) == "ema20-trend":
                return [object()]
            return []

    routed = type("Routed", (), {"_strategy_id": "ema20-trend"})()
    unrouted = type("Unrouted", (), {"_strategy_id": "banknifty-delta-strangle"})()
    strategies = [
        {"name": "ema20_strategy", "underlying": "NIFTY_IDX", "instance": routed},
        {"name": "delta_strangle", "underlying": "BANKNIFTY_IDX", "instance": unrouted},
    ]

    out = mis._filter_strategies_with_missing_routes(
        strategies,
        routing_table=_RoutingTable(),
        fail_fast=False,
    )
    assert len(out) == 1
    assert out[0]["name"] == "ema20_strategy"


def test_filter_strategies_with_missing_routes_fail_fast_raises():
    class _RoutingTable:
        def get_routes_for_strategy(self, _strategy_id):
            return []

    unrouted = type("Unrouted", (), {"_strategy_id": "banknifty-delta-strangle"})()
    strategies = [
        {"name": "delta_strangle", "underlying": "BANKNIFTY_IDX", "instance": unrouted},
    ]

    try:
        mis._filter_strategies_with_missing_routes(
            strategies,
            routing_table=_RoutingTable(),
            fail_fast=True,
        )
    except RuntimeError as exc:
        assert "missing routes" in str(exc)
    else:
        raise AssertionError("Expected RuntimeError for fail-fast route validation")


def test_run_stream_lifecycle_teardown_is_safe_and_idempotent(monkeypatch):
    stop_event = threading.Event()
    refresh_stop = threading.Event()

    class _Runner:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    class _Persister:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    class _HealthServer:
        def __init__(self):
            self.shutdown_calls = 0
            self.server_close_calls = 0

        def shutdown(self):
            self.shutdown_calls += 1

        def server_close(self):
            self.server_close_calls += 1

    health_server = _HealthServer()
    monkeypatch.setenv("SKIP_HEALTH_SERVER", "0")
    monkeypatch.setattr(mis, "_start_health_server", lambda _port: health_server)

    def _refresh_loop():
        while not refresh_stop.wait(0.01):
            pass

    def _position_loop():
        return

    def _run_socket():
        stop_event.set()

    runner = _Runner()
    persister = _Persister()

    mis._run_stream_lifecycle(
        stop_event=stop_event,
        refresh_stop=refresh_stop,
        start_health_server=True,
        refresh_atm_loop=_refresh_loop,
        position_sync_loop=_position_loop,
        position_sync_interval=0,
        run_socket=_run_socket,
        runner=runner,
        persister=persister,
    )

    assert runner.close_calls == 1
    assert persister.close_calls == 1
    assert health_server.shutdown_calls == 1
    assert health_server.server_close_calls == 1


class _SeedEngine:
    def __init__(self, timeframes):
        self.timeframes = list(timeframes)
        self.seeded_bars = []

    def seed_from_closed_bars(self, bars):
        self.seeded_bars = list(bars)
        return len(self.seeded_bars)


class _DummyCursor:
    def __init__(self, rows, calls):
        self._rows = rows
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, query, params):
        self._calls.append((query, params))
        return self

    def fetchall(self):
        return list(self._rows)


class _DummyConn:
    def __init__(self, rows):
        self._rows = rows
        self.calls = []
        self.closed = False

    def cursor(self):
        return _DummyCursor(self._rows, self.calls)

    def close(self):
        self.closed = True


def test_seed_indicators_from_postgres_reads_rows_and_seeds_engine(monkeypatch):
    ts0 = datetime(2026, 3, 4, 12, 0, tzinfo=timezone.utc)
    rows = [
        (ts0, None, "NIFTY_IDX", 30, 100.0, 101.0, 99.5, 100.5),
        (
            ts0 + timedelta(seconds=30),
            ts0 + timedelta(seconds=60),
            "NIFTY_IDX",
            30,
            100.5,
            101.2,
            100.1,
            101.0,
        ),
        # This row should be ignored because timeframe isn't in engine.timeframes.
        (ts0, ts0 + timedelta(seconds=300), "NIFTY_IDX", 300, 1, 1, 1, 1),
    ]
    conn = _DummyConn(rows)

    def _fake_connect_with_retry(dsn, **kwargs):
        assert dsn == "postgresql://user:pass@localhost:5432/testdb"
        return conn

    monkeypatch.setattr(mis, "connect_with_retry", _fake_connect_with_retry)
    engine = _SeedEngine([30])

    seeded = mis._seed_indicators_from_postgres(
        engine,
        "postgresql://user:pass@localhost:5432/testdb",
        "indicator_bars",
        allowed_labels={"NIFTY_IDX"},
        max_bars_per_series=300,
    )

    assert seeded == 2
    assert len(engine.seeded_bars) == 2
    assert engine.seeded_bars[0]["label"] == "NIFTY_IDX"
    assert engine.seeded_bars[0]["timeframe_seconds"] == 30
    # ts_end should be synthesized when source row has NULL ts_end.
    assert engine.seeded_bars[0]["ts_end"] == ts0 + timedelta(seconds=30)
    assert conn.closed is True
    assert conn.calls, "expected one SELECT call"
    query, params = conn.calls[0]
    assert '"indicator_bars"' in query
    assert params[-1] == 300


def test_seed_indicators_from_postgres_rejects_invalid_table_name(monkeypatch):
    connect_calls = {"count": 0}

    def _fake_connect_with_retry(_dsn, **_kwargs):
        connect_calls["count"] += 1
        raise AssertionError("connect should not be called for invalid table names")

    monkeypatch.setattr(mis, "connect_with_retry", _fake_connect_with_retry)
    engine = _SeedEngine([30])

    seeded = mis._seed_indicators_from_postgres(
        engine,
        "postgresql://user:pass@localhost:5432/testdb",
        "indicator_bars;drop table indicator_bars;",
        allowed_labels={"NIFTY_IDX"},
        max_bars_per_series=100,
    )

    assert seeded == 0
    assert connect_calls["count"] == 0


def test_load_indicator_gap_bars_from_postgres_filters_label_time_window(monkeypatch):
    ts0 = datetime(2026, 3, 4, 12, 0, tzinfo=timezone.utc)
    rows = [
        (
            ts0 + timedelta(seconds=300),
            ts0 + timedelta(seconds=600),
            "NIFTY_IDX",
            300,
            100.0,
            101.0,
            99.5,
            100.5,
        ),
        (
            ts0 + timedelta(seconds=600),
            ts0 + timedelta(seconds=900),
            "NIFTY_IDX",
            300,
            100.5,
            101.2,
            100.1,
            101.0,
        ),
    ]
    conn = _DummyConn(rows)

    def _fake_connect_with_retry(dsn, **kwargs):
        assert dsn == "postgresql://user:pass@localhost:5432/testdb"
        return conn

    monkeypatch.setattr(mis, "connect_with_retry", _fake_connect_with_retry)

    bars = mis._load_indicator_gap_bars_from_postgres(
        "postgresql://user:pass@localhost:5432/testdb",
        "indicator_bars",
        label="NIFTY_IDX",
        timeframe_seconds=300,
        start_ts=ts0 + timedelta(seconds=300),
        end_ts=ts0 + timedelta(seconds=900),
    )

    assert len(bars) == 2
    assert bars[0]["ts_start"] == ts0 + timedelta(seconds=300)
    query, params = conn.calls[0]
    assert 'FROM "indicator_bars"' in query
    assert "ts_start >= %s" in query
    assert "ts_start < %s" in query
    assert params == (
        300,
        "NIFTY_IDX",
        ts0 + timedelta(seconds=300),
        ts0 + timedelta(seconds=900),
    )


def _gap_backfill_request(
    *,
    label: str = "NIFTY_IDX",
    timeframe_seconds: int = 300,
) -> GapBackfillRequest:
    return GapBackfillRequest(
        label=label,
        timeframe_seconds=timeframe_seconds,
        reason="stale_gap",
        current_candle=Candle(
            start_ts=datetime(2026, 3, 4, 4, 0, tzinfo=timezone.utc),
            end_ts=datetime(2026, 3, 4, 4, 5, tzinfo=timezone.utc),
            o=100.0,
            h=101.0,
            low=99.5,
            c=100.5,
        ),
        gap_start_ts=datetime(2026, 3, 4, 4, 5, tzinfo=timezone.utc),
        next_bucket_start_ts=datetime(2026, 3, 4, 4, 15, tzinfo=timezone.utc),
        gap_seconds=900.0,
        max_expected_gap_seconds=900.0,
    )


def test_load_indicator_gap_bars_from_broker_normalizes_exchange_and_filters_window():
    request = _gap_backfill_request()

    class _StubOrderClient:
        def __init__(self):
            self.calls = []

        def fetch_historical_candles(self, **kwargs):
            self.calls.append(kwargs)
            return [
                ["2026-03-04 09:35", 100.0, 101.0, 99.5, 100.5, 1000],
                ["2026-03-04 09:40", 100.5, 101.2, 100.1, 101.0, 1000],
                ["2026-03-04 09:45", 101.0, 101.4, 100.8, 101.2, 1000],
            ]

    client = _StubOrderClient()
    bars = mis._load_indicator_gap_bars_from_broker(
        client,
        {
            "NIFTY_IDX": {
                "kind": "UNDERLYING",
                "exchange": "NSE_CM",
                "token": "99926000",
            }
        },
        request,
    )

    assert len(bars) == 2
    assert bars[0]["ts_start"] == datetime(2026, 3, 4, 4, 5, tzinfo=timezone.utc)
    assert bars[1]["ts_start"] == datetime(2026, 3, 4, 4, 10, tzinfo=timezone.utc)
    assert client.calls[0]["exchange"] == "NSE"
    assert client.calls[0]["interval"] == "FIVE_MINUTE"


def test_resolve_indicator_gap_backfill_merges_broker_data_and_clears_blocks(monkeypatch):
    request = _gap_backfill_request()
    maintenance = MaintenanceManager()
    maintenance.block_new_entries("NIFTY_IDX", reason="indicator_gap_data_stale:300")

    postgres_bars = [
        {
            "ts_start": request.gap_start_ts,
            "ts_end": request.gap_start_ts + timedelta(seconds=300),
            "label": "NIFTY_IDX",
            "timeframe_seconds": 300,
            "o": 100.0,
            "h": 101.0,
            "l": 99.5,
            "c": 100.5,
        }
    ]
    broker_bars = [
        {
            "ts_start": request.gap_start_ts + timedelta(seconds=300),
            "ts_end": request.gap_start_ts + timedelta(seconds=600),
            "label": "NIFTY_IDX",
            "timeframe_seconds": 300,
            "o": 100.5,
            "h": 101.2,
            "l": 100.1,
            "c": 101.0,
        }
    ]

    monkeypatch.setattr(mis, "_load_indicator_gap_bars_from_postgres", lambda *args, **kwargs: postgres_bars)
    monkeypatch.setattr(mis, "_load_indicator_gap_bars_from_broker", lambda *args, **kwargs: broker_bars)

    bars = mis._resolve_indicator_gap_backfill(
        request,
        instrument_meta={"NIFTY_IDX": {"kind": "UNDERLYING"}},
        label_to_underlying={"NIFTY_IDX": "NIFTY_IDX"},
        maintenance=maintenance,
        postgres_dsn="postgresql://user:pass@localhost:5432/testdb",
        postgres_table="indicator_bars",
        order_client=object(),
        broker_backfill_enabled=True,
    )

    assert len(bars) == 2
    assert maintenance.entry_block_reasons("NIFTY_IDX") == set()


def test_resolve_indicator_gap_backfill_blocks_entries_when_gap_stays_uncovered(monkeypatch):
    request = _gap_backfill_request()
    maintenance = MaintenanceManager()

    monkeypatch.setattr(mis, "_load_indicator_gap_bars_from_postgres", lambda *args, **kwargs: [])
    monkeypatch.setattr(mis, "_load_indicator_gap_bars_from_broker", lambda *args, **kwargs: [])

    bars = mis._resolve_indicator_gap_backfill(
        request,
        instrument_meta={"NIFTY_IDX": {"kind": "UNDERLYING"}},
        label_to_underlying={"NIFTY_IDX": "NIFTY_IDX"},
        maintenance=maintenance,
        postgres_dsn="postgresql://user:pass@localhost:5432/testdb",
        postgres_table="indicator_bars",
        order_client=object(),
        broker_backfill_enabled=True,
    )

    assert bars == []
    assert maintenance.entry_block_reasons("NIFTY_IDX") == {
        "indicator_gap_data_stale:300"
    }


def test_replay_indicator_seed_bars_rebuilds_market_context_from_seed_history():
    ts0 = datetime(2026, 3, 6, 12, 0, tzinfo=timezone.utc)
    price = 100.0
    bars = []
    for idx in range(70):
        close = price + 0.4
        bars.append(
            {
                "ts_start": ts0 + timedelta(seconds=300 * idx),
                "ts_end": ts0 + timedelta(seconds=300 * (idx + 1)),
                "label": "NG_FUT",
                "timeframe_seconds": 300,
                "o": price,
                "h": close + 0.2,
                "l": price - 0.2,
                "c": close,
            }
        )
        price = close

    builder = mis.MarketContextBuilder(ema_period=8)
    contexts = []

    replayed = mis._replay_indicator_seed_bars(
        seed_bars=list(reversed(bars)),
        timeframes=[300],
        ema_periods_by_timeframe={300: [8]},
        allowed_labels={"NG_FUT"},
        on_bar=lambda _label, _tf, candle, indicators: contexts.append(
            builder.build(candle=candle, indicators=indicators)
        ),
    )

    assert replayed == len(bars)
    assert len(contexts) == len(bars)
    assert any(ctx.atr_norm is not None for ctx in contexts)
    assert any(ctx.ema_slope is not None for ctx in contexts)
    assert contexts[-1].atr_norm is not None
    assert contexts[-1].ema_slope is not None


def test_build_indicator_engine_config_adds_put_momentum_ema_requirements(monkeypatch):
    monkeypatch.delenv("EMA_5M_PERIODS", raising=False)

    ema_periods_by_tf, engine_timeframes = mis._build_indicator_engine_config(
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

    assert 300 in engine_timeframes
    assert 900 in engine_timeframes
    assert ema_periods_by_tf[300] == [20, 50]
    assert ema_periods_by_tf[900] == [20]


def test_seeded_900s_history_computes_ema20_when_timeframe_is_configured():
    engine = MultiInstrumentIndicatorEngine(
        timeframes=[900],
        ema_periods_by_timeframe={900: [20]},
        max_candles=100,
    )
    ts0 = datetime(2026, 3, 9, 3, 45, tzinfo=timezone.utc)
    bars = []
    price = 23800.0
    for idx in range(25):
        close = price + 5.0
        bars.append(
            {
                "ts_start": ts0 + timedelta(seconds=900 * idx),
                "ts_end": ts0 + timedelta(seconds=900 * (idx + 1)),
                "label": "NIFTY_IDX",
                "timeframe_seconds": 900,
                "o": price,
                "h": close + 2.0,
                "l": price - 2.0,
                "c": close,
            }
        )
        price = close

    seeded = engine.seed_from_closed_bars(bars)
    latest = engine.get_latest_indicators("NIFTY_IDX", 900)

    assert seeded == len(bars)
    assert latest is not None
    assert latest["ema_20"] is not None


def test_prune_indicator_state_for_active_labels_skips_empty_active_set():
    engine = MultiInstrumentIndicatorEngine(
        timeframes=[300],
        ema_periods_by_timeframe={300: [20]},
        max_candles=20,
    )
    ts0 = datetime(2026, 3, 10, 3, 45, tzinfo=timezone.utc)

    seeded = engine.seed_from_closed_bars(
        [
            {
                "ts_start": ts0,
                "ts_end": ts0 + timedelta(seconds=300),
                "label": "NIFTY_IDX",
                "timeframe_seconds": 300,
                "o": 100.0,
                "h": 101.0,
                "l": 99.0,
                "c": 100.5,
            }
        ]
    )

    assert seeded == 1
    assert "NIFTY_IDX" in engine.states

    removed = mis._prune_indicator_state_for_active_labels(engine, [])

    assert removed == (0, 0)
    assert "NIFTY_IDX" in engine.states


def test_refresh_strategy_instances_marks_degraded_in_legacy_mode():
    """When reconcile_open_legs is disabled, open_legs are preserved (not cleared)
    and the strategy is marked DEGRADED per Architecture §13 critical ATM rule."""
    class _Strategy:
        def __init__(self) -> None:
            self.open_legs = {"OLD_LABEL": object()}
            self._degraded = False
            self.refresh_atm_calls = 0
            self.refresh_meta_calls = 0
            self.reconcile_calls = 0

        def refresh_atm_labels(self, instrument_meta):
            self.refresh_atm_calls += 1
            self.instrument_meta = instrument_meta

        def refresh_meta(self, instrument_meta):
            self.refresh_meta_calls += 1
            self.instrument_meta = instrument_meta

        def reconcile_open_legs(self, instrument_meta, *, previous_instrument_meta=None):
            self.reconcile_calls += 1
            return {"open_legs_before": 1, "open_legs_after": 1, "rebound": 1}

    inst = _Strategy()
    summary = mis._refresh_strategy_instances_after_atm_update(
        [{"instance": inst}],
        {"NEW_LABEL": {"token": "1"}},
        previous_instrument_meta={"OLD_LABEL": {"token": "1"}},
        reconcile_open_legs=False,
    )

    assert inst.refresh_atm_calls == 1
    assert inst.refresh_meta_calls == 1
    assert inst.reconcile_calls == 0
    # Architecture: open_legs must NOT be cleared; strategy enters DEGRADED instead
    assert len(inst.open_legs) == 1  # preserved, not cleared
    assert inst._degraded is True
    assert summary["open_legs_before"] == 1
    assert summary["open_legs_after"] == 1
    assert summary["degraded"] == 1


def test_refresh_strategy_instances_preserves_open_legs_when_reconcile_flag_enabled():
    class _Strategy:
        def __init__(self) -> None:
            self.open_legs = {"OLD_LABEL": object()}
            self.reconcile_calls = 0

        def refresh_atm_labels(self, instrument_meta):
            self.instrument_meta = instrument_meta

        def reconcile_open_legs(self, instrument_meta, *, previous_instrument_meta=None):
            self.reconcile_calls += 1
            self.open_legs = {"NEW_LABEL": object()}
            return {
                "open_legs_before": 1,
                "open_legs_after": 1,
                "rebound": 1,
                "preserved": 0,
            }

    inst = _Strategy()
    summary = mis._refresh_strategy_instances_after_atm_update(
        [{"instance": inst}],
        {"NEW_LABEL": {"token": "1"}},
        previous_instrument_meta={"OLD_LABEL": {"token": "1"}},
        reconcile_open_legs=True,
    )

    assert inst.reconcile_calls == 1
    assert list(inst.open_legs.keys()) == ["NEW_LABEL"]
    assert summary["open_legs_before"] == 1
    assert summary["open_legs_after"] == 1
    assert summary["reconciled"] == 1


@pytest.mark.parametrize(
    ("reason", "row_underlying", "switch_enabled", "allowed_strategies", "selector_allowed", "strict_target", "strict_cutoff"),
    [
        ("strategy_switch_disabled", "NIFTY_IDX", False, ["ema20_strategy"], True, False, False),
        ("instrument_policy_blocked", "NIFTY_IDX", True, ["ce_orb"], True, False, False),
        ("selector_blocked", "NIFTY_IDX", True, ["ema20_strategy"], False, False, False),
        ("underlying_mismatch", "BANKNIFTY_IDX", True, ["ema20_strategy"], True, False, False),
        ("strict_intraday_cutoff_blocked", "NIFTY_IDX", True, ["ema20_strategy"], True, True, True),
    ],
)
def test_should_dispatch_strategy_bar_logs_skip_reasons(
    caplog,
    reason,
    row_underlying,
    switch_enabled,
    allowed_strategies,
    selector_allowed,
    strict_target,
    strict_cutoff,
):
    switch = mis.StrategySwitchboard({"ema20_strategy": switch_enabled})
    controller = InstrumentController(
        {
            "NIFTY_IDX": {
                "enabled": True,
                "allowed_strategies": allowed_strategies,
            }
        }
    )
    strategy_row = {
        "name": "ema20_strategy",
        "underlying": row_underlying,
        "instance": SimpleNamespace(_strategy_id="ema20-trend"),
    }
    candle = SimpleNamespace(
        start_ts=datetime(2026, 3, 13, 4, 0, tzinfo=timezone.utc),
    )

    with caplog.at_level(logging.INFO):
        allowed = mis._should_dispatch_strategy_bar(
            bar_label="NIFTY_IDX",
            underlying_label="NIFTY_IDX",
            timeframe_seconds=300,
            candle=candle,
            bar_ts=datetime(2026, 3, 13, 10, 0, tzinfo=timezone.utc),
            strategy_row=strategy_row,
            strategy_switch=switch,
            instrument_controller=controller,
            is_strategy_selected=lambda *_args: selector_allowed,
            is_strict_target_strategy=lambda _name: strict_target,
            strict_cutoff_reached=lambda _ts: strict_cutoff,
        )

    assert allowed is False
    text = "\n".join(rec.message for rec in caplog.records)
    assert "event_type=STRATEGY_BAR_SKIP" in text
    assert f"reason={reason}" in text
    assert "underlying=NIFTY_IDX" in text
    assert "strategy=ema20_strategy" in text


def test_build_strategy_runtime_startup_snapshot_redacts_sensitive_env_and_persists(tmp_path):
    snapshot = mis._build_strategy_runtime_startup_snapshot(
        process_start_ts=datetime(2026, 3, 14, 3, 30, tzinfo=timezone.utc),
        trade_mode="LIVE",
        strategy_cfg={"strategies": [{"name": "ema20_strategy"}]},
        env={
            "TRADE_MODE": "LIVE",
            "NIFTY_EMA20_LOTS": "1",
            "ADMIN_API_KEY": "super-secret",
        },
        strategies=[
            {
                "name": "ema20_strategy",
                "underlying": "NIFTY_IDX",
                "instance": SimpleNamespace(_strategy_id="ema20-trend"),
            }
        ],
        instrument_policy_snapshot={
            "NIFTY_IDX": {
                "enabled": True,
                "allowed_strategies": ["ema20_strategy"],
            }
        },
        strategy_switch_snapshot={"ema20_strategy": True},
        selector_config=SimpleNamespace(
            enabled=True,
            max_active_per_underlying=2,
            min_hold_seconds=180.0,
            ema20_is_authoritative=True,
            ema20_authoritative_underlyings=("NIFTY_IDX",),
            mapping={"NIFTY_IDX": {}},
        ),
        selector_state={
            "NIFTY_IDX": {
                "underlying": "NIFTY_IDX",
                "regime": "NORMAL",
                "selected_strategies": ["ema20_strategy"],
            }
        },
        strict_intraday_state={"enabled": False, "target_strategies": []},
        indicator_plan_snapshot={
            "engine_timeframes": [300, 900],
            "ema_periods_by_timeframe": {"300": [20, 50], "900": [20]},
            "adx_period": 14,
        },
        indicator_seed_summary={
            "seed_source": "csv",
            "requested_bars_per_series": 500,
            "seeded_bars": 240,
            "seeded_series_count": 2,
            "per_series": [
                {"label": "NIFTY_IDX", "timeframe_seconds": 300, "bars": 120},
                {"label": "NIFTY_IDX", "timeframe_seconds": 900, "bars": 120},
            ],
        },
    )

    assert snapshot["env_overrides"]["ADMIN_API_KEY"] == "***REDACTED***"
    assert snapshot["env_overrides"]["NIFTY_EMA20_LOTS"] == "1"
    assert snapshot["attached_strategies_by_underlying"]["NIFTY_IDX"][0]["name"] == "ema20_strategy"
    assert snapshot["config_hash"]
    assert snapshot["indicator_plan"]["engine_timeframes"] == [300, 900]
    assert snapshot["indicator_plan"]["ema_periods_by_timeframe"]["300"] == [20, 50]
    assert snapshot["indicator_seed_summary"]["seed_source"] == "csv"
    assert snapshot["indicator_seed_summary"]["per_series"][0]["label"] == "NIFTY_IDX"
    assert snapshot["selector"]["ema20_authoritative_underlyings"] == ["NIFTY_IDX"]

    snapshot_path = mis._persist_strategy_runtime_startup_snapshot(
        snapshot=snapshot,
        logs_dir=tmp_path,
    )

    persisted = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert persisted["config_hash"] == snapshot["config_hash"]
    assert snapshot_path.name.startswith("strategy_runtime_startup_snapshot_")


def test_validate_required_strategy_attachments_live_raises_for_missing_enabled_attachment():
    with pytest.raises(RuntimeError, match="NIFTY_IDX:ema20_strategy"):
        mis._validate_required_strategy_attachments(
            trade_mode="LIVE",
            strategies=[
                {
                    "name": "ce_orb",
                    "underlying": "NIFTY_IDX",
                    "instance": SimpleNamespace(_strategy_id="ce-orb"),
                }
            ],
            instrument_policy_snapshot={
                "NIFTY_IDX": {
                    "enabled": True,
                    "allowed_strategies": ["ema20_strategy", "ce_orb"],
                }
            },
            enabled_strategy_names={"ema20_strategy", "ce_orb"},
        )


def test_validate_required_strategy_attachments_allows_duplicate_strategy_rows(caplog):
    with caplog.at_level(logging.WARNING):
        missing = mis._validate_required_strategy_attachments(
            trade_mode="PAPER",
            strategies=[
                {
                    "name": "ema20_strategy",
                    "underlying": "NIFTY_IDX",
                    "instance": SimpleNamespace(_strategy_id="ema20-1"),
                },
                {
                    "name": "ema20_strategy",
                    "underlying": "NIFTY_IDX",
                    "instance": SimpleNamespace(_strategy_id="ema20-2"),
                },
            ],
            instrument_policy_snapshot={
                "NIFTY_IDX": {
                    "enabled": True,
                    "allowed_strategies": ["ema20_strategy"],
                }
            },
            enabled_strategy_names={"ema20_strategy"},
        )

    assert missing == []
    assert caplog.records == []
