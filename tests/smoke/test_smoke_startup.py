"""
Smoke test suite for Phoenix v8 startup and basic order flow.

Validates:
- FastAPI app starts and responds to health probes
- Settings load without error
- Core module imports succeed
- Health endpoints return expected schema
- Strategy switchboard initializes
"""

from __future__ import annotations

import importlib
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

pytestmark = pytest.mark.smoke


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Ensure smoke tests don't load real broker credentials or connect to prod."""
    monkeypatch.setenv("TRADE_MODE", "PAPER")
    monkeypatch.setenv("CONTROL_PLANE_BACKEND", "postgres")
    monkeypatch.setenv("SWEEP_STATE_BACKEND", "postgres")
    monkeypatch.setenv("BROKER_SECRET_BACKEND", "env")
    monkeypatch.setenv("ENABLE_MULTI_HUB", "false")
    monkeypatch.setenv("DASHBOARD_AUTH_DISABLED", "true")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    monkeypatch.setenv("PROFIT_SWEEP_ENABLED", "true")
    monkeypatch.setenv("PROFIT_SWEEP_STRATEGY", "SIMPLE")
    monkeypatch.setenv("PROFIT_DAILY_TARGET", "2000")
    monkeypatch.setenv("PROFIT_ENABLE_DAILY_TARGET", "true")


class TestSettingsLoad:
    """Settings load and validate without crash."""

    def test_settings_instantiate(self):
        from app.config.settings import Settings

        settings = Settings()
        assert settings.default_time_zone == "Asia/Kolkata"

    def test_settings_trade_mode_default(self):
        from app.config.settings import Settings

        settings = Settings()
        assert settings.log_level in ("INFO", "WARNING", "DEBUG", "ERROR")

    def test_settings_risk_guard_dataclass(self):
        from app.config.settings import Settings

        settings = Settings()
        rg = settings.risk_guard_settings
        assert hasattr(rg, "enable_capital_checks")
        assert hasattr(rg, "enable_risk_checks")
        assert hasattr(rg, "enable_profit_checks")


class TestCoreImports:
    """Critical modules import without error."""

    def test_import_auth_routes(self):
        mod = importlib.import_module("app.api.auth_routes")
        assert hasattr(mod, "router")

    def test_import_order_router(self):
        from app.orders import router
        assert hasattr(router, "OrderRouter") or hasattr(router, "route_order")

    def test_import_risk_manager(self):
        from app.core import risk_manager
        assert risk_manager is not None

    def test_import_indicators_engine(self):
        from app.core import indicators_engine
        assert indicators_engine is not None

    def test_import_order_lifecycle(self):
        from app.orders import order_lifecycle
        assert order_lifecycle is not None

    def test_import_startup_validator(self):
        from app.core.startup_config_validator import validate_startup_config
        assert callable(validate_startup_config)

    def test_import_strategy_switchboard(self):
        from app.core.strategy_switch import StrategySwitchboard
        sb = StrategySwitchboard()
        snapshot = sb.snapshot()
        assert isinstance(snapshot, dict)

    def test_import_instrument_controller(self):
        from app.core.instrument_control import InstrumentController
        ic = InstrumentController()
        snapshot = ic.snapshot()
        assert isinstance(snapshot, dict)

    def test_uvicorn_import_string_resolves_server_app(self):
        from uvicorn.importer import import_from_string

        app = import_from_string("app.server:app")
        assert app.title == "Multi-instrument stream"


class TestHealthEndpoints:
    """FastAPI health endpoints return expected shapes."""

    @pytest.fixture
    def client(self):
        """Create a test client with mocked runtime to avoid broker/DB connections."""
        mock_runtime = MagicMock()
        mock_runtime.start = AsyncMock()
        mock_runtime.stop = AsyncMock()
        mock_runtime.stream_worker_running = MagicMock(return_value=False)
        mock_runtime.watchdog_running = MagicMock(return_value=False)
        mock_runtime.schema_status = MagicMock(return_value={
            "status": "ok",
            "checked_at": "2026-01-01T00:00:00Z",
            "missing_tables": [],
            "missing_indexes": [],
        })
        mock_runtime.watchdog_status = MagicMock(return_value={})
        mock_runtime.strategy_switchboard = MagicMock()
        mock_runtime.strategy_switchboard.snapshot = MagicMock(return_value={})
        mock_runtime.instrument_controller = MagicMock()
        mock_runtime.instrument_controller.snapshot = MagicMock(return_value={})

        with patch("app.runtime.app_runtime.get_app_runtime", return_value=mock_runtime), \
             patch("app.server.get_app_runtime", return_value=mock_runtime), \
             patch("app.server._app_runtime", mock_runtime), \
             patch("app.server.strategy_switchboard", mock_runtime.strategy_switchboard), \
             patch("app.server.instrument_controller", mock_runtime.instrument_controller):
            try:
                from httpx import ASGITransport, AsyncClient  # noqa: F401 — availability probe
                # Return async client info for async tests
                return {"runtime": mock_runtime, "use_httpx": True}
            except ImportError:
                return {"runtime": mock_runtime, "use_httpx": False}

    @pytest.mark.asyncio
    async def test_health_endpoint_schema(self, client):
        """Verify /health returns expected keys (unit-level check)."""
        from app.server import health
        result = await health()
        assert isinstance(result, dict)
        assert "status" in result
        assert result["status"] == "ok"
        assert result["service"] == "phoenix"

    def test_ready_endpoint_exists(self):
        """Verify /ready endpoint is registered."""
        from app.server import app
        routes = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/ready" in routes

    def test_health_summary_endpoint_exists(self):
        """Verify /health/summary endpoint is registered."""
        from app.server import app
        routes = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/health/summary" in routes


class TestStartupValidator:
    """Startup config validator catches invalid configs."""

    def test_rejects_invalid_trade_mode(self):
        from app.core.startup_config_validator import validate_startup_config
        with pytest.raises(ValueError, match="TRADE_MODE"):
            validate_startup_config(
                strategy_cfg={"strategies": []},
                trade_mode="INVALID",
                disable_trading_window_filter=False,
                known_strategy_names=[],
            )

    def test_rejects_live_with_disabled_trading_window(self):
        from app.core.startup_config_validator import validate_startup_config
        with pytest.raises(ValueError, match="DISABLE_TRADING_WINDOW_FILTER"):
            validate_startup_config(
                strategy_cfg={"strategies": []},
                trade_mode="LIVE",
                disable_trading_window_filter=True,
                known_strategy_names=[],
            )

    def test_accepts_paper_mode(self):
        from app.core.startup_config_validator import validate_startup_config
        # Should not raise
        validate_startup_config(
            strategy_cfg={"strategies": []},
            trade_mode="PAPER",
            disable_trading_window_filter=False,
            known_strategy_names=[],
        )
