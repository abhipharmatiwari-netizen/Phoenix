import importlib
import json
import types
import pytest
import logging
from datetime import datetime, timezone, timedelta

MODULE_PATH = "app.core.maintenance"
CALLABLE_NAMES = ['MaintenanceManager']


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


def test_heartbeat_warning_throttles_per_symbol(monkeypatch, caplog):
    mod = importlib.import_module(MODULE_PATH)

    class _FrozenDatetime(datetime):
        current = datetime(2024, 1, 1, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return cls.current.replace(tzinfo=None)
            return cls.current

    monkeypatch.setattr(mod, "datetime", _FrozenDatetime)

    manager = mod.MaintenanceManager(
        heartbeat_timeout_seconds=5,
        feed_stale_log_throttle_seconds=60,
    )
    manager.last_tick_by_label["NIFTY_IDX"] = _FrozenDatetime.current - timedelta(
        seconds=10
    )

    caplog.set_level(logging.WARNING)
    manager.check_heartbeat()

    _FrozenDatetime.current += timedelta(seconds=10)
    manager.check_heartbeat()

    _FrozenDatetime.current += timedelta(seconds=61)
    manager.check_heartbeat()

    warnings = [rec.message for rec in caplog.records if rec.levelname == "WARNING"]
    assert len(warnings) == 2
    assert "suppressed 1 repeats" in warnings[-1]


def test_heartbeat_warning_can_suppress_specific_labels(monkeypatch, caplog):
    mod = importlib.import_module(MODULE_PATH)

    class _FrozenDatetime(datetime):
        current = datetime(2024, 1, 1, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return cls.current.replace(tzinfo=None)
            return cls.current

    monkeypatch.setattr(mod, "datetime", _FrozenDatetime)
    manager = mod.MaintenanceManager(
        heartbeat_timeout_seconds=5,
        feed_stale_log_throttle_seconds=0,
    )
    manager.last_tick_by_label["NIFTY_IDX"] = _FrozenDatetime.current - timedelta(
        seconds=20
    )
    manager.last_tick_by_label["BANKNIFTY_IDX"] = _FrozenDatetime.current - timedelta(
        seconds=20
    )

    caplog.set_level(logging.WARNING)
    manager.check_heartbeat(suppress_labels={"NIFTY_IDX"})
    warnings = [rec.message for rec in caplog.records if rec.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "BANKNIFTY_IDX" in warnings[0]


def test_explicit_entry_blocks_gate_new_entries_until_cleared():
    mod = importlib.import_module(MODULE_PATH)

    manager = mod.MaintenanceManager(
        heartbeat_timeout_seconds=5,
        feed_stale_log_throttle_seconds=0,
    )
    manager.block_new_entries(
        "nifty_idx",
        reason="indicator_gap_data_stale:300",
    )

    assert manager.entry_block_reasons("NIFTY_IDX") == {"indicator_gap_data_stale:300"}
    assert manager.allow_new_entries("NIFTY_IDX") is False

    manager.clear_entry_block(
        "NIFTY_IDX",
        reason="indicator_gap_data_stale:300",
    )

    assert manager.entry_block_reasons("NIFTY_IDX") == set()
    assert manager.allow_new_entries("NIFTY_IDX") is True


def test_trading_session_calendar_supports_warmup_and_holidays(tmp_path):
    mod = importlib.import_module(MODULE_PATH)
    holiday_path = tmp_path / "market_holidays.json"
    holiday_path.write_text(
        json.dumps({"holidays": {"NSE": ["2026-01-26"], "MCX": []}}),
        encoding="utf-8",
    )

    calendar = mod.TradingSessionCalendar(
        holiday_config_path=holiday_path,
        reload_check_interval_seconds=1,
    )

    warmup_ts = datetime(2026, 1, 27, 3, 40, tzinfo=timezone.utc)  # 09:10 IST
    open_ts = datetime(2026, 1, 27, 4, 0, tzinfo=timezone.utc)  # 09:30 IST
    holiday_ts = datetime(2026, 1, 26, 4, 0, tzinfo=timezone.utc)  # 09:30 IST

    assert calendar.session_phase({"exchange": "NSE"}, warmup_ts) == "warmup"
    assert calendar.is_session_active(
        {"exchange": "NSE"},
        warmup_ts,
        include_warmup=True,
    ) is True
    assert calendar.is_trading_open({"exchange": "NSE"}, warmup_ts) is False
    assert calendar.is_trading_open({"exchange": "NSE"}, open_ts) is True
    assert calendar.is_holiday({"exchange": "NSE"}, holiday_ts) is True


def test_trading_session_calendar_reloads_holiday_config_updates(tmp_path):
    mod = importlib.import_module(MODULE_PATH)
    holiday_path = tmp_path / "market_holidays.json"
    holiday_path.write_text(
        json.dumps({"holidays": {"NSE": ["2026-01-26"]}}),
        encoding="utf-8",
    )

    calendar = mod.TradingSessionCalendar(
        holiday_config_path=holiday_path,
        reload_check_interval_seconds=1,
    )
    ts = datetime(2026, 1, 26, 4, 0, tzinfo=timezone.utc)

    assert calendar.is_holiday({"exchange": "NSE"}, ts) is True

    holiday_path.write_text(
        json.dumps({"holidays": {"NSE": []}}),
        encoding="utf-8",
    )
    calendar.reload_holidays(force=True)

    assert calendar.is_holiday({"exchange": "NSE"}, ts) is False
