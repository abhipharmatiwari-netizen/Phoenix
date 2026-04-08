import importlib

from app.core.instrument_control import InstrumentController


def test_import_module_succeeds():
    mod = importlib.import_module("app.core.instrument_control")
    assert hasattr(mod, "InstrumentController")


def test_instrument_controller_enable_toggle_case_insensitive():
    ctrl = InstrumentController({"abc": {"enabled": False}})

    assert ctrl.is_enabled("ABC") is False
    ctrl.set_enabled("Abc", True)
    assert ctrl.is_enabled("abc") is True


def test_instrument_controller_allowed_strategies():
    ctrl = InstrumentController()
    # Empty allowed list means allow all
    assert ctrl.is_strategy_allowed("XYZ", "any") is True

    ctrl.update_allowed_strategies("XYZ", ["options_generic", "ema20"])
    assert ctrl.is_strategy_allowed("xyz", "option_strategy") is True
    assert ctrl.is_strategy_allowed("xyz", "options_generic") is True
    assert ctrl.is_strategy_allowed("xyz", "ema20_strategy") is True
    assert ctrl.is_strategy_allowed("xyz", "unknown_strategy") is False

    snap = ctrl.snapshot()
    assert snap["XYZ"]["enabled"] is True
    assert snap["XYZ"]["allowed_strategies"] == ["ema20_strategy", "option_strategy"]
