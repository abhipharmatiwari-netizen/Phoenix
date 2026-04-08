import importlib
import types
import pytest

MODULE_PATH = "app.data.trade_records"
CALLABLE_NAMES = ['TradeRecord', '_clean_trade_id', '_parse_timestamp', '_default_tenant_id', '_default_broker_account_id', '_default_strategy_id', '_safe_int', '_safe_float', 'build_trade_record_from_fill', 'build_trade_record_from_exec', 'build_trade_record_from_legacy_trade', 'trade_record_to_bq_row']


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
