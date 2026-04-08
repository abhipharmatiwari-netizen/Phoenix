import importlib
import types
import pytest

from app.tenants.models import BrokerAccountModel

MODULE_PATH = "app.tenants.models"
CALLABLE_NAMES = ['TenantModel', 'BrokerAccountModel', 'SubscriptionModel', 'StrategyConfigModel']


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


def test_broker_account_model_parses_default_strategies_json():
    model = BrokerAccountModel(
        broker_account_id="broker-1",
        tenant_id="tenant-default",
        broker_type="angel",
        display_name="Test",
        client_code="abc",
        secret_ref="secret",
        default_strategies='["ema20_strategy"]',
    )

    assert model.default_strategies == ["ema20_strategy"]
