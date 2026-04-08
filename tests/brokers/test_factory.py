import importlib
import types

from app.tenants.models import BrokerAccountModel

MODULE_PATH = "app.brokers.factory"
CALLABLE_NAMES = ["create_broker_client"]


def _account(*, broker_type: str = "angel") -> BrokerAccountModel:
    return BrokerAccountModel(
        broker_account_id="A1",
        tenant_id="tenant-1",
        broker_type=broker_type,
        display_name="acct",
        client_code="client",
        secret_ref="secret-ref",
    )


def test_import_module_succeeds():
    mod = importlib.import_module(MODULE_PATH)
    assert isinstance(mod, types.ModuleType)


def test_module_defines_callable():
    mod = importlib.import_module(MODULE_PATH)
    for name in CALLABLE_NAMES:
        assert hasattr(mod, name), f"Module {MODULE_PATH} should define {name}"


def test_factory_shadow_mode_never_touches_angel_paths(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)

    def _raise_if_called(*_args, **_kwargs):
        raise AssertionError("Angel path should not be called in SHADOW mode")

    monkeypatch.setattr(mod, "resolve_angel_secrets", _raise_if_called)
    monkeypatch.setattr(mod, "AngelBrokerClient", _raise_if_called)

    client = mod.create_broker_client(_account(broker_type="angel"), "SHADOW")
    assert type(client).__name__ == "ShadowBrokerClient"


def test_factory_live_mode_angel_uses_secrets_and_angel_client(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)
    calls: list[tuple] = []

    def _fake_resolve(account):
        calls.append(("resolve", account.broker_account_id))
        return {"api_key": "x", "jwt_token": "y", "feed_token": "z"}

    class _FakeAngelClient:
        def __init__(self, *, account, secrets):
            calls.append(("angel", account.broker_account_id, bool(secrets)))

    monkeypatch.setattr(mod, "resolve_angel_secrets", _fake_resolve)
    monkeypatch.setattr(mod, "AngelBrokerClient", _FakeAngelClient)

    client = mod.create_broker_client(_account(broker_type="angel"), "LIVE")
    assert type(client).__name__ == "_FakeAngelClient"
    assert calls == [("resolve", "A1"), ("angel", "A1", True)]


def test_factory_unknown_broker_uses_simulated_client():
    mod = importlib.import_module(MODULE_PATH)
    client = mod.create_broker_client(_account(broker_type="unknown"), "PAPER")
    assert type(client).__name__ == "SimulatedBrokerClient"
