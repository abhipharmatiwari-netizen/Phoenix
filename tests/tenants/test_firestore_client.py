import importlib
import types
import pytest

MODULE_PATH = "app.tenants.firestore_client"
CALLABLE_NAMES = ['_client', '_doc_to_model', 'upsert_tenant', 'get_tenant', 'get_all_active_tenants', 'get_all_tenants', 'upsert_broker_account', 'get_broker_account', 'get_broker_accounts_for_tenant', 'get_all_broker_accounts', 'get_active_broker_accounts', 'upsert_subscription', 'get_subscriptions_for_account', 'get_all_subscriptions', 'upsert_strategy_config', 'get_strategy_configs_for_account']


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
