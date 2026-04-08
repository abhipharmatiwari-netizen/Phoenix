import importlib
import types
from types import SimpleNamespace

import pytest

MODULE_PATH = "app.brokers.secret_loader"
CALLABLE_NAMES = ['get_secret_manager_client', '_build_secret_name', '_fetch_secret_payload', 'load_angel_secrets_from_secret_manager', 'load_angel_secrets_from_env', 'resolve_angel_secrets']


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


def _account():
    return SimpleNamespace(
        broker_account_id="acc-1",
        client_code="CLIENT123",
        secret_ref="secret-1",
    )


def test_resolve_angel_secrets_live_rejects_env_backend(monkeypatch):
    """LIVE rejects env as a broker-secret source."""
    mod = importlib.import_module(MODULE_PATH)
    monkeypatch.setenv("TRADE_MODE", "LIVE")
    monkeypatch.setenv("ANGEL_API_KEY", "test-key")
    monkeypatch.setenv("ANGEL_CLIENT_PIN", "1234")
    monkeypatch.setenv("ANGEL_TOTP_SECRET", "TOTP")
    monkeypatch.setattr(
        mod,
        "get_settings",
        lambda: SimpleNamespace(broker_secret_backend="env"),
    )

    with pytest.raises(RuntimeError, match="secret_manager, postgres"):
        mod.resolve_angel_secrets(_account())


def test_resolve_angel_secrets_live_accepts_postgres_backend(monkeypatch):
    """LIVE accepts postgres as broker secret backend."""
    mod = importlib.import_module(MODULE_PATH)
    monkeypatch.setenv("TRADE_MODE", "LIVE")
    sentinel = SimpleNamespace(
        api_key="k", api_secret="s", client_code="C", pin="1",
        totp_secret="T", client_local_ip="1.2.3.4",
        client_public_ip="5.6.7.8", mac_address="02:00:00:00:00:11",
    )
    monkeypatch.setattr(
        mod,
        "get_settings",
        lambda: SimpleNamespace(broker_secret_backend="postgres"),
    )
    monkeypatch.setattr(mod, "load_angel_secrets_from_postgres", lambda _account: sentinel)

    assert mod.resolve_angel_secrets(_account()) is sentinel


def test_resolve_angel_secrets_live_rejects_unknown_backend(monkeypatch):
    """LIVE rejects unrecognized broker secret backends."""
    mod = importlib.import_module(MODULE_PATH)
    monkeypatch.setenv("TRADE_MODE", "LIVE")
    monkeypatch.setattr(
        mod,
        "get_settings",
        lambda: SimpleNamespace(broker_secret_backend="plaintext_file"),
    )

    with pytest.raises(RuntimeError, match="TRADE_MODE=LIVE requires BROKER_SECRET_BACKEND"):
        mod.resolve_angel_secrets(_account())


def test_resolve_angel_secrets_live_accepts_secret_manager_backend(monkeypatch):
    """LIVE accepts secret_manager as broker secret backend (Cloud Run path)."""
    mod = importlib.import_module(MODULE_PATH)
    monkeypatch.setenv("TRADE_MODE", "LIVE")
    sentinel = SimpleNamespace(
        api_key="sm-key", api_secret="sm-secret", client_code="C", pin="1",
        totp_secret="T", client_local_ip="1.2.3.4",
        client_public_ip="5.6.7.8", mac_address="02:00:00:00:00:12",
    )
    monkeypatch.setattr(
        mod,
        "get_settings",
        lambda: SimpleNamespace(broker_secret_backend="secret_manager"),
    )
    monkeypatch.setattr(mod, "load_angel_secrets_from_secret_manager", lambda _account: sentinel)

    result = mod.resolve_angel_secrets(_account())
    assert result is sentinel
    assert result.api_key == "sm-key"


def test_resolve_angel_secrets_live_secret_manager_fails_closed(monkeypatch):
    """LIVE + secret_manager raises if Secret Manager returns None (no env fallback)."""
    mod = importlib.import_module(MODULE_PATH)
    monkeypatch.setenv("TRADE_MODE", "LIVE")
    monkeypatch.setattr(
        mod,
        "get_settings",
        lambda: SimpleNamespace(broker_secret_backend="secret_manager"),
    )
    monkeypatch.setattr(mod, "load_angel_secrets_from_secret_manager", lambda _account: None)

    with pytest.raises(RuntimeError, match="secret_manager credentials unavailable"):
        mod.resolve_angel_secrets(_account())


