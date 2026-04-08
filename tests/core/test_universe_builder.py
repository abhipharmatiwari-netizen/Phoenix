import importlib
import types
import pytest

MODULE_PATH = "app.core.universe_builder"
CALLABLE_NAMES = ['_exchange_type_for_segment', '_format_strike', '_override_token', '_build_secure_headers', 'get_ltp_for_token', 'get_ltp_for_tokens', '_load_universe', '_build_future_underlying', '_build_index_underlying', 'build_instrument_universe']


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


def test_full_instrument_list_logging_defaults_off_in_live(monkeypatch):
    monkeypatch.delenv("UNIVERSE_LOG_FULL_LIST", raising=False)
    monkeypatch.setenv("TRADE_MODE", "LIVE")
    mod = importlib.import_module(MODULE_PATH)

    assert mod._should_log_full_instrument_list() is False


def test_full_instrument_list_logging_can_be_forced_in_live(monkeypatch):
    monkeypatch.setenv("TRADE_MODE", "LIVE")
    monkeypatch.setenv("UNIVERSE_LOG_FULL_LIST", "1")
    mod = importlib.import_module(MODULE_PATH)

    assert mod._should_log_full_instrument_list() is True
