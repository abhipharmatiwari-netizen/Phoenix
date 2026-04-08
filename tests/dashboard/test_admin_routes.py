import importlib
import types
import pytest

MODULE_PATH = "app.dashboard.admin_routes"
CALLABLE_NAMES = ['TenantUpsertRequest', 'BrokerAccountUpsertRequest', 'SubscriptionUpsertRequest', 'AdminTestOrderRequest', 'list_tenants', 'list_broker_accounts', 'list_runners', 'create_or_update_tenant', 'create_or_update_broker_account', 'create_or_update_subscription', 'admin_test_order']


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
