import importlib
import types
from datetime import datetime, timedelta, timezone
import pytest

from app.brokers.base import OrderResponse

MODULE_PATH = "app.strategies.ema20_strategy"
CALLABLE_NAMES = ['Ema20Strategy']


def test_import_module_succeeds():
    mod = importlib.import_module(MODULE_PATH)
    assert isinstance(mod, types.ModuleType)


@pytest.mark.parametrize("name", CALLABLE_NAMES)
def test_module_defines_callable(name):
    mod = importlib.import_module(MODULE_PATH)
    assert hasattr(mod, name), f"Module {MODULE_PATH} should define {name}"


@pytest.mark.parametrize("name", CALLABLE_NAMES)
def test_callable_behaviour_placeholder(name):
    mod = importlib.import_module(MODULE_PATH)
    obj = getattr(mod, name)
    assert obj is not None


def _make_candle(ts: datetime, close: float):
    return types.SimpleNamespace(start_ts=ts, c=close)


def _make_strategy(monkeypatch, *, require_rsi_falling: str = "1", **kwargs):
    monkeypatch.setenv("NG_EMA20_REQUIRE_RSI_FALLING", require_rsi_falling)
    mod = importlib.import_module(MODULE_PATH)
    instrument_meta = {
        "NG_FUT": {"symbol": "NG_FUT"},
        "NG_ATM_CE_100": {
            "symbol": "NATURALGAS20FEB26100CE",
            "token": "123",
            "lot_size": 1,
            "exchange": "MCX",
        },
    }
    return mod, mod.Ema20Strategy(
        instrument_meta,
        env_prefix="NG_",
        underlying_label="NG_FUT",
        signal_timeframe=300,
        ema_period=20,
        **kwargs,
    )


def _make_restored_risk_manager(
    *,
    label: str = "NG_ATM_CE_100",
    entry_price: float = 100.0,
    qty: int = 1,
    strategy_name: str = "ema20-trend",
    tp_pct: float = 0.10,
    sl_pct: float = 0.15,
    trail_buffer_pct: float = 0.10,
    trail_trigger_pct: float = 0.20,
):
    entry_ts = datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc).isoformat()
    return types.SimpleNamespace(
        restored_positions={
            label: {
                "side": "SELL",
                "qty": qty,
                "entry_price": entry_price,
                "template_name": strategy_name,
                "strategy_name": strategy_name,
                "underlying": "NG",
                "entry_ts": entry_ts,
                "exchange": "MCX",
                "symboltoken": "123",
                "tradingsymbol": "NATURALGAS20FEB26100CE",
                "lot_size": 1,
                "strategy_context": {
                    "sl_pct": sl_pct,
                    "tp_pct": tp_pct,
                    "trail_buffer_pct": trail_buffer_pct,
                    "trail_trigger_pct": trail_trigger_pct,
                    "managed_exit_mode": "strategy",
                    "broker_brackets_enabled": False,
                },
            }
        }
    )


def _patch_bridge(monkeypatch, mod):
    calls = []

    def fake_place_order_via_bridge(**kwargs):
        calls.append(kwargs)
        return OrderResponse(
            broker_order_id="order-1",
            status="SUCCESS",
            message="ok",
        )

    monkeypatch.setattr(mod, "place_order_via_bridge", fake_place_order_via_bridge)
    return calls


def test_ema20_blocks_when_rsi_not_falling(monkeypatch):
    mod, strategy = _make_strategy(monkeypatch)
    calls = _patch_bridge(monkeypatch, mod)

    base_ts = datetime(2025, 1, 1, 3, 30, tzinfo=timezone.utc)
    for idx, rsi in enumerate([60.0, 58.0, 59.0]):
        candle = _make_candle(base_ts + timedelta(minutes=5 * idx), 90.0)
        strategy.on_bar(
            "NG_FUT",
            300,
            candle,
            {"ema_20": 100.0, "atr": 1.0, "rsi": rsi},
        )

    assert calls == []


def test_ema20_allows_when_rsi_falling(monkeypatch):
    mod, strategy = _make_strategy(monkeypatch)
    calls = _patch_bridge(monkeypatch, mod)

    base_ts = datetime(2025, 1, 1, 3, 30, tzinfo=timezone.utc)
    for idx, rsi in enumerate([60.0, 59.0, 58.0]):
        candle = _make_candle(base_ts + timedelta(minutes=5 * idx), 90.0)
        strategy.on_bar(
            "NG_FUT",
            300,
            candle,
            {"ema_20": 100.0, "atr": 1.0, "rsi": rsi},
        )

    assert len(calls) == 1


