from datetime import datetime, timedelta, timezone
import importlib

from app.core.dashboard_bus import DashboardBus


def test_import_module_succeeds():
    mod = importlib.import_module("app.core.dashboard_bus")
    assert hasattr(mod, "DashboardBus")


def test_dashboard_bus_tracks_ticks_positions_and_trades():
    bus = DashboardBus()
    bus.set_instrument_meta({"ABC": {"lot_size": 2}})

    ts_open = datetime(2024, 1, 1, 4, 0, tzinfo=timezone.utc)
    bus.record_tick("ABC", 100.0, ts_open)
    bus.record_bar("ABC", 60, candle=None, indicators={"atr": 1.0, "rsi": 55, "macd": 0.2})

    bus.add_position(
        "ABC",
        side="BUY",
        qty=1,
        entry_price=100.0,
        lot_size=2,
        template_name="tpl",
        role=None,
        underlying="ABC",
        strategy_name="strat",
        reason="signal",
        entry_ts=ts_open.isoformat(),
    )

    ts_later = ts_open + timedelta(minutes=5)
    bus.record_tick("ABC", 110.0, ts_later)

    snap = bus.snapshot()
    instrument = snap["instruments"][0]
    assert instrument["label"] == "ABC"
    assert instrument["price"] == 110.0
    assert instrument["change_pct"] == 10.0  # from 100 -> 110
    assert snap["pnl"]["open"] == 20.0  # (110-100)*qty*lot_size
    assert snap["trades"]["open_positions"][0]["pnl"] == 20.0

    bus.close_position("ABC", exit_price=120.0, realized_pnl=40.0)
    snap_after = bus.snapshot()
    assert snap_after["trades"]["open_positions"] == []
    assert snap_after["pnl"]["realized"] == 40.0
    assert snap_after["trades"]["recent"][-1]["exit_price"] == 120.0


def test_dashboard_bus_uses_explicit_lot_size_when_meta_is_gone():
    bus = DashboardBus()
    bus.set_instrument_meta({"ABC": {"lot_size": 2, "symbol": "ABC-EQ", "token": "101"}})

    ts_open = datetime(2024, 1, 1, 4, 0, tzinfo=timezone.utc)
    bus.record_tick("ABC", 100.0, ts_open)
    bus.add_position(
        "ABC",
        side="SELL",
        qty=1,
        entry_price=100.0,
        lot_size=25,
        template_name="tpl",
        role=None,
        underlying="ABC",
        strategy_name="strat",
        reason="signal",
        entry_ts=ts_open.isoformat(),
    )

    bus.set_instrument_meta({})
    bus.record_tick("ABC", 102.0, ts_open + timedelta(minutes=1))

    snap = bus.snapshot()
    assert snap["pnl"]["open"] == -50.0

    bus.close_position("ABC", exit_price=102.0, realized_pnl=-50.0)
    snap_after = bus.snapshot()
    assert snap_after["pnl"]["realized"] == -50.0


def test_dashboard_bus_resolves_last_price_by_symbol():
    bus = DashboardBus()
    bus.set_instrument_meta({"ABC": {"symbol": "ABC-EQ", "token": "101"}})
    ts_open = datetime(2024, 1, 1, 4, 0, tzinfo=timezone.utc)
    bus.record_tick("ABC", 123.45, ts_open)

    assert bus.get_last_price_for_instrument(symbol="ABC-EQ") == 123.45
    assert bus.get_last_price_for_instrument(token="101") == 123.45


def test_dashboard_bus_upserts_instrument_meta_for_broker_only_symbol():
    bus = DashboardBus()
    bus.upsert_instrument_meta(
        "NATURALGAS24MAR26305CE",
        {
            "symbol": "NATURALGAS24MAR26305CE",
            "token": "505130",
            "lot_size": 1250,
            "exchange": "MCX",
        },
    )
    ts_open = datetime(2024, 1, 1, 4, 0, tzinfo=timezone.utc)
    bus.record_tick("NATURALGAS24MAR26305CE", 25.9, ts_open)

    assert bus.get_last_price_for_instrument(symbol="NATURALGAS24MAR26305CE") == 25.9
    assert bus.get_last_price_for_instrument(token="505130") == 25.9


def test_dashboard_bus_tracks_strategy_selection_snapshot():
    bus = DashboardBus()
    bus.upsert_strategy_selection(
        "nifty_idx",
        regime="TRENDING",
        selected_strategies=["ema20_strategy"],
        reason="mapping",
        hold_until="2026-03-05T10:00:00Z",
        updated_at="2026-03-05T09:30:00Z",
    )
    snapshot = bus.strategy_selection_snapshot()
    assert "NIFTY_IDX" in snapshot
    assert snapshot["NIFTY_IDX"]["regime"] == "TRENDING"
    assert snapshot["NIFTY_IDX"]["selected_strategies"] == ["ema20_strategy"]
    full_snapshot = bus.snapshot()
    assert full_snapshot["strategy_selection"][0]["underlying"] == "NIFTY_IDX"


def test_dashboard_bus_delta_snapshot_uses_deltas_and_periodic_compaction():
    bus = DashboardBus()
    ts_open = datetime(2024, 1, 1, 4, 0, tzinfo=timezone.utc)
    bus.set_instrument_meta({"ABC": {"symbol": "ABC", "token": "101", "lot_size": 1}})
    bus.record_tick("ABC", 100.0, ts_open)

    first = bus.delta_snapshot(full_interval=3)
    assert first["_mode"] == "full_snapshot"

    bus.record_tick("ABC", 101.0, ts_open + timedelta(minutes=1))
    second = bus.delta_snapshot(full_interval=3)
    assert second["_mode"] == "delta"
    assert "instruments" in second
    assert len(second) < len(first)

    third = bus.delta_snapshot(full_interval=3)
    assert third["_mode"] == "full_snapshot"
    assert "instruments" in third
