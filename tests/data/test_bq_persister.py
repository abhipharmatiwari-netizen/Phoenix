import importlib
import types
import pytest

MODULE_PATH = "app.data.bq_persister"
CALLABLE_NAMES = ['get_bq_client', '_orders_table_id', '_trades_table_id', '_daily_pnl_table_id', 'insert_order_record', 'insert_trade_record', 'insert_daily_pnl_snapshot', 'fetch_trades_for_tenant']


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