def test_ema20_adx_filter_blocks_when_indicator_values_missing(monkeypatch):
    mod, strategy = _make_strategy(
        monkeypatch,
        require_rsi_falling="0",
        use_adx_filter=True,
    )
    calls = _patch_bridge(monkeypatch, mod)
    candle = _make_candle(datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc), 90.0)
    strategy.on_bar(
        "NG_FUT",
        300,
        candle,
        {"ema_20": 100.0, "atr": 1.0, "rsi": 50.0},
    )
    assert calls == []


def test_ema20_adx_filter_blocks_when_adx_is_low(monkeypatch):
    mod, strategy = _make_strategy(
        monkeypatch,
        require_rsi_falling="0",
        use_adx_filter=True,
        min_adx=25.0,
    )
    calls = _patch_bridge(monkeypatch, mod)
    candle = _make_candle(datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc), 90.0)
    strategy.on_bar(
        "NG_FUT",
        300,
        candle,
        {
            "ema_20": 100.0,
            "atr": 1.0,
            "rsi": 50.0,
            "adx": 18.0,
            "plus_di": 20.0,
            "minus_di": 35.0,
        },
    )
    assert calls == []


def test_ema20_adx_filter_blocks_when_direction_is_not_bearish(monkeypatch):
    mod, strategy = _make_strategy(
        monkeypatch,
        require_rsi_falling="0",
        use_adx_filter=True,
        min_adx=18.0,
        require_bearish_di=True,
    )
    calls = _patch_bridge(monkeypatch, mod)
    candle = _make_candle(datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc), 90.0)
    strategy.on_bar(
        "NG_FUT",
        300,
        candle,
        {
            "ema_20": 100.0,
            "atr": 1.0,
            "rsi": 50.0,
            "adx": 30.0,
            "plus_di": 31.0,
            "minus_di": 25.0,
        },
    )
    assert calls == []


def test_ema20_adx_filter_blocks_when_di_spread_too_low(monkeypatch):
    mod, strategy = _make_strategy(
        monkeypatch,
        require_rsi_falling="0",
        use_adx_filter=True,
        min_adx=18.0,
        require_bearish_di=True,
        min_di_spread=8.0,
    )
    calls = _patch_bridge(monkeypatch, mod)
    candle = _make_candle(datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc), 90.0)
    strategy.on_bar(
        "NG_FUT",
        300,
        candle,
        {
            "ema_20": 100.0,
            "atr": 1.0,
            "rsi": 50.0,
            "adx": 30.0,
            "plus_di": 21.0,
            "minus_di": 25.0,
        },
    )
    assert calls == []


def test_ema20_adx_filter_allows_when_bearish_trend_is_strong(monkeypatch):
    mod, strategy = _make_strategy(
        monkeypatch,
        require_rsi_falling="0",
        use_adx_filter=True,
        min_adx=18.0,
        require_bearish_di=True,
        min_di_spread=3.0,
    )
    calls = _patch_bridge(monkeypatch, mod)
    candle = _make_candle(datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc), 90.0)
    strategy.on_bar(
        "NG_FUT",
        300,
        candle,
        {
            "ema_20": 100.0,
            "atr": 1.0,
            "rsi": 50.0,
            "adx": 30.0,
            "plus_di": 18.0,
            "minus_di": 27.0,
        },
    )
    assert len(calls) == 1


def test_ema20_blocks_new_entries_after_square_off(monkeypatch):
    mod, strategy = _make_strategy(
        monkeypatch,
        require_rsi_falling="0",
        square_off_time="15:20",
    )
    calls = _patch_bridge(monkeypatch, mod)

    # 15:30 IST
    candle = _make_candle(datetime(2025, 1, 1, 10, 0, tzinfo=timezone.utc), 90.0)
    strategy.on_bar(
        "NG_FUT",
        300,
        candle,
        {"ema_20": 100.0, "atr": 1.0, "rsi": 50.0},
    )

    assert calls == []
    assert strategy.position is None


