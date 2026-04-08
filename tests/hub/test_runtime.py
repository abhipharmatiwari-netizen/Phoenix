import importlib
import types
from types import SimpleNamespace
import pytest

MODULE_PATH = "app.hub.runtime"
CALLABLE_NAMES = ['HubRuntime', 'get_hub_runtime']


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


def test_allow_mock_sweep_state_explicit_true_for_local_dev(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)
    monkeypatch.setenv("ALLOW_MOCK_SWEEP_STATE", "true")
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("TRADE_MODE", "PAPER")
    monkeypatch.delenv("K_SERVICE", raising=False)
    assert mod._allow_mock_sweep_state_for_error(Exception("boom")) is True


def test_allow_mock_sweep_state_explicit_false(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)
    monkeypatch.setenv("ALLOW_MOCK_SWEEP_STATE", "false")
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("TRADE_MODE", "PAPER")
    monkeypatch.delenv("K_SERVICE", raising=False)
    assert mod._allow_mock_sweep_state_for_error(Exception("boom")) is False


def test_allow_mock_sweep_state_explicit_true_rejected_in_live(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)
    monkeypatch.setenv("ALLOW_MOCK_SWEEP_STATE", "true")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("TRADE_MODE", "LIVE")
    monkeypatch.delenv("K_SERVICE", raising=False)
    assert mod._allow_mock_sweep_state_for_error(Exception("boom")) is False


def test_allow_mock_sweep_state_explicit_true_rejected_in_non_local(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)
    monkeypatch.setenv("ALLOW_MOCK_SWEEP_STATE", "true")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("TRADE_MODE", "PAPER")
    monkeypatch.delenv("K_SERVICE", raising=False)
    assert mod._allow_mock_sweep_state_for_error(Exception("boom")) is False


def test_allow_mock_sweep_state_local_missing_control_plane(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)
    monkeypatch.delenv("ALLOW_MOCK_SWEEP_STATE", raising=False)
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("TRADE_MODE", "PAPER")
    monkeypatch.delenv("K_SERVICE", raising=False)
    err = RuntimeError("Missing control plane Postgres settings: CONTROL_PLANE_PG_HOST")
    assert mod._allow_mock_sweep_state_for_error(err) is True


def test_allow_mock_sweep_state_cloudrun_missing_control_plane(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)
    monkeypatch.delenv("ALLOW_MOCK_SWEEP_STATE", raising=False)
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("TRADE_MODE", "PAPER")
    monkeypatch.setenv("K_SERVICE", "svc")
    err = RuntimeError("Missing control plane Postgres settings: CONTROL_PLANE_PG_HOST")
    assert mod._allow_mock_sweep_state_for_error(err) is False


def test_initialize_sweep_and_eod_state_managers_rejects_live_mock_override(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)
    settings = SimpleNamespace(
        sweep_state_backend="postgres",
        default_time_zone="Asia/Kolkata",
    )
    sweep_calls = []
    eod_calls = []

    def _raise_init_error(_settings):
        raise RuntimeError("Missing control plane Postgres settings: CONTROL_PLANE_PG_HOST")

    def _mock_sweep():
        sweep_calls.append(True)
        return "mock-sweep"

    def _mock_eod():
        eod_calls.append(True)
        return "mock-eod"

    monkeypatch.setenv("ALLOW_MOCK_SWEEP_STATE", "true")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("TRADE_MODE", "LIVE")
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.setattr(mod, "_create_durable_sweep_and_eod_state_managers", _raise_init_error)
    monkeypatch.setattr(mod, "_create_mock_sweep_state_manager", _mock_sweep)
    monkeypatch.setattr(mod, "_create_mock_eod_state_manager", _mock_eod)

    with pytest.raises(RuntimeError, match="ALLOW_MOCK_SWEEP_STATE"):
        mod._initialize_sweep_and_eod_state_managers(settings)

    assert sweep_calls == []
    assert eod_calls == []


def test_initialize_sweep_and_eod_state_managers_allows_explicit_local_mock_override(
    monkeypatch,
):
    mod = importlib.import_module(MODULE_PATH)
    settings = SimpleNamespace(
        sweep_state_backend="postgres",
        default_time_zone="Asia/Kolkata",
    )
    sweep_calls = []
    eod_calls = []
    sweep_manager = object()
    eod_manager = object()

    def _raise_init_error(_settings):
        raise RuntimeError("database unavailable")

    def _mock_sweep():
        sweep_calls.append(True)
        return sweep_manager

    def _mock_eod():
        eod_calls.append(True)
        return eod_manager

    monkeypatch.setenv("ALLOW_MOCK_SWEEP_STATE", "true")
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("TRADE_MODE", "PAPER")
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.setattr(mod, "_create_durable_sweep_and_eod_state_managers", _raise_init_error)
    monkeypatch.setattr(mod, "_create_mock_sweep_state_manager", _mock_sweep)
    monkeypatch.setattr(mod, "_create_mock_eod_state_manager", _mock_eod)

    actual_sweep_manager, actual_eod_manager = mod._initialize_sweep_and_eod_state_managers(
        settings
    )

    assert actual_sweep_manager is sweep_manager
    assert actual_eod_manager is eod_manager
    assert sweep_calls == [True]
    assert eod_calls == [True]


def test_seed_runtime_pnl_snapshots_seeds_unique_active_accounts_and_fallback():
    mod = importlib.import_module(MODULE_PATH)

    class _PnlEngine:
        def __init__(self):
            self.calls = []

        def ensure_account_snapshot(self, *, tenant_id, broker_account_id, strategy_id=None):
            self.calls.append((str(tenant_id), str(broker_account_id)))
            return True

    pnl_engine = _PnlEngine()
    accounts = [
        SimpleNamespace(tenant_id="tenant-1", broker_account_id="A1"),
        SimpleNamespace(tenant_id="tenant-1", broker_account_id="A1"),  # duplicate
        SimpleNamespace(tenant_id="tenant-2", broker_account_id="B1"),
    ]
    settings = SimpleNamespace(
        hub_default_tenant_id="tenant-3",
        hub_default_broker_account_id="C1",
    )

    result = mod._seed_runtime_pnl_snapshots(
        pnl_engine=pnl_engine,
        settings=settings,
        account_loader=lambda: accounts,
    )

    assert result.seeded == 3
    assert result.account_pairs_count == 3
    assert result.status == "complete"
    assert pnl_engine.calls == [
        ("tenant-1", "A1"),
        ("tenant-2", "B1"),
        ("tenant-3", "C1"),
    ]


def test_seed_runtime_pnl_snapshots_skips_fallback_when_already_in_active_accounts():
    mod = importlib.import_module(MODULE_PATH)

    class _PnlEngine:
        def __init__(self):
            self.calls = []

        def ensure_account_snapshot(self, *, tenant_id, broker_account_id, strategy_id=None):
            self.calls.append((str(tenant_id), str(broker_account_id)))
            return True

    pnl_engine = _PnlEngine()
    accounts = [SimpleNamespace(tenant_id="tenant-1", broker_account_id="A1")]
    settings = SimpleNamespace(
        hub_default_tenant_id="tenant-1",
        hub_default_broker_account_id="A1",
    )

    result = mod._seed_runtime_pnl_snapshots(
        pnl_engine=pnl_engine,
        settings=settings,
        account_loader=lambda: accounts,
    )

    assert result.seeded == 1
    assert result.account_pairs_count == 1
    assert result.status == "complete"
    assert pnl_engine.calls == [("tenant-1", "A1")]
