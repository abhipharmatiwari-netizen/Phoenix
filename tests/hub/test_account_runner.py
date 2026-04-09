import importlib
import types
from types import SimpleNamespace

import pytest

MODULE_PATH = "app.hub.account_runner"
CALLABLE_NAMES = ['AccountRunner']


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


@pytest.mark.asyncio
async def test_account_runner_login_failure_keeps_runner_not_running():
    mod = importlib.import_module(MODULE_PATH)

    class _BrokerClient:
        async def login(self):
            raise RuntimeError("bad credentials")

    runner = mod.AccountRunner(
        tenant_id="tenant-1",
        broker_account=SimpleNamespace(
            broker_account_id="A1",
            broker_type="angel",
        ),
        broker_client=_BrokerClient(),
        execution_port=SimpleNamespace(),
        runtime_mode="LIVE",
        state_store=SimpleNamespace(),
    )

    await runner.start()

    assert runner.is_running is False
    await runner.stop()