def test_ema20_forces_exit_at_or_after_square_off(monkeypatch):
    mod, strategy = _make_strategy(
        monkeypatch,
        require_rsi_falling="0",
        square_off_time="15:20",
    )
    calls = _patch_bridge(monkeypatch, mod)

    # 15:10 IST, should allow entry.
    pre_cutoff = _make_candle(datetime(2025, 1, 1, 9, 40, tzinfo=timezone.utc), 90.0)
    strategy.on_bar(
        "NG_FUT",
        300,
        pre_cutoff,
        {"ema_20": 100.0, "atr": 1.0, "rsi": 50.0},
    )
    assert strategy.position is not None
    assert len(calls) == 1

    # 15:30 IST, should force exit without taking new entry.
    post_cutoff = _make_candle(datetime(2025, 1, 1, 10, 0, tzinfo=timezone.utc), 90.0)
    strategy.on_bar(
        "NG_FUT",
        300,
        post_cutoff,
        {"ema_20": 100.0, "atr": 1.0, "rsi": 50.0},
    )

    assert strategy.position is None
    assert len(calls) == 2
    assert calls[1]["order_req"].tag == "EMA20_EXIT_EOD"


def test_ema20_time_guards_without_candle_use_strategy_clock(monkeypatch):
    _mod, strategy = _make_strategy(
        monkeypatch,
        require_rsi_falling="0",
        first_entry_time="09:30",
        square_off_time="15:20",
    )
    calls = {"n": 0}

    def _fake_now_ist() -> datetime:
        calls["n"] += 1
        return datetime(2025, 1, 1, 9, 0, tzinfo=_mod.IST)

    strategy._now_ist = _fake_now_ist  # type: ignore[method-assign]

    assert strategy._is_before_first_entry() is True
    assert strategy._is_after_square_off() is False
    assert calls["n"] >= 2


def test_ema20_on_tick_square_off_uses_strategy_clock(monkeypatch):
    mod, strategy = _make_strategy(
        monkeypatch,
        require_rsi_falling="0",
        square_off_time="15:20",
    )
    calls = _patch_bridge(monkeypatch, mod)

    # 15:10 IST, should allow entry.
    pre_cutoff = _make_candle(datetime(2025, 1, 1, 9, 40, tzinfo=timezone.utc), 90.0)
    strategy.on_bar(
        "NG_FUT",
        300,
        pre_cutoff,
        {"ema_20": 100.0, "atr": 1.0, "rsi": 50.0},
    )
    assert strategy.position is not None
    assert len(calls) == 1

    # After entry, forbid any direct datetime.now usage inside on_tick time checks.
    class _ForbiddenDateTime:
        @classmethod
        def now(cls, *_args, **_kwargs):
            raise AssertionError("datetime.now should not be used in replayable time checks")

    monkeypatch.setattr(mod, "datetime", _ForbiddenDateTime)
    strategy._now_ist = (
        lambda: datetime(2025, 1, 1, 15, 0, tzinfo=mod.IST)
    )  # type: ignore[method-assign]
    strategy.on_tick("NG_FUT", 90.0)

    assert strategy.position is not None
    assert len(calls) == 1


def test_ema20_exit_uses_entry_broker_symbol_when_meta_symbol_missing(monkeypatch):
    mod, strategy = _make_strategy(monkeypatch, require_rsi_falling="0")
    calls = _patch_bridge(monkeypatch, mod)

    candle = _make_candle(datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc), 90.0)
    strategy.on_bar(
        "NG_FUT",
        300,
        candle,
        {"ema_20": 100.0, "atr": 1.0, "rsi": 50.0},
    )
    assert strategy.position is not None
    assert len(calls) == 1

    option_label = strategy.position.option_label
    entry_broker_symbol = calls[0]["order_req"].symbol
    assert entry_broker_symbol != option_label

    # Simulate stale metadata losing tradingsymbol at exit time.
    strategy.instrument_meta[option_label] = {
        "token": "123",
        "exchange": "MCX",
    }

    strategy._exit_position(reason="EOD")

    assert len(calls) == 2
    assert calls[1]["order_req"].symbol == entry_broker_symbol


