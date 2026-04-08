import importlib
import sys
import types

import pytest

MODULE_PATH = "app.main"
main_mod = importlib.import_module(MODULE_PATH)


def test_import_module_succeeds():
    mod = importlib.import_module(MODULE_PATH)
    assert isinstance(mod, types.ModuleType)


@pytest.mark.parametrize("name", ["main"])
def test_module_defines_callable(name):
    mod = importlib.import_module(MODULE_PATH)
    assert hasattr(mod, name), f"Module {MODULE_PATH} should define {name}"


def test_main_rejects_non_uvicorn_modes(monkeypatch):
    monkeypatch.setenv("RUNNER_MODE", "stream")
    with pytest.raises(SystemExit) as exc:
        main_mod.main()
    assert exc.value.code == 1


def test_main_uvicorn_mode_invokes_uvicorn(monkeypatch):
    called: dict = {}
    dummy_uvicorn = types.SimpleNamespace(
        run=lambda *args, **kwargs: called.update({"args": args, "kwargs": kwargs})
    )
    monkeypatch.setitem(sys.modules, "uvicorn", dummy_uvicorn)
    monkeypatch.setenv("RUNNER_MODE", "uvicorn")
    monkeypatch.setenv("HOST", "1.2.3.4")
    monkeypatch.setenv("PORT", "9999")
    monkeypatch.setenv("UVICORN_LOG_LEVEL", "warning")
    monkeypatch.setenv("PORT_BIND_PREFLIGHT", "0")

    main_mod.main()

    assert called["args"] == ("app.server:app",)
    assert called["kwargs"]["host"] == "1.2.3.4"
    assert called["kwargs"]["port"] == 9999
    assert called["kwargs"]["log_level"] == "warning"


def test_main_uvicorn_mode_exits_when_missing_dependency(monkeypatch):
    monkeypatch.setenv("RUNNER_MODE", "uvicorn")
    monkeypatch.setenv("PORT_BIND_PREFLIGHT", "0")

    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "uvicorn":
            raise ImportError("missing uvicorn")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(SystemExit) as exc:
        main_mod.main()
    assert exc.value.code == 1


def test_preflight_strict_mode_returns_false_on_bind_error(monkeypatch):
    class _Sock:
        def setsockopt(self, *_args, **_kwargs):
            return None

        def bind(self, _addr):
            raise OSError("in use")

        def close(self):
            return None

    monkeypatch.setenv("PORT_BIND_PREFLIGHT", "1")
    monkeypatch.setenv("PORT_BIND_PREFLIGHT_MODE", "strict")
    monkeypatch.setattr(main_mod.socket, "socket", lambda *_a, **_k: _Sock())
    assert main_mod._preflight_port_bind("0.0.0.0", 8080) is False


def test_preflight_soft_mode_continues_on_bind_error(monkeypatch):
    class _Sock:
        def setsockopt(self, *_args, **_kwargs):
            return None

        def bind(self, _addr):
            raise OSError("in use")

        def close(self):
            return None

    monkeypatch.setenv("PORT_BIND_PREFLIGHT", "1")
    monkeypatch.setenv("PORT_BIND_PREFLIGHT_MODE", "soft")
    monkeypatch.setattr(main_mod.socket, "socket", lambda *_a, **_k: _Sock())
    assert main_mod._preflight_port_bind("0.0.0.0", 8080) is True
