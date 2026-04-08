import importlib

from app.core.strategy_switch import StrategySwitchboard


def test_import_module_succeeds():
    mod = importlib.import_module("app.core.strategy_switch")
    assert hasattr(mod, "StrategySwitchboard")


def test_strategy_switchboard_defaults_to_enabled():
    board = StrategySwitchboard()
    assert board.is_enabled("unknown") is True


def test_strategy_switchboard_toggles_and_snapshots():
    board = StrategySwitchboard({"options_generic": False})

    assert board.is_enabled("option_strategy") is False
    board.set_enabled("option_strategy", True)
    board.set_enabled("delta", False)

    snap = board.snapshot()
    assert snap == {"option_strategy": True, "delta_strangle": False}