def test_ema20_exit_circuit_breaker_caps_retry_attempts(monkeypatch, caplog):
    monkeypatch.setenv("NG_EMA20_EXIT_RETRY_COOLDOWN_SECONDS", "0.5")
    monkeypatch.setenv("NG_EMA20_EXIT_MAX_RETRIES", "2")
    monkeypatch.setenv("NG_EMA20_EXIT_CIRCUIT_OPEN_SECONDS", "30")
    mod, strategy = _make_strategy(monkeypatch, require_rsi_falling="0")

    clock = {"t": 1000.0}
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock["t"])

    calls = []

    def fake_place_order_via_bridge(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return OrderResponse(
                broker_order_id="entry-1",
                status="SUCCESS",
                message="ok",
            )
        raise RuntimeError("insufficient funds")

    monkeypatch.setattr(mod, "place_order_via_bridge", fake_place_order_via_bridge)

    candle = _make_candle(datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc), 90.0)
    strategy.on_bar(
        "NG_FUT",
        300,
        candle,
        {"ema_20": 100.0, "atr": 1.0, "rsi": 50.0},
    )
    assert strategy.position is not None
    assert len(calls) == 1

    with caplog.at_level("ERROR"):
        strategy._exit_position(reason="EOD")
        assert len(calls) == 2
        assert strategy._exit_failure_count == 1

        clock["t"] += 0.6
        strategy._exit_position(reason="EOD")
        assert len(calls) == 3
        assert strategy._exit_failure_count == 2
        assert strategy._exit_circuit_open_until_mono > clock["t"]

        clock["t"] += 0.1
        strategy._exit_position(reason="EOD")
        assert len(calls) == 3

    assert strategy.position is not None
    assert any("circuit opened" in rec.message.lower() for rec in caplog.records)


def test_ema20_blocks_entries_before_first_entry_time(monkeypatch):
    mod, strategy = _make_strategy(
        monkeypatch,
        require_rsi_falling="0",
        first_entry_time="09:30",
    )
    calls = _patch_bridge(monkeypatch, mod)

    # 09:25 IST
    candle = _make_candle(datetime(2025, 1, 1, 3, 55, tzinfo=timezone.utc), 90.0)
    strategy.on_bar(
        "NG_FUT",
        300,
        candle,
        {"ema_20": 100.0, "atr": 1.0, "rsi": 50.0},
    )

    assert calls == []
    assert strategy.position is None


def test_ema20_allows_entries_at_first_entry_time(monkeypatch):
    mod, strategy = _make_strategy(
        monkeypatch,
        require_rsi_falling="0",
        first_entry_time="09:30",
    )
    calls = _patch_bridge(monkeypatch, mod)

    # 09:30 IST
    candle = _make_candle(datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc), 90.0)
    strategy.on_bar(
        "NG_FUT",
        300,
        candle,
        {"ema_20": 100.0, "atr": 1.0, "rsi": 50.0},
    )

    assert len(calls) == 1
    assert strategy.position is not None


def test_ema20_warns_when_qty_equals_known_lot_size(monkeypatch, caplog):
    monkeypatch.setenv("NG_EMA20_LOTS", "1250")
    with caplog.at_level("WARNING"):
        _mod, _strategy = _make_strategy(monkeypatch, require_rsi_falling="0")
    assert any(
        "EMA20 lots may be misconfigured" in rec.message
        for rec in caplog.records
    )


def test_ema20_entry_uses_effective_lots_from_requested_broker_quantity(monkeypatch):
    monkeypatch.setenv("BANKNIFTY_EMA20_REQUIRE_RSI_FALLING", "0")
    monkeypatch.setenv("BANKNIFTY_EMA20_LOTS", "30")
    mod = importlib.import_module(MODULE_PATH)
    instrument_meta = {
        "BANKNIFTY_IDX": {"symbol": "BANKNIFTY_IDX"},
        "BANKNIFTY_ATM_CE_61000": {
            "symbol": "BANKNIFTY24FEB2661000CE",
            "token": "61000",
            "lot_size": 30,
            "exchange": "NFO",
        },
    }
    strategy = mod.Ema20Strategy(
        instrument_meta,
        env_prefix="BANKNIFTY_",
        underlying_label="BANKNIFTY_IDX",
        signal_timeframe=300,
        ema_period=20,
        min_atr=1.0,
        require_rsi_falling=False,
    )

    def fake_place_order_via_bridge(**_kwargs):
        return OrderResponse(
            broker_order_id="order-1",
            status="SUCCESS",
            message="ok",
            requested_quantity=60,  # 2 lots after router-side sizing
        )

    monkeypatch.setattr(mod, "place_order_via_bridge", fake_place_order_via_bridge)
    candle = _make_candle(datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc), 61000.0)
    strategy.on_bar(
        "BANKNIFTY_IDX",
        300,
        candle,
        {"ema_20": 61200.0, "atr": 5.0, "rsi": 50.0},
    )

    assert strategy.position is not None
    assert strategy.position.requested_lots == 30
    assert strategy.position.qty == 2
    assert strategy.position.broker_qty == 60
    assert strategy.position.lot_size == 30


