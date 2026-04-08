import importlib
import types

import pytest

MODULE_PATHS = [
    "app",
    "app.brokers",
    "app.core",
    "app.dashboard",
    "app.data",
    "app.hub",
    "app.indicators",
    "app.orders",
    "app.pnl",
    "app.risk",
    "app.runners",
    "app.strategies",
    "app.tenants",
]


@pytest.mark.parametrize("module_path", MODULE_PATHS)
def test_package_imports(module_path: str):
    mod = importlib.import_module(module_path)
    assert isinstance(mod, types.ModuleType)