def test_resolve_angel_secrets_paper_secret_manager_falls_back_to_env(monkeypatch):
    """In PAPER mode, secret_manager failure falls back to env-based secrets."""
    mod = importlib.import_module(MODULE_PATH)
    monkeypatch.setenv("TRADE_MODE", "PAPER")
    env_sentinel = SimpleNamespace(
        api_key="env-key", api_secret="", client_code="C", pin="1",
        totp_secret="T", client_local_ip="127.0.0.1",
        client_public_ip="127.0.0.1", mac_address="00:00:00:00:00:00",
    )
    monkeypatch.setattr(
        mod,
        "get_settings",
        lambda: SimpleNamespace(broker_secret_backend="secret_manager"),
    )
    monkeypatch.setattr(mod, "load_angel_secrets_from_secret_manager", lambda _account: None)
    monkeypatch.setattr(mod, "load_angel_secrets_from_env", lambda _account: env_sentinel)

    result = mod.resolve_angel_secrets(_account())
    assert result is env_sentinel


def test_resolve_angel_secrets_auto_tries_postgres_then_secret_manager(monkeypatch):
    """Auto backend tries postgres first, then secret_manager."""
    mod = importlib.import_module(MODULE_PATH)
    monkeypatch.setenv("TRADE_MODE", "PAPER")
    sm_sentinel = SimpleNamespace(
        api_key="sm-key", api_secret="", client_code="C", pin="1",
        totp_secret="T", client_local_ip="1.2.3.4",
        client_public_ip="5.6.7.8", mac_address="02:00:00:00:00:13",
    )
    monkeypatch.setattr(
        mod,
        "get_settings",
        lambda: SimpleNamespace(broker_secret_backend="auto"),
    )
    monkeypatch.setattr(mod, "load_angel_secrets_from_postgres", lambda _account: None)
    monkeypatch.setattr(mod, "load_angel_secrets_from_secret_manager", lambda _account: sm_sentinel)

    result = mod.resolve_angel_secrets(_account())
    assert result is sm_sentinel


def test_resolve_angel_secrets_non_live_can_fallback_to_env(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)
    monkeypatch.setenv("TRADE_MODE", "PAPER")
    sentinel = object()
    monkeypatch.setattr(
        mod,
        "get_settings",
        lambda: SimpleNamespace(broker_secret_backend="postgres"),
    )
    monkeypatch.setattr(mod, "load_angel_secrets_from_postgres", lambda _account: None)
    monkeypatch.setattr(mod, "load_angel_secrets_from_env", lambda _account: sentinel)

    assert mod.resolve_angel_secrets(_account()) is sentinel


def test_resolve_angel_secrets_live_rejects_placeholder_network_identity(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)
    monkeypatch.setenv("TRADE_MODE", "LIVE")
    sentinel = SimpleNamespace(
        api_key="k", api_secret="s", client_code="C", pin="1",
        totp_secret="T", client_local_ip="127.0.0.1",
        client_public_ip="127.0.0.1", mac_address="AA:BB:CC:DD:EE:FF",
    )
    monkeypatch.setattr(
        mod,
        "get_settings",
        lambda: SimpleNamespace(broker_secret_backend="postgres"),
    )
    monkeypatch.setattr(mod, "load_angel_secrets_from_postgres", lambda _account: sentinel)

    with pytest.raises(ValueError, match="requires real broker network identity"):
        mod.resolve_angel_secrets(_account())


def test_build_secret_name_uses_project_and_prefix(monkeypatch):
    """_build_secret_name constructs the correct Secret Manager resource path."""
    mod = importlib.import_module(MODULE_PATH)
    monkeypatch.setattr(
        mod,
        "get_settings",
        lambda: SimpleNamespace(
            broker_secret_project_id="my-project",
            gcp_project_id="fallback-project",
            broker_secret_prefix="phoenix-",
        ),
    )

    name = mod._build_secret_name("broker-a1")
    assert name == "projects/my-project/secrets/phoenix-broker-a1/versions/latest"


def test_build_secret_name_falls_back_to_gcp_project(monkeypatch):
    """_build_secret_name falls back to gcp_project_id when broker_secret_project_id is empty."""
    mod = importlib.import_module(MODULE_PATH)
    monkeypatch.setattr(
        mod,
        "get_settings",
        lambda: SimpleNamespace(
            broker_secret_project_id="",
            gcp_project_id="fallback-project",
            broker_secret_prefix="",
        ),
    )

    name = mod._build_secret_name("my-secret")
    assert name == "projects/fallback-project/secrets/my-secret/versions/latest"


def test_build_secret_name_raises_without_project(monkeypatch):
    """_build_secret_name raises when no project id is available."""
    mod = importlib.import_module(MODULE_PATH)
    monkeypatch.setattr(
        mod,
        "get_settings",
        lambda: SimpleNamespace(
            broker_secret_project_id="",
            gcp_project_id="",
            broker_secret_prefix="",
        ),
    )

    with pytest.raises(RuntimeError, match="No project id configured"):
        mod._build_secret_name("my-secret")