def test_ema20_exit_prefers_open_quantity_from_state_store(monkeypatch):
    monkeypatch.setenv("BANKNIFTY_EMA20_REQUIRE_RSI_FALLING", "0")
    mod = importlib.import_module(MODULE_PATH)
    instrument_meta = {
        "BANKNIFTY_IDX": {"symbol": "BANKNIFTY_IDX"},
        "BANKNIFTY_ATM_CE_61000": {
            "symbol": "BANKNIFTY24FEB2661200CE",
            "token": "61000",
            "lot_size": 30,
            "exchange": "NFO",
        },
    }
    strategy = mod.Ema20Strategy(
        instrument_meta,
        env_prefix="BANKNIFTY_",
        underlying_label="BANKNIFTY_IDX",
        signal_timeframe=300,
        ema_period=20,
        min_atr=1.0,
        require_rsi_falling=False,
    )
    strategy.position = mod.Ema20Position(
        option_label="BANKNIFTY_ATM_CE_61000",
        broker_symbol="BANKNIFTY24FEB2661200CE",
        exchange="NFO",
        symbol_token="61000",
        qty=30,
        requested_lots=30,
        lot_size=30,
        broker_qty=900,
        entry_price=100.0,
        sl_price=110.0,
        tp_price=90.0,
        best_price=100.0,
        trail_active=False,
        entry_time=datetime.now(timezone.utc),
    )
    calls = []

    def fake_place_order_via_bridge(**kwargs):
        calls.append(kwargs)
        return OrderResponse(
            broker_order_id="exit-1",
            status="SUCCESS",
            message="ok",
        )

    runtime = types.SimpleNamespace(
        routing_table=types.SimpleNamespace(
            get_routes_for_strategy=lambda _sid: [
                types.SimpleNamespace(broker_account_id="A1")
            ]
        ),
        state_store=types.SimpleNamespace(
            get_positions=lambda _acct: [
                types.SimpleNamespace(
                    symbol="BANKNIFTY24FEB2661200CE",
                    quantity=-60,  # two lots currently open
                )
            ]
        ),
    )

    monkeypatch.setattr(mod, "place_order_via_bridge", fake_place_order_via_bridge)
    monkeypatch.setattr(mod, "_get_hub_runtime", lambda: runtime)

    strategy._exit_position(reason="EOD")

    assert len(calls) == 1


def test_ema20_entry_carries_live_strategy_context(monkeypatch):
    mod, strategy = _make_strategy(monkeypatch, require_rsi_falling="0")
    calls = _patch_bridge(monkeypatch, mod)

    candle = _make_candle(datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc), 90.0)
    strategy.on_bar(
        "NG_FUT",
        300,
        candle,
        {"ema_20": 100.0, "atr": 1.0, "rsi": 50.0},
    )

    assert len(calls) == 1
    order_req = calls[0]["order_req"]
    assert order_req.position_label == "NG_ATM_CE_100"
    assert order_req.strategy_context == {
        "sl_pct": pytest.approx(0.30),
        "tp_pct": pytest.approx(0.30),
        "trail_buffer_pct": pytest.approx(0.0),
        "trail_trigger_pct": None,
        "managed_exit_mode": "strategy",
        "broker_brackets_enabled": False,
    }


def test_ema20_exit_timeout_clears_local_position_when_broker_state_is_fresh_and_flat(
    monkeypatch,
):
    mod, strategy = _make_strategy(monkeypatch, require_rsi_falling="0")
    strategy.position = mod.Ema20Position(
        option_label="NG_ATM_CE_100",
        broker_symbol="NATURALGAS20FEB26100CE",
        exchange="MCX",
        symbol_token="123",
        qty=1,
        requested_lots=1,
        lot_size=1,
        broker_qty=1,
        entry_price=100.0,
        sl_price=110.0,
        tp_price=90.0,
        best_price=100.0,
        trail_active=False,
        entry_time=datetime.now(timezone.utc),
    )

    def _raise_timeout(**_kwargs):
        raise TimeoutError("timed out")

    runtime = types.SimpleNamespace(
        routing_table=types.SimpleNamespace(
            get_routes_for_strategy=lambda _sid: [
                types.SimpleNamespace(broker_account_id="A1")
            ]
        ),
        state_store=types.SimpleNamespace(
            get_positions_status=lambda _acct: {
                "status": "OK",
                "last_ok_ts": datetime.now(timezone.utc).isoformat(),
            },
            get_positions=lambda _acct: [],
        ),
    )

    monkeypatch.setattr(mod, "place_order_via_bridge", _raise_timeout)
    monkeypatch.setattr(mod, "_get_hub_runtime", lambda: runtime)

    strategy._exit_position(reason="TP")

    assert strategy.position is None
    assert strategy._exit_failure_count == 0
    assert strategy._next_exit_retry_mono == 0.0


