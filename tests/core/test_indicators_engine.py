import importlib
import types
import pytest

MODULE_PATH = "app.core.indicators_engine"
CALLABLE_NAMES = ['Candle', 'TimeframeState', 'InstrumentState', '_compute_atr', '_compute_rsi_from_closes', '_ema_series', '_compute_macd_from_closes', '_compute_adx_dmi', 'MultiInstrumentIndicatorEngine']


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