def test_ema20_exit_treats_error_status_as_failed_retryable(monkeypatch):
    mod, strategy = _make_strategy(monkeypatch, require_rsi_falling="0")
    strategy.position = mod.Ema20Position(
        option_label="NG_ATM_CE_100",
        broker_symbol="NATURALGAS20FEB26100CE",
        exchange="MCX",
        symbol_token="123",
        qty=1,
        requested_lots=1,
        lot_size=1,
        broker_qty=1,
        entry_price=100.0,
        sl_price=110.0,
        tp_price=90.0,
        best_price=100.0,
        trail_active=False,
        entry_time=datetime.now(timezone.utc),
    )

    def _error_response(**_kwargs):
        return OrderResponse(
            broker_order_id="",
            status="ERROR",
            message="timed out",
        )

    monkeypatch.setattr(mod, "place_order_via_bridge", _error_response)

    strategy._exit_position(reason="TP")

    assert strategy.position is not None
    assert strategy._exit_failure_count == 1


def test_ema20_rehydrates_restored_position_and_exits_on_tp_after_restart(monkeypatch):
    risk_manager = _make_restored_risk_manager(entry_price=100.0)
    mod, strategy = _make_strategy(
        monkeypatch,
        require_rsi_falling="0",
        risk_manager=risk_manager,
        sl_pct=0.15,
        tp_pct=0.10,
        trail_buffer_pct=0.10,
        trail_trigger_pct=0.20,
    )
    calls = _patch_bridge(monkeypatch, mod)

    assert strategy.position is not None
    assert strategy.position.option_label == "NG_ATM_CE_100"
    assert strategy.position.entry_price == pytest.approx(100.0)
    assert strategy.position.sl_price == pytest.approx(115.0)
    assert strategy.position.tp_price == pytest.approx(90.0)

    strategy.on_tick("NG_ATM_CE_100", 89.0)

    assert strategy.position is None
    assert len(calls) == 1
    assert calls[0]["order_req"].tag == "EMA20_EXIT_TP"
    assert calls[0]["order_req"].symbol == "NATURALGAS20FEB26100CE"


def test_ema20_restore_ignores_broker_sync_markers_without_warning(monkeypatch, caplog):
    risk_manager = _make_restored_risk_manager(strategy_name="broker_sync")
    caplog.set_level("WARNING")

    _mod, strategy = _make_strategy(
        monkeypatch,
        require_rsi_falling="0",
        risk_manager=risk_manager,
    )

    assert strategy.position is None
    assert "Unknown strategy name 'broker_sync'" not in caplog.text


def test_ema20_rehydrated_position_trailing_activates_only_after_threshold(monkeypatch):
    risk_manager = _make_restored_risk_manager(entry_price=100.0, tp_pct=0.30)
    mod, strategy = _make_strategy(
        monkeypatch,
        require_rsi_falling="0",
        risk_manager=risk_manager,
        sl_pct=0.15,
        tp_pct=0.30,
        trail_buffer_pct=0.10,
        trail_trigger_pct=0.20,
    )
    calls = _patch_bridge(monkeypatch, mod)

    assert strategy.position is not None

    strategy.on_tick("NG_ATM_CE_100", 85.0)

    assert strategy.position is not None
    assert strategy.position.trail_active is False
    assert strategy.position.sl_price == pytest.approx(115.0)

    strategy.on_tick("NG_ATM_CE_100", 79.0)

    assert strategy.position is not None
    assert strategy.position.trail_active is True
    assert strategy.position.best_price == pytest.approx(79.0)
    assert strategy.position.sl_price == pytest.approx(86.9)

    strategy.on_tick("NG_ATM_CE_100", 82.0)

    assert strategy.position is not None
    assert strategy.position.best_price == pytest.approx(79.0)
    assert strategy.position.sl_price == pytest.approx(86.9)
    assert calls == []

    strategy.on_tick("NG_ATM_CE_100", 87.0)

    assert strategy.position is None
    assert len(calls) == 1
    assert calls[0]["order_req"].tag == "EMA20_EXIT_TRAIL"
