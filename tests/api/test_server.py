from datetime import datetime, timezone
import json
from types import SimpleNamespace

import importlib
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app.server as server
from app.data.state_store import StateStore
from app.core.dashboard_bus import DashboardBus
from app.core.instrument_control import InstrumentController
from app.core.strategy_switch import StrategySwitchboard
from app.brokers.base import Position, ProductType
from app.dashboard import auth as dashboard_auth


class DummyAppRuntime:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0
        self.worker_running_state = False
        self.watchdog_running_state = False
        self.worker_fatal_error_state = None
        self.ready = True  # default: ready so existing tests are unaffected
        self.startup_recovery_state = {
            "status": "ok",
            "reason": None,
            "summary": {},
        }
        self.schema_state = {
            "status": "ok",
            "checked_at": "2026-03-05T00:00:00Z",
            "missing_tables": [],
            "missing_indexes": [],
        }
        self.watchdog_state = {
            "restart_attempts": 0,
            "current_backoff_seconds": 0.0,
            "next_restart_in_seconds": 0.0,
        }
        self.leader_lease_state = {
            "enabled": False,
            "owned": True,
            "task_running": False,
        }

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1

    def stream_worker_running(self) -> bool:
        return self.worker_running_state

    def watchdog_running(self) -> bool:
        return self.watchdog_running_state

    def stream_worker_fatal_error(self):
        return self.worker_fatal_error_state

    def stream_worker_status(self) -> dict:
        return {
            "running": self.worker_running_state,
            "fatal_error": self.worker_fatal_error_state,
        }

    def schema_status(self) -> dict:
        return dict(self.schema_state)

    def watchdog_status(self) -> dict:
        return dict(self.watchdog_state)

    def leader_lease_status(self) -> dict:
        return dict(self.leader_lease_state)

    def startup_recovery_status(self) -> dict:
        return dict(self.startup_recovery_state)


def test_import_module_succeeds():
    mod = importlib.import_module("app.server")
    assert hasattr(mod, "app")


def test_dashboard_ws_prefers_delta_by_default_in_production(monkeypatch):
    monkeypatch.setattr(
        server,
        "get_boot_config",
        lambda: SimpleNamespace(runtime=SimpleNamespace(app_env="production")),
    )

    assert server._dashboard_ws_prefers_delta(None) is True
    assert server._dashboard_ws_prefers_delta("full") is False
    assert server._dashboard_ws_prefers_delta("delta") is True


def test_dashboard_ws_uses_full_by_default_in_local(monkeypatch):
    monkeypatch.setattr(
        server,
        "get_boot_config",
        lambda: SimpleNamespace(runtime=SimpleNamespace(app_env="local")),
    )

    assert server._dashboard_ws_prefers_delta(None) is False


def test_dashboard_auth_required_in_production_even_when_flag_disabled():
    assert server._dashboard_auth_required(
        SimpleNamespace(app_env="production", dashboard_auth_disabled=True)
    ) is True


def test_dashboard_auth_optional_only_in_local_when_disabled():
    assert server._dashboard_auth_required(
        SimpleNamespace(app_env="local", dashboard_auth_disabled=True)
    ) is False


@pytest.fixture
def api_client(monkeypatch):
    """Yield TestClient with worker/watchdog/switchboard patched to in-memory stubs."""
    runtime = DummyAppRuntime()
    monkeypatch.setattr(server, "get_app_runtime", lambda: runtime)
    monkeypatch.setattr(server, "strategy_switchboard", StrategySwitchboard({"s1": False}))
    monkeypatch.setattr(server, "instrument_controller", InstrumentController({"ABC": {"enabled": True}}))
    monkeypatch.setattr(
        server,
        "get_settings",
        lambda: SimpleNamespace(
            enable_multi_hub=False,
            log_level="INFO",
            admin_api_key="test-admin",
        ),
    )
    monkeypatch.setattr(
        importlib.import_module("app.dashboard.auth"),
        "get_settings",
        lambda: SimpleNamespace(admin_api_key="test-admin"),
    )
    monkeypatch.setattr(
        importlib.import_module("app.dashboard.auth"),
        "get_settings",
        lambda: SimpleNamespace(admin_api_key="test-admin"),
    )

    with TestClient(server.app) as client:
        yield client, runtime


def _fetch_dashboard_ws_ticket(client: TestClient, mode: str = "delta") -> str:
    response = client.post(
        f"/admin/dashboard/ws-ticket?mode={mode}",
        headers={"X-Admin-Key": "test-admin"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == mode
    return str(payload["ticket"])


def test_health_reports_worker_status(api_client):
    client, runtime = api_client
    runtime.worker_running_state = True
    runtime.watchdog_running_state = False

    resp = client.get("/health")
    payload = resp.json()

    assert resp.status_code == 200
    assert payload["status"] == "ok"
    assert payload["runtime_path"] == "app_runtime_lifespan"
    assert payload["order_path"] == "strategy_bridge_order_router"
    assert "boot_config_created_at" in payload
    assert payload["stream_worker_running"] is True
    assert payload["watchdog_running"] is False
    assert payload["leader_lease_status"]["owned"] is True


def test_health_response_sets_correlation_headers(api_client):
    client, _ = api_client
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.headers.get("X-Correlation-Id")
    assert resp.headers.get("X-Request-Id")


def test_health_summary_reports_required_fields(api_client, monkeypatch):
    client, runtime = api_client
    runtime.worker_running_state = True
    runtime.watchdog_running_state = True

    state_store = StateStore()
    state_store.update_positions_status(
        "acc-1",
        status="OK",
        last_ok_ts="2026-03-05T10:00:00+00:00",
        last_count=1,
        error_reason=None,
        blocked_ts=None,
        retry_after_seconds=None,
    )
    state_store.update_positions_status(
        "acc-2",
        status="BLOCKED",
        last_ok_ts=None,
        last_count=None,
        error_reason="rate_limited",
        blocked_ts="2026-03-05T11:00:00+00:00",
        retry_after_seconds=30.0,
    )
    monkeypatch.setattr(
        server,
        "get_hub_runtime",
        lambda: SimpleNamespace(
            hub=SimpleNamespace(list_runner_ids=lambda: ["acc-1", "acc-2"]),
            state_store=state_store,
        ),
    )

    resp = client.get("/health/summary")
    payload = resp.json()

    assert resp.status_code == 200
    assert payload["status"] == "ok"
    assert payload["schema_status"] == "ok"
    assert payload["stream_worker_running"] is True
    assert payload["watchdog_running"] is True
    assert payload["last_position_sync_ok_ts"] == "2026-03-05T10:00:00Z"
    assert payload["last_broker_blocked_or_rate_limited_ts"] == "2026-03-05T11:00:00Z"


def test_health_summary_degrades_when_schema_guard_degraded(api_client, monkeypatch):
    client, runtime = api_client
    runtime.worker_running_state = True
    runtime.watchdog_running_state = True
    runtime.schema_state = {
        "status": "degraded",
        "checked_at": "2026-03-05T00:00:00Z",
        "missing_tables": ["strategy_config_candidates"],
        "missing_indexes": [],
    }
    monkeypatch.setattr(
        server,
        "get_hub_runtime",
        lambda: SimpleNamespace(
            hub=SimpleNamespace(list_runner_ids=lambda: []),
            state_store=StateStore(),
        ),
    )

    resp = client.get("/health/summary")
    payload = resp.json()

    assert resp.status_code == 200
    assert payload["status"] == "degraded"
    assert payload["schema_status"] == "degraded"
    assert "schema_error" in payload["degraded_reasons"]


def test_health_summary_degrades_when_terminal_position_records_have_quantity(
    api_client,
    monkeypatch,
):
    client, runtime = api_client
    runtime.worker_running_state = True
    runtime.watchdog_running_state = True
    monkeypatch.setattr(
        server,
        "get_hub_runtime",
        lambda: SimpleNamespace(
            hub=SimpleNamespace(list_runner_ids=lambda: []),
            state_store=StateStore(),
        ),
    )
    monkeypatch.setattr(
        server,
        "get_settings",
        lambda: SimpleNamespace(
            control_plane_backend="postgres",
            enable_multi_hub=False,
            log_level="INFO",
            admin_api_key="test-admin",
        ),
    )
    from app.orders import position_record_invariants

    monkeypatch.setattr(
        position_record_invariants,
        "count_terminal_nonzero_position_records",
        lambda: 2,
    )

    resp = client.get("/health/summary")
    payload = resp.json()

    assert resp.status_code == 200
    assert payload["status"] == "degraded"
    assert (
        payload["position_record_invariants"]["terminal_nonzero_net_qty_count"]
        == 2
    )
    assert "terminal_position_records_nonzero_net_qty" in payload["degraded_reasons"]


def test_dashboard_status_degrades_when_readyz_position_authority_blocks(
    api_client,
    monkeypatch,
):
    client, runtime = api_client
    runtime.worker_running_state = True
    runtime.watchdog_running_state = True
    monkeypatch.setattr(server, "_readiness_trade_mode", lambda: "LIVE")
    monkeypatch.setenv("TRADE_MODE", "LIVE")
    from app.observability import alert_rules

    alert_rules.reset_alert_evaluator()
    hub_runtime = SimpleNamespace(
        hub=SimpleNamespace(list_runner_ids=lambda: []),
        state_store=StateStore(),
        order_lifecycle=SimpleNamespace(
            count_positions_by_state=lambda: {"RECONCILING": 1}
        ),
    )
    monkeypatch.setattr(server, "get_hub_runtime", lambda: hub_runtime)
    monkeypatch.setattr("app.hub.runtime.get_hub_runtime", lambda: hub_runtime)

    resp = client.get("/dashboard/status")
    payload = resp.json()

    assert resp.status_code == 200
    assert payload["status"] == "degraded"
    assert "position_authority_degraded" in payload["degraded_reasons"]
    assert payload["readiness"]["ready"] is False
    assert payload["readiness"]["http_status"] == 503
    assert payload["readiness"]["reason"] == "position_authority_degraded"
    assert "readiness_position_authority_degraded" in payload["alerts"]["firing_rules"]


def test_health_summary_surfaces_oi_ml_shadow_ingestion_degraded(
    api_client,
    monkeypatch,
):
    client, runtime = api_client
    runtime.worker_running_state = True
    runtime.watchdog_running_state = True
    monkeypatch.setattr(
        server,
        "get_hub_runtime",
        lambda: SimpleNamespace(
            hub=SimpleNamespace(list_runner_ids=lambda: []),
            state_store=StateStore(),
        ),
    )
    monkeypatch.setattr(
        server,
        "_oi_ml_shadow_ingestion_status",
        lambda: {
            "enabled": True,
            "status": "degraded",
            "reason": "option_chain_rows_missing",
            "dry_run_only": True,
            "live_order_path_enabled": False,
            "option_chain": {"today_row_count": 0},
        },
    )

    resp = client.get("/health/summary")
    payload = resp.json()

    assert resp.status_code == 200
    assert payload["status"] == "degraded"
    assert "oi_ml_shadow_ingestion_degraded" in payload["degraded_reasons"]
    assert payload["readiness"]["ready"] is True
    assert payload["oi_ml_shadow_ingestion"]["reason"] == "option_chain_rows_missing"
    assert payload["oi_ml_shadow_ingestion"]["live_order_path_enabled"] is False


def test_health_summary_does_not_expect_stream_worker_when_disabled(api_client, monkeypatch):
    client, runtime = api_client
    runtime.worker_running_state = False
    runtime.watchdog_running_state = False
    monkeypatch.setattr(
        server,
        "get_boot_config",
        lambda: SimpleNamespace(
            runtime=SimpleNamespace(
                disable_stream_worker=True,
                app_env="production",
                dashboard_auth_disabled=False,
            )
        ),
    )
    monkeypatch.setattr(
        server,
        "get_hub_runtime",
        lambda: SimpleNamespace(
            hub=SimpleNamespace(list_runner_ids=lambda: []),
            state_store=StateStore(),
        ),
    )

    resp = client.get("/health/summary")
    payload = resp.json()

    assert resp.status_code == 200
    assert payload["stream_worker_expected"] is False
    assert "stream_worker_stopped" not in payload["degraded_reasons"]


def test_strategy_selection_endpoint(api_client, monkeypatch):
    client, _ = api_client
    monkeypatch.setattr(
        server.dashboard_bus,
        "strategy_selection_snapshot",
        lambda: {
            "NIFTY_IDX": {
                "underlying": "NIFTY_IDX",
                "regime": "TRENDING",
                "selected_strategies": ["ema20_strategy"],
                "reason": "mapping",
            }
        },
    )

    resp = client.get(
        "/admin/strategy-selection",
        headers={"X-Admin-Key": "test-admin"},
    )
    payload = resp.json()
    assert resp.status_code == 200
    assert "NIFTY_IDX" in payload["strategy_selection"]
    assert payload["strategy_selection"]["NIFTY_IDX"]["regime"] == "TRENDING"


def test_dashboard_ws_delta_mode_emits_delta_then_compaction(api_client, monkeypatch):
    client, _ = api_client
    bus = DashboardBus()
    bus._delta_full_interval = 3
    ts_open = datetime(2024, 1, 1, 4, 0, tzinfo=timezone.utc)
    bus.set_instrument_meta({"ABC": {"symbol": "ABC", "token": "101", "lot_size": 1}})
    bus.record_tick("ABC", 100.0, ts_open)

    sleep_calls = {"count": 0}

    async def _fast_sleep(_: float) -> None:
        sleep_calls["count"] += 1
        if sleep_calls["count"] == 1:
            bus.record_tick("ABC", 101.0, ts_open.replace(minute=1))
        return None

    monkeypatch.setattr(server, "dashboard_bus", bus)
    monkeypatch.setattr(
        server,
        "get_boot_config",
        lambda: SimpleNamespace(
            runtime=SimpleNamespace(app_env="production", dashboard_auth_disabled=False)
        ),
    )
    monkeypatch.setattr(server.asyncio, "sleep", _fast_sleep)
    ticket = _fetch_dashboard_ws_ticket(client, mode="delta")

    with client.websocket_connect(
        f"/ws/dashboard?ticket={ticket}&mode=delta",
    ) as websocket:
        first_text = websocket.receive_text()
        second_text = websocket.receive_text()
        third_text = websocket.receive_text()

    first_payload = json.loads(first_text)
    second_payload = json.loads(second_text)
    third_payload = json.loads(third_text)

    assert first_payload["_mode"] == "full_snapshot"
    assert second_payload["_mode"] == "delta"
    assert "instruments" in second_payload
    assert len(second_text) < len(first_text)
    assert third_payload["_mode"] == "full_snapshot"


def test_dashboard_ws_rejects_invalid_or_expired_ticket(api_client, monkeypatch):
    client, _ = api_client
    monkeypatch.setattr(
        server,
        "get_boot_config",
        lambda: SimpleNamespace(
            runtime=SimpleNamespace(app_env="production", dashboard_auth_disabled=False)
        ),
    )

    with pytest.raises(WebSocketDisconnect) as invalid_exc:
        with client.websocket_connect("/ws/dashboard?ticket=invalid-ticket&mode=delta"):
            pass
    assert invalid_exc.value.code == 1008

    expired_ticket = dashboard_auth.issue_dashboard_ws_ticket(
        admin_ctx=dashboard_auth.AdminContext(caller="admin"),
        path="/ws/dashboard",
        mode="delta",
        settings=SimpleNamespace(
            admin_api_key="test-admin",
            dashboard_hmac_secret=None,
        ),
        now_ts=int(datetime.now(timezone.utc).timestamp()) - 120,
        ttl_seconds=1,
    ).token

    with pytest.raises(WebSocketDisconnect) as expired_exc:
        with client.websocket_connect(
            f"/ws/dashboard?ticket={expired_ticket}&mode=delta"
        ):
            pass
    assert expired_exc.value.code == 1008


def test_lifespan_uses_app_runtime_start_stop(monkeypatch):
    runtime = DummyAppRuntime()
    monkeypatch.setattr(server, "get_app_runtime", lambda: runtime)
    monkeypatch.setattr(
        server,
        "get_settings",
        lambda: SimpleNamespace(
            enable_multi_hub=False,
            log_level="INFO",
            admin_api_key="test-admin",
        ),
    )
    monkeypatch.setattr(
        importlib.import_module("app.dashboard.auth"),
        "get_settings",
        lambda: SimpleNamespace(admin_api_key="test-admin"),
    )
    with TestClient(server.app):
        pass

    assert runtime.started == 1
    assert runtime.stopped == 1


def test_ready_when_multi_hub_disabled(api_client):
    client, _ = api_client

    resp = client.get("/ready")
    payload = resp.json()

    assert payload == {"status": "ready", "multi_hub_enabled": False}


def test_ready_when_multi_hub_enabled(monkeypatch):
    # Prepare stub runtime + hub before creating TestClient so startup hooks use it.
    last_refresh = datetime(2024, 1, 1, tzinfo=timezone.utc)

    class DummyHub:
        def __init__(self):
            self.initialized = 0
            self.started = 0
            self.stopped = 0
            self.runner_count = 2
            self.last_subscription_refresh = last_refresh

        async def initialize(self):
            self.initialized += 1

        async def start_all(self):
            self.started += 1

        async def stop_all(self):
            self.stopped += 1

    runtime = SimpleNamespace(hub=DummyHub())

    monkeypatch.setattr(
        server,
        "get_settings",
        lambda: SimpleNamespace(
            enable_multi_hub=True,
            log_level="INFO",
            admin_api_key="test-admin",
        ),
    )
    monkeypatch.setattr(server, "get_hub_runtime", lambda: runtime)
    monkeypatch.setattr(server, "get_app_runtime", lambda: DummyAppRuntime())
    monkeypatch.setattr(server, "strategy_switchboard", StrategySwitchboard())
    monkeypatch.setattr(server, "instrument_controller", InstrumentController())
    monkeypatch.setattr(
        server.dashboard_bus, "set_mode", lambda mode: None
    )  # avoid thread mode mutation side effects
    monkeypatch.setattr(
        server, "refresh_daily_levels_cache", lambda: True
    )
    monkeypatch.setattr(
        importlib.import_module("app.tenants.firestore_client"),
        "get_all_tenants",
        lambda: [SimpleNamespace(tenant_id="t1")],
    )

    with TestClient(server.app) as client:
        resp = client.get("/ready?deep=true")
        payload = resp.json()

    assert payload["multi_hub_enabled"] is True
    assert payload["runner_count"] == 2
    assert payload["tenant_count"] == 1
    assert payload["last_subscription_refresh"] == last_refresh.isoformat()


def test_build_hub_positions_snapshot_resolves_ltp_from_dashboard_label(monkeypatch):
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [
            Position(
                symbol="NATURALGAS24MAR26310CE",
                quantity=-1250,
                avg_price=26.6,
                product_type=ProductType.INTRADAY,
            )
        ],
    )

    runtime = SimpleNamespace(
        hub=SimpleNamespace(
            list_runner_ids=lambda: ["A1"],
            get_runner=lambda broker_account_id: SimpleNamespace(tenant_id="tenant-1"),
        ),
        state_store=state_store,
    )

    monkeypatch.setattr(server, "get_hub_runtime", lambda: runtime)
    monkeypatch.setattr(
        server.dashboard_bus,
        "get_last_price_for_instrument",
        lambda **kwargs: 27.75 if kwargs.get("symbol") == "NATURALGAS24MAR26310CE" else None,
    )

    rows = server._build_hub_positions_snapshot()

    assert len(rows) == 1
    assert rows[0]["side"] == "SELL"
    assert rows[0]["ltp"] == 27.75


def test_build_hub_positions_snapshot_filters_by_admin_entitlement(monkeypatch):
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [
            Position(
                symbol="NATURALGAS24MAR26310CE",
                quantity=-1250,
                avg_price=26.6,
                product_type=ProductType.INTRADAY,
            )
        ],
    )
    state_store.set_positions(
        "A2",
        [
            Position(
                symbol="NIFTY17FEB2625750CE",
                quantity=-65,
                avg_price=101.0,
                product_type=ProductType.INTRADAY,
            )
        ],
    )
    runners = {
        "A1": SimpleNamespace(tenant_id="tenant-1"),
        "A2": SimpleNamespace(tenant_id="tenant-2"),
    }
    runtime = SimpleNamespace(
        hub=SimpleNamespace(
            list_runner_ids=lambda: ["A1", "A2"],
            get_runner=lambda broker_account_id: runners.get(broker_account_id),
        ),
        state_store=state_store,
    )
    monkeypatch.setattr(server, "get_hub_runtime", lambda: runtime)

    rows = server._build_hub_positions_snapshot(
        admin_ctx=dashboard_auth.AdminContext(
            caller="scoped",
            tenant_ids=("tenant-1",),
            broker_account_ids=("A1",),
        )
    )

    assert [row["broker_account_id"] for row in rows] == ["A1"]


def test_build_hub_pnl_snapshot_aggregates_open_and_realized_pnl(monkeypatch):
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [
            Position(
                symbol="NATURALGAS24MAR26310CE",
                quantity=-1250,
                avg_price=26.6,
                product_type=ProductType.INTRADAY,
            )
        ],
    )

    runtime = SimpleNamespace(
        hub=SimpleNamespace(
            list_runner_ids=lambda: ["A1"],
            get_runner=lambda broker_account_id: SimpleNamespace(tenant_id="tenant-1"),
        ),
        state_store=state_store,
        pnl_engine=SimpleNamespace(
            get_current_realized_pnl=lambda tenant_id, broker_account_id: 150.0
        ),
    )

    monkeypatch.setattr(server, "get_hub_runtime", lambda: runtime)
    monkeypatch.setattr(
        server.dashboard_bus,
        "get_last_price_for_instrument",
        lambda **kwargs: 27.75 if kwargs.get("symbol") == "NATURALGAS24MAR26310CE" else None,
    )

    rows = server._build_hub_positions_snapshot()
    pnl = server._build_hub_pnl_snapshot(rows)

    assert pnl["realized"] == 150.0
    assert pnl["open"] == pytest.approx(-1437.5)
    assert pnl["total"] == pytest.approx(-1287.5)
    assert pnl["by_instrument"] == {"NATURALGAS24MAR26310CE": pytest.approx(-1437.5)}


def test_build_hub_pnl_snapshot_keeps_realized_when_positions_are_flat(monkeypatch):
    runtime = SimpleNamespace(
        hub=SimpleNamespace(
            list_runner_ids=lambda: ["A1"],
            get_runner=lambda broker_account_id: SimpleNamespace(tenant_id="tenant-1"),
        ),
        state_store=StateStore(),
        pnl_engine=SimpleNamespace(
            get_current_realized_pnl=lambda tenant_id, broker_account_id: 99.25
        ),
    )

    monkeypatch.setattr(server, "get_hub_runtime", lambda: runtime)

    pnl = server._build_hub_pnl_snapshot([])

    assert pnl == {
        "by_instrument": {},
        "total": 99.25,
        "realized": 99.25,
        "open": 0.0,
        "invalid_marks_count": 0,
    }


def test_build_hub_pnl_snapshot_counts_invalid_avg_price_marks(monkeypatch):
    runtime = SimpleNamespace(
        hub=SimpleNamespace(list_runner_ids=lambda: [], get_runner=lambda _: None),
        state_store=StateStore(),
        pnl_engine=None,
    )
    monkeypatch.setattr(server, "get_hub_runtime", lambda: runtime)

    pnl = server._build_hub_pnl_snapshot(
        [
            {
                "symbol": "NIFTY17FEB2625750CE",
                "net_qty": -65,
                "avg_price": 0.0,
                "entry_price": 0.0,
                "ltp": 151.2,
                "unrealized_pnl": None,
            }
        ]
    )

    assert pnl["open"] == 0.0
    assert pnl["invalid_marks_count"] == 1
    assert pnl["by_instrument"] == {}


def test_toggle_strategy_endpoint_updates_switchboard(api_client):
    client, _ = api_client

    resp = client.post(
        "/admin/strategies/toggle",
        json={"name": "s1", "enabled": True},
        headers={"X-Admin-Key": "test-admin"},
    )
    payload = resp.json()

    assert resp.status_code == 200
    assert payload == {"name": "s1", "enabled": True}

    resp_missing = client.post(
        "/admin/strategies/toggle",
        json={"name": "", "enabled": True},
        headers={"X-Admin-Key": "test-admin"},
    )
    assert resp_missing.status_code == 400


def test_toggle_strategy_endpoint_emits_app_log(api_client, caplog):
    client, _ = api_client
    caplog.set_level("INFO", logger="app.server")

    resp = client.post(
        "/admin/strategies/toggle",
        json={"name": "s1", "enabled": True},
        headers={"X-Admin-Key": "test-admin", "X-Request-Id": "req-123"},
    )

    assert resp.status_code == 200
    assert "event_type=ADMIN_STRATEGY_TOGGLE" in caplog.text
    assert "request_id=req-123" in caplog.text


def test_strategy_alias_usage_endpoint(api_client):
    client, _ = api_client

    client.post(
        "/admin/strategies/toggle",
        json={"name": "options_generic", "enabled": True},
        headers={"X-Admin-Key": "test-admin"},
    )
    resp = client.get(
        "/admin/strategies/aliases",
        headers={"X-Admin-Key": "test-admin"},
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert "strategy_alias_usage" in payload
    assert any("options_generic->option_strategy" in k for k in payload["strategy_alias_usage"].keys())


def test_toggle_strategy_endpoint_canonicalizes_aliases(api_client):
    client, _ = api_client

    resp = client.post(
        "/admin/strategies/toggle",
        json={"name": "options_generic", "enabled": True},
        headers={"X-Admin-Key": "test-admin"},
    )
    payload = resp.json()

    assert resp.status_code == 200
    assert payload == {"name": "option_strategy", "enabled": True}


def test_instrument_update_endpoint(api_client):
    client, _ = api_client

    resp = client.post(
        "/admin/instruments/update",
        json={"underlying": "abc", "enabled": False, "allowed_strategies": ["one", "two"]},
        headers={"X-Admin-Key": "test-admin"},
    )
    payload = resp.json()

    assert resp.status_code == 200
    assert payload["underlying"] == "abc"
    assert payload["state"]["enabled"] is False
    assert set(payload["state"]["allowed_strategies"]) == {"one", "two"}

    resp_missing = client.post(
        "/admin/instruments/update",
        json={"underlying": ""},
        headers={"X-Admin-Key": "test-admin"},
    )
    assert resp_missing.status_code == 400


def test_instrument_update_endpoint_emits_app_log(api_client, caplog):
    client, _ = api_client
    caplog.set_level("INFO", logger="app.server")

    resp = client.post(
        "/admin/instruments/update",
        json={"underlying": "abc", "enabled": False, "allowed_strategies": ["one"]},
        headers={"X-Admin-Key": "test-admin", "X-Request-Id": "req-456"},
    )

    assert resp.status_code == 200
    assert "event_type=ADMIN_INSTRUMENT_UPDATE" in caplog.text
    assert "instrument=ABC" in caplog.text


def test_instrument_update_endpoint_canonicalizes_allowed_strategies(api_client):
    client, _ = api_client

    resp = client.post(
        "/admin/instruments/update",
        json={
            "underlying": "abc",
            "allowed_strategies": ["options_generic", "ema20", "put-buy"],
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    payload = resp.json()

    assert resp.status_code == 200
    assert set(payload["state"]["allowed_strategies"]) == {
        "option_strategy",
        "ema20_strategy",
        "put_buy",
    }


def test_ml_health_reports_disabled_without_model_loading(monkeypatch):
    monkeypatch.setattr(
        server,
        "get_boot_config",
        lambda: SimpleNamespace(
            ml_config=SimpleNamespace(enabled=False, mode="OFF", models_dir=""),
            runtime=SimpleNamespace(dashboard_auth_disabled=False),
        ),
    )
    monkeypatch.setattr(server, "get_app_runtime", lambda: DummyAppRuntime())
    monkeypatch.setattr(
        server,
        "get_settings",
        lambda: SimpleNamespace(
            enable_multi_hub=False,
            log_level="INFO",
            admin_api_key="test-admin",
        ),
    )
    monkeypatch.setattr(
        importlib.import_module("app.dashboard.auth"),
        "get_settings",
        lambda: SimpleNamespace(admin_api_key="test-admin"),
    )

    with TestClient(server.app) as client:
        resp = client.get("/ml/health")
        payload = resp.json()

    assert payload["ml_enabled"] is False
    assert payload["global_mode"] == "OFF"
    assert payload["models_dir"] == ""
    assert payload["underlyings"] == []


def test_angel_postback_updates_state_store(api_client, monkeypatch):
    client, _ = api_client
    runtime = SimpleNamespace(state_store=StateStore())
    monkeypatch.setattr(server, "get_hub_runtime", lambda: runtime)
    monkeypatch.setattr(
        server,
        "get_broker_account_by_client_code",
        lambda client_code: SimpleNamespace(
            broker_account_id="acc-1", tenant_id="tenant-1"
        ),
    )

    payload = {
        "clientcode": "DUMMY123",
        "orderid": "111111111111111",
        "tradingsymbol": "SBIN-EQ",
        "transactiontype": "BUY",
        "orderstatus": "open",
        "ordertype": "MARKET",
        "producttype": "DELIVERY",
        "duration": "DAY",
        "price": 0.0,
        "quantity": "1000",
        "averageprice": 584.7,
        "filledshares": "74",
        "exchange": "NSE",
        "updatetime": "09-Oct-2023 18:22:02",
    }

    resp = client.post(
        "/webhook/angel/postback",
        json=payload,
        headers={"X-Admin-Key": "test-admin"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["broker_account_id"] == "acc-1"

    orders = runtime.state_store.get_orders("acc-1")
    assert len(orders) == 1
    order = orders[0]
    assert order.order_id == "111111111111111"
    assert order.symbol == "SBIN-EQ"
    assert order.side == "BUY"
    assert order.status == "open"
    assert order.quantity == 1000
    assert order.filled_quantity == 74
    assert order.average_price == 584.7
    assert order.order_type == "MARKET"
    assert order.product_type == "DELIVERY"


def _postback_payload() -> dict[str, object]:
    return {
        "clientcode": "DUMMY123",
        "orderid": "111111111111111",
        "tradingsymbol": "SBIN-EQ",
        "transactiontype": "BUY",
        "orderstatus": "open",
        "ordertype": "MARKET",
        "producttype": "DELIVERY",
        "duration": "DAY",
        "price": 0.0,
        "quantity": "1000",
        "averageprice": 584.7,
        "filledshares": "74",
        "exchange": "NSE",
        "updatetime": "09-Oct-2023 18:22:02",
    }


def _postback_settings(**overrides):
    base = {
        "enable_multi_hub": False,
        "log_level": "INFO",
        "admin_api_key": "test-admin",
        "dashboard_hmac_auth_enabled": False,
        "dashboard_hmac_secret": None,
        "dashboard_hmac_max_skew_seconds": 300,
        "angel_postback_token": "broker-secret",
        "angel_postback_relay_token": "relay-secret",
        "hub_default_broker_account_id": "",
        "hub_default_tenant_id": "tenant-1",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_angel_postback_legacy_mode_keeps_admin_auth(api_client, monkeypatch):
    client, _ = api_client
    monkeypatch.setenv("ANGEL_POSTBACK_AUTH_MODE", "LEGACY")
    # Ensure auth is required so the missing X-Admin-Key returns 401 before
    # any Postgres paths are hit. CI sets APP_ENV=test + DASHBOARD_AUTH_DISABLED=1
    # which bypasses auth; clearing that flag reinstates the requirement.
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("DASHBOARD_AUTH_DISABLED", raising=False)
    monkeypatch.setattr(server, "get_settings", lambda: _postback_settings())
    monkeypatch.setattr(
        importlib.import_module("app.dashboard.auth"),
        "get_settings",
        lambda: _postback_settings(),
    )

    resp = client.post("/webhook/angel/postback", json=_postback_payload())

    assert resp.status_code == 401


def test_angel_postback_direct_broker_mode_accepts_broker_token_without_admin(
    api_client,
    monkeypatch,
):
    client, _ = api_client
    runtime = SimpleNamespace(state_store=StateStore())
    monkeypatch.setenv("ANGEL_POSTBACK_AUTH_MODE", "DIRECT_BROKER")
    monkeypatch.setattr(server, "get_hub_runtime", lambda: runtime)
    monkeypatch.setattr(server, "get_settings", lambda: _postback_settings())
    monkeypatch.setattr(
        server,
        "get_broker_account_by_client_code",
        lambda client_code: SimpleNamespace(
            broker_account_id="acc-1", tenant_id="tenant-1"
        ),
    )

    resp = client.post(
        "/webhook/angel/postback",
        json=_postback_payload(),
        headers={"X-Angel-Postback-Token": "broker-secret"},
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_angel_postback_direct_broker_mode_rejects_invalid_request(
    api_client,
    monkeypatch,
):
    client, _ = api_client
    monkeypatch.setenv("ANGEL_POSTBACK_AUTH_MODE", "DIRECT_BROKER")
    monkeypatch.setattr(server, "get_settings", lambda: _postback_settings())

    unauthorized = client.post(
        "/webhook/angel/postback",
        json=_postback_payload(),
        headers={"X-Angel-Postback-Token": "wrong-token"},
    )
    malformed = client.post(
        "/webhook/angel/postback",
        json={"clientcode": "DUMMY123"},
        headers={"X-Angel-Postback-Token": "broker-secret"},
    )

    assert unauthorized.status_code == 401
    assert malformed.status_code == 400


def test_angel_postback_relay_mode_accepts_relay_and_rejects_direct(
    api_client,
    monkeypatch,
):
    client, _ = api_client
    runtime = SimpleNamespace(state_store=StateStore())
    monkeypatch.setenv("ANGEL_POSTBACK_AUTH_MODE", "RELAY")
    monkeypatch.setattr(server, "get_hub_runtime", lambda: runtime)
    monkeypatch.setattr(server, "get_settings", lambda: _postback_settings())
    monkeypatch.setattr(
        server,
        "get_broker_account_by_client_code",
        lambda client_code: SimpleNamespace(
            broker_account_id="acc-1", tenant_id="tenant-1"
        ),
    )

    ok = client.post(
        "/webhook/angel/postback",
        json=_postback_payload(),
        headers={"X-Relay-Token": "relay-secret"},
    )
    rejected = client.post(
        "/webhook/angel/postback",
        json=_postback_payload(),
        headers={"X-Angel-Postback-Token": "broker-secret"},
    )

    assert ok.status_code == 200
    assert rejected.status_code == 401


def test_daily_levels_refresh_response(monkeypatch):
    monkeypatch.setattr(server, "refresh_daily_levels_cache", lambda: False)
    monkeypatch.setattr(server, "get_app_runtime", lambda: DummyAppRuntime())
    monkeypatch.setattr(
        server,
        "get_settings",
        lambda: SimpleNamespace(
            enable_multi_hub=False,
            log_level="INFO",
            admin_api_key="test-admin",
        ),
    )
    monkeypatch.setattr(
        importlib.import_module("app.dashboard.auth"),
        "get_settings",
        lambda: SimpleNamespace(admin_api_key="test-admin"),
    )
    with TestClient(server.app) as client:
        resp = client.post(
            "/admin/daily-levels/refresh",
            headers={"X-Admin-Key": "test-admin"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "skipped"}


# ── /readyz endpoint tests ────────────────────────────────────────────────────


def test_readyz_returns_503_when_runtime_not_ready(monkeypatch):
    """/readyz must return HTTP 503 while the readiness latch is False."""
    runtime = DummyAppRuntime()
    runtime.ready = False
    monkeypatch.setattr(server, "get_app_runtime", lambda: runtime)
    monkeypatch.setattr(
        server,
        "get_settings",
        lambda: SimpleNamespace(
            enable_multi_hub=False,
            log_level="INFO",
            admin_api_key="test-admin",
        ),
    )
    monkeypatch.setattr(server, "strategy_switchboard", StrategySwitchboard())
    monkeypatch.setattr(server, "instrument_controller", InstrumentController())
    monkeypatch.setattr(
        importlib.import_module("app.dashboard.auth"),
        "get_settings",
        lambda: SimpleNamespace(admin_api_key="test-admin"),
    )
    with TestClient(server.app, raise_server_exceptions=False) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 503
    payload = resp.json()
    assert payload["ready"] is False
    assert payload["reason"] == "startup_recovery_in_progress"


def test_readyz_returns_503_when_startup_recovery_is_degraded(monkeypatch):
    """/readyz must stay unready when strict startup recovery completed in a degraded state."""
    runtime = DummyAppRuntime()
    runtime.ready = True
    runtime.startup_recovery_state = {
        "status": "degraded",
        "reason": "startup_recovery_degraded_failed_1_unresolved_0",
        "summary": {"failed": 1, "unresolved_active": 0},
    }
    monkeypatch.setattr(server, "get_app_runtime", lambda: runtime)
    monkeypatch.setattr(
        server,
        "get_settings",
        lambda: SimpleNamespace(
            enable_multi_hub=False,
            log_level="INFO",
            admin_api_key="test-admin",
        ),
    )
    monkeypatch.setattr(server, "strategy_switchboard", StrategySwitchboard())
    monkeypatch.setattr(server, "instrument_controller", InstrumentController())
    monkeypatch.setattr(
        importlib.import_module("app.dashboard.auth"),
        "get_settings",
        lambda: SimpleNamespace(admin_api_key="test-admin"),
    )
    with TestClient(server.app, raise_server_exceptions=False) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 503
    payload = resp.json()
    assert payload["ready"] is False
    assert payload["reason"] == "startup_recovery_degraded_failed_1_unresolved_0"
    assert payload["startup_recovery_status"] == "degraded"
    assert payload["startup_recovery_summary"] == {"failed": 1, "unresolved_active": 0}


def test_readyz_returns_503_when_schema_guard_degraded(monkeypatch):
    runtime = DummyAppRuntime()
    runtime.ready = True
    runtime.schema_state = {
        "status": "degraded",
        "checked_at": "2026-03-05T00:00:00Z",
        "missing_tables": ["strategy_config_candidates"],
        "missing_indexes": [],
    }
    monkeypatch.setattr(server, "get_app_runtime", lambda: runtime)
    monkeypatch.setattr(
        server,
        "get_settings",
        lambda: SimpleNamespace(
            enable_multi_hub=False,
            log_level="INFO",
            admin_api_key="test-admin",
        ),
    )
    monkeypatch.setattr(server, "strategy_switchboard", StrategySwitchboard())
    monkeypatch.setattr(server, "instrument_controller", InstrumentController())
    monkeypatch.setattr(
        importlib.import_module("app.dashboard.auth"),
        "get_settings",
        lambda: SimpleNamespace(admin_api_key="test-admin"),
    )

    with TestClient(server.app, raise_server_exceptions=False) as client:
        resp = client.get("/readyz")

    assert resp.status_code == 503
    payload = resp.json()
    assert payload["ready"] is False
    assert payload["reason"] == "schema_guard_degraded"
    assert payload["schema_guard"]["missing_tables"] == ["strategy_config_candidates"]


def test_readyz_returns_503_when_terminal_position_records_have_quantity(monkeypatch):
    runtime = DummyAppRuntime()
    runtime.ready = True
    runtime.worker_running_state = True
    runtime.watchdog_running_state = True
    runtime.leader_lease_state = {
        "enabled": True,
        "owned": True,
        "task_running": True,
    }
    hub_stub = SimpleNamespace(
        registered_runner_count=1,
        running_runner_count=1,
        failed_runner_count=0,
    )
    hub_runtime_stub = SimpleNamespace(
        hub=hub_stub,
        audit_position_avg_price_corruption=lambda: {
            "corrupt_count": 0,
            "samples": [],
            "read_failures": [],
        },
    )

    monkeypatch.setattr(server, "get_app_runtime", lambda: runtime)
    monkeypatch.setattr(server, "get_hub_runtime", lambda: hub_runtime_stub)
    monkeypatch.setattr(
        server,
        "get_boot_config",
        lambda: SimpleNamespace(
            env={"TRADE_MODE": "LIVE"},
            runtime=SimpleNamespace(app_env="production"),
        ),
    )
    monkeypatch.setattr(
        server,
        "get_settings",
        lambda: SimpleNamespace(
            enable_multi_hub=True,
            log_level="INFO",
            admin_api_key="test-admin",
        ),
    )
    from app.orders import position_record_invariants

    monkeypatch.setattr(
        position_record_invariants,
        "count_terminal_nonzero_position_records",
        lambda: 4,
    )
    monkeypatch.setattr(server, "strategy_switchboard", StrategySwitchboard())
    monkeypatch.setattr(server, "instrument_controller", InstrumentController())
    monkeypatch.setattr(
        importlib.import_module("app.dashboard.auth"),
        "get_settings",
        lambda: SimpleNamespace(admin_api_key="test-admin"),
    )

    with TestClient(server.app, raise_server_exceptions=False) as client:
        resp = client.get("/readyz")

    assert resp.status_code == 503
    payload = resp.json()
    assert payload["reason"] == "terminal_position_records_nonzero_net_qty"
    assert payload["terminal_position_records_nonzero_net_qty_count"] == 4


def test_readyz_returns_200_when_runtime_ready_no_multi_hub(monkeypatch):
    """/readyz must return HTTP 200 when ready and multi-hub is disabled."""
    runtime = DummyAppRuntime()
    runtime.ready = True
    monkeypatch.setattr(server, "get_app_runtime", lambda: runtime)
    monkeypatch.setattr(
        server,
        "get_settings",
        lambda: SimpleNamespace(
            enable_multi_hub=False,
            log_level="INFO",
            admin_api_key="test-admin",
        ),
    )
    monkeypatch.setattr(server, "strategy_switchboard", StrategySwitchboard())
    monkeypatch.setattr(server, "instrument_controller", InstrumentController())
    monkeypatch.setattr(
        importlib.import_module("app.dashboard.auth"),
        "get_settings",
        lambda: SimpleNamespace(admin_api_key="test-admin"),
    )
    with TestClient(server.app) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json()["ready"] is True


def test_readyz_returns_503_when_no_runners_are_registered_in_live(monkeypatch):
    """/readyz must stay unready in LIVE until at least one runner is registered."""
    runtime = DummyAppRuntime()
    runtime.ready = True
    runtime.worker_running_state = True
    runtime.watchdog_running_state = True
    runtime.leader_lease_state = {
        "enabled": True,
        "owned": True,
        "task_running": True,
    }

    hub_stub = SimpleNamespace(
        registered_runner_count=0,
        running_runner_count=0,
        failed_runner_count=0,
    )
    hub_runtime_stub = SimpleNamespace(hub=hub_stub)

    monkeypatch.setattr(server, "get_app_runtime", lambda: runtime)
    monkeypatch.setattr(server, "get_hub_runtime", lambda: hub_runtime_stub)
    monkeypatch.setattr(
        server,
        "get_boot_config",
        lambda: SimpleNamespace(
            env={"TRADE_MODE": "LIVE"},
            runtime=SimpleNamespace(app_env="production"),
        ),
    )
    monkeypatch.setattr(
        server,
        "get_settings",
        lambda: SimpleNamespace(
            enable_multi_hub=True,
            log_level="INFO",
            admin_api_key="test-admin",
        ),
    )
    monkeypatch.setattr(server, "strategy_switchboard", StrategySwitchboard())
    monkeypatch.setattr(server, "instrument_controller", InstrumentController())
    monkeypatch.setattr(
        importlib.import_module("app.dashboard.auth"),
        "get_settings",
        lambda: SimpleNamespace(admin_api_key="test-admin"),
    )
    with TestClient(server.app, raise_server_exceptions=False) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 503
    payload = resp.json()
    assert payload["ready"] is False
    assert payload["reason"] == "no_runners_registered"
    assert payload["registered_runner_count"] == 0
    assert payload["running_runner_count"] == 0
    assert payload["failed_runner_count"] == 0


def test_readyz_allows_zero_runners_outside_live(monkeypatch):
    """/readyz may stay green outside LIVE when no runners are registered yet."""
    runtime = DummyAppRuntime()
    runtime.ready = True

    hub_stub = SimpleNamespace(
        registered_runner_count=0,
        running_runner_count=0,
        failed_runner_count=0,
    )
    hub_runtime_stub = SimpleNamespace(hub=hub_stub)

    monkeypatch.setattr(server, "get_app_runtime", lambda: runtime)
    monkeypatch.setattr(server, "get_hub_runtime", lambda: hub_runtime_stub)
    monkeypatch.setattr(
        server,
        "get_boot_config",
        lambda: SimpleNamespace(
            env={"TRADE_MODE": "PAPER"},
            runtime=SimpleNamespace(app_env="local"),
        ),
    )
    monkeypatch.setattr(
        server,
        "get_settings",
        lambda: SimpleNamespace(
            enable_multi_hub=True,
            log_level="INFO",
            admin_api_key="test-admin",
        ),
    )
    monkeypatch.setattr(server, "strategy_switchboard", StrategySwitchboard())
    monkeypatch.setattr(server, "instrument_controller", InstrumentController())
    monkeypatch.setattr(
        importlib.import_module("app.dashboard.auth"),
        "get_settings",
        lambda: SimpleNamespace(admin_api_key="test-admin"),
    )
    with TestClient(server.app) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ready"] is True
    assert payload["registered_runner_count"] == 0
    assert payload["running_runner_count"] == 0
    assert payload["failed_runner_count"] == 0


def test_readyz_returns_503_when_all_runners_failed(monkeypatch):
    """/readyz must return HTTP 503 when runners are registered but none is running."""
    runtime = DummyAppRuntime()
    runtime.ready = True
    runtime.worker_running_state = True
    runtime.watchdog_running_state = True

    hub_stub = SimpleNamespace(
        runner_count=2,
        running_runner_count=0,
        failed_runner_count=2,
    )
    hub_runtime_stub = SimpleNamespace(hub=hub_stub)

    monkeypatch.setattr(server, "get_app_runtime", lambda: runtime)
    monkeypatch.setattr(server, "get_hub_runtime", lambda: hub_runtime_stub)
    monkeypatch.setattr(
        server,
        "get_settings",
        lambda: SimpleNamespace(
            enable_multi_hub=True,
            log_level="INFO",
            admin_api_key="test-admin",
        ),
    )
    monkeypatch.setattr(server, "strategy_switchboard", StrategySwitchboard())
    monkeypatch.setattr(server, "instrument_controller", InstrumentController())
    monkeypatch.setattr(
        importlib.import_module("app.dashboard.auth"),
        "get_settings",
        lambda: SimpleNamespace(admin_api_key="test-admin"),
    )
    with TestClient(server.app, raise_server_exceptions=False) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 503
    payload = resp.json()
    assert payload["ready"] is False
    assert payload["reason"] == "no_runners_running"
    assert payload["runner_count"] == 2
    assert payload["registered_runner_count"] == 2
    assert payload["running_runner_count"] == 0


def test_readyz_returns_503_when_single_registered_runner_is_not_running(monkeypatch):
    """/readyz must fail when one expected runner exists but never reaches running state."""
    runtime = DummyAppRuntime()
    runtime.ready = True
    runtime.worker_running_state = True
    runtime.watchdog_running_state = True
    runtime.leader_lease_state = {
        "enabled": True,
        "owned": True,
        "task_running": True,
    }

    hub_stub = SimpleNamespace(
        registered_runner_count=1,
        running_runner_count=0,
        failed_runner_count=1,
    )
    hub_runtime_stub = SimpleNamespace(hub=hub_stub)

    monkeypatch.setattr(server, "get_app_runtime", lambda: runtime)
    monkeypatch.setattr(server, "get_hub_runtime", lambda: hub_runtime_stub)
    monkeypatch.setattr(
        server,
        "get_boot_config",
        lambda: SimpleNamespace(
            env={"TRADE_MODE": "LIVE"},
            runtime=SimpleNamespace(app_env="production"),
        ),
    )
    monkeypatch.setattr(
        server,
        "get_settings",
        lambda: SimpleNamespace(
            enable_multi_hub=True,
            log_level="INFO",
            admin_api_key="test-admin",
        ),
    )
    monkeypatch.setattr(server, "strategy_switchboard", StrategySwitchboard())
    monkeypatch.setattr(server, "instrument_controller", InstrumentController())
    monkeypatch.setattr(
        importlib.import_module("app.dashboard.auth"),
        "get_settings",
        lambda: SimpleNamespace(admin_api_key="test-admin"),
    )
    with TestClient(server.app, raise_server_exceptions=False) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 503
    payload = resp.json()
    assert payload["ready"] is False
    assert payload["reason"] == "no_runners_running"
    assert payload["registered_runner_count"] == 1
    assert payload["running_runner_count"] == 0
    assert payload["failed_runner_count"] == 1


def test_readyz_returns_503_when_some_registered_runners_are_not_running(monkeypatch):
    """/readyz must fail if registered runners exist but not all of them are actually running."""
    runtime = DummyAppRuntime()
    runtime.ready = True
    runtime.worker_running_state = True
    runtime.watchdog_running_state = True
    runtime.leader_lease_state = {
        "enabled": True,
        "owned": True,
        "task_running": True,
    }

    hub_stub = SimpleNamespace(
        registered_runner_count=2,
        running_runner_count=1,
        failed_runner_count=1,
    )
    hub_runtime_stub = SimpleNamespace(hub=hub_stub)

    monkeypatch.setattr(server, "get_app_runtime", lambda: runtime)
    monkeypatch.setattr(server, "get_hub_runtime", lambda: hub_runtime_stub)
    monkeypatch.setattr(
        server,
        "get_settings",
        lambda: SimpleNamespace(
            enable_multi_hub=True,
            log_level="INFO",
            admin_api_key="test-admin",
        ),
    )
    monkeypatch.setattr(server, "strategy_switchboard", StrategySwitchboard())
    monkeypatch.setattr(server, "instrument_controller", InstrumentController())
    monkeypatch.setattr(
        importlib.import_module("app.dashboard.auth"),
        "get_settings",
        lambda: SimpleNamespace(admin_api_key="test-admin"),
    )
    with TestClient(server.app, raise_server_exceptions=False) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 503
    payload = resp.json()
    assert payload["ready"] is False
    assert payload["reason"] == "runner_startup_incomplete"
    assert payload["registered_runner_count"] == 2
    assert payload["running_runner_count"] == 1
    assert payload["failed_runner_count"] == 1


def test_readyz_returns_200_when_single_runner_is_running(monkeypatch):
    """/readyz must return HTTP 200 when LIVE has one registered runner and it is running."""
    runtime = DummyAppRuntime()
    runtime.ready = True
    runtime.worker_running_state = True
    runtime.watchdog_running_state = True
    runtime.leader_lease_state = {
        "enabled": True,
        "owned": True,
        "task_running": True,
    }

    hub_stub = SimpleNamespace(
        registered_runner_count=1,
        running_runner_count=1,
        failed_runner_count=0,
    )
    hub_runtime_stub = SimpleNamespace(hub=hub_stub)

    monkeypatch.setattr(server, "get_app_runtime", lambda: runtime)
    monkeypatch.setattr(server, "get_hub_runtime", lambda: hub_runtime_stub)
    monkeypatch.setattr(
        server,
        "get_boot_config",
        lambda: SimpleNamespace(
            env={"TRADE_MODE": "LIVE"},
            runtime=SimpleNamespace(app_env="production"),
        ),
    )
    monkeypatch.setattr(
        server,
        "get_settings",
        lambda: SimpleNamespace(
            enable_multi_hub=True,
            log_level="INFO",
            admin_api_key="test-admin",
        ),
    )
    monkeypatch.setattr(server, "strategy_switchboard", StrategySwitchboard())
    monkeypatch.setattr(server, "instrument_controller", InstrumentController())
    monkeypatch.setattr(
        importlib.import_module("app.dashboard.auth"),
        "get_settings",
        lambda: SimpleNamespace(admin_api_key="test-admin"),
    )
    # /readyz's LIVE-mode tradeable-instrument gate calls into the
    # multi-instrument-stream module which expects a real instrument
    # universe; mock it to return 1 so the gate passes deterministically
    # in this unit test (the gate itself has separate coverage).
    monkeypatch.setattr(
        importlib.import_module("app.runners.multi_instrument_stream"),
        "get_active_instrument_count",
        lambda: 1,
    )
    with TestClient(server.app) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ready"] is True
    assert payload["registered_runner_count"] == 1
    assert payload["running_runner_count"] == 1
    assert payload["failed_runner_count"] == 0
    assert payload["leader_lease_status"]["enabled"] is True
    assert payload["leader_lease_status"]["owned"] is True
    assert payload["stream_worker_expected"] is True
    assert payload["stream_worker_running"] is True
    assert payload["watchdog_running"] is True


def test_readyz_returns_503_when_live_leader_lease_is_not_owned(monkeypatch):
    """/readyz must fail closed on a standby stack that did not acquire the shared LIVE lease."""
    runtime = DummyAppRuntime()
    runtime.ready = True
    runtime.leader_lease_state = {
        "enabled": True,
        "owned": False,
        "task_running": False,
        "backend": "postgres",
        "lease_id": "phoenix-live-single-stack",
    }

    monkeypatch.setattr(server, "get_app_runtime", lambda: runtime)
    monkeypatch.setattr(
        server,
        "get_boot_config",
        lambda: SimpleNamespace(
            env={"TRADE_MODE": "LIVE"},
            runtime=SimpleNamespace(app_env="production", disable_stream_worker=False),
        ),
    )
    monkeypatch.setattr(
        server,
        "get_settings",
        lambda: SimpleNamespace(
            enable_multi_hub=False,
            log_level="INFO",
            admin_api_key="test-admin",
        ),
    )
    monkeypatch.setattr(server, "strategy_switchboard", StrategySwitchboard())
    monkeypatch.setattr(server, "instrument_controller", InstrumentController())
    monkeypatch.setattr(
        importlib.import_module("app.dashboard.auth"),
        "get_settings",
        lambda: SimpleNamespace(admin_api_key="test-admin"),
    )
    with TestClient(server.app, raise_server_exceptions=False) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 503
    payload = resp.json()
    assert payload["reason"] == "leader_lease_not_owned"
    assert payload["leader_lease_status"]["backend"] == "postgres"
    assert payload["leader_lease_status"]["lease_id"] == "phoenix-live-single-stack"


def test_readyz_returns_503_when_live_leader_lease_task_is_not_running(monkeypatch):
    """/readyz must fail closed if the LIVE lease renew loop is no longer running."""
    runtime = DummyAppRuntime()
    runtime.ready = True
    runtime.leader_lease_state = {
        "enabled": True,
        "owned": True,
        "task_running": False,
        "backend": "postgres",
    }

    monkeypatch.setattr(server, "get_app_runtime", lambda: runtime)
    monkeypatch.setattr(
        server,
        "get_boot_config",
        lambda: SimpleNamespace(
            env={"TRADE_MODE": "LIVE"},
            runtime=SimpleNamespace(app_env="production", disable_stream_worker=False),
        ),
    )
    monkeypatch.setattr(
        server,
        "get_settings",
        lambda: SimpleNamespace(
            enable_multi_hub=False,
            log_level="INFO",
            admin_api_key="test-admin",
        ),
    )
    monkeypatch.setattr(server, "strategy_switchboard", StrategySwitchboard())
    monkeypatch.setattr(server, "instrument_controller", InstrumentController())
    monkeypatch.setattr(
        importlib.import_module("app.dashboard.auth"),
        "get_settings",
        lambda: SimpleNamespace(admin_api_key="test-admin"),
    )
    with TestClient(server.app, raise_server_exceptions=False) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 503
    payload = resp.json()
    assert payload["reason"] == "leader_lease_task_not_running"


def test_readyz_returns_200_when_runners_are_running(monkeypatch):
    """/readyz must return HTTP 200 when ready and at least one runner is running."""
    runtime = DummyAppRuntime()
    runtime.ready = True

    hub_stub = SimpleNamespace(
        runner_count=2,
        running_runner_count=2,
        failed_runner_count=0,
    )
    hub_runtime_stub = SimpleNamespace(hub=hub_stub)

    monkeypatch.setattr(server, "get_app_runtime", lambda: runtime)
    monkeypatch.setattr(server, "get_hub_runtime", lambda: hub_runtime_stub)
    monkeypatch.setattr(
        server,
        "get_settings",
        lambda: SimpleNamespace(
            enable_multi_hub=True,
            log_level="INFO",
            admin_api_key="test-admin",
        ),
    )
    monkeypatch.setattr(server, "strategy_switchboard", StrategySwitchboard())
    monkeypatch.setattr(server, "instrument_controller", InstrumentController())
    monkeypatch.setattr(
        importlib.import_module("app.dashboard.auth"),
        "get_settings",
        lambda: SimpleNamespace(admin_api_key="test-admin"),
    )
    with TestClient(server.app) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ready"] is True
    assert payload["registered_runner_count"] == 2
    assert payload["running_runner_count"] == 2
    assert payload["failed_runner_count"] == 0


def test_readyz_returns_503_when_live_stream_worker_is_stopped(monkeypatch):
    """/readyz must fail closed in LIVE when the stream worker is expected but not running."""
    runtime = DummyAppRuntime()
    runtime.ready = True
    runtime.worker_running_state = False
    runtime.watchdog_running_state = True
    runtime.leader_lease_state = {
        "enabled": True,
        "owned": True,
        "task_running": True,
    }

    hub_stub = SimpleNamespace(
        registered_runner_count=1,
        running_runner_count=1,
        failed_runner_count=0,
    )
    hub_runtime_stub = SimpleNamespace(hub=hub_stub)

    monkeypatch.setattr(server, "get_app_runtime", lambda: runtime)
    monkeypatch.setattr(server, "get_hub_runtime", lambda: hub_runtime_stub)
    monkeypatch.setattr(
        server,
        "get_boot_config",
        lambda: SimpleNamespace(
            env={"TRADE_MODE": "LIVE"},
            runtime=SimpleNamespace(app_env="production", disable_stream_worker=False),
        ),
    )
    monkeypatch.setattr(
        server,
        "get_settings",
        lambda: SimpleNamespace(
            enable_multi_hub=True,
            log_level="INFO",
            admin_api_key="test-admin",
        ),
    )
    monkeypatch.setattr(server, "strategy_switchboard", StrategySwitchboard())
    monkeypatch.setattr(server, "instrument_controller", InstrumentController())
    monkeypatch.setattr(
        importlib.import_module("app.dashboard.auth"),
        "get_settings",
        lambda: SimpleNamespace(admin_api_key="test-admin"),
    )
    with TestClient(server.app, raise_server_exceptions=False) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 503
    payload = resp.json()
    assert payload["ready"] is False
    assert payload["reason"] == "stream_worker_not_running"
    assert payload["stream_worker_expected"] is True
    assert payload["stream_worker_running"] is False
    assert payload["watchdog_running"] is True


def test_readyz_returns_503_when_live_stream_worker_has_fatal_error(monkeypatch):
    """/readyz must fail closed in LIVE when the stream worker is in a fatal state."""
    runtime = DummyAppRuntime()
    runtime.ready = True
    runtime.worker_running_state = False
    runtime.watchdog_running_state = True
    runtime.worker_fatal_error_state = "BROKER_SECRET_BACKEND=postgres invalid credentials"
    runtime.leader_lease_state = {
        "enabled": True,
        "owned": True,
        "task_running": True,
    }

    hub_stub = SimpleNamespace(
        registered_runner_count=1,
        running_runner_count=1,
        failed_runner_count=0,
    )
    hub_runtime_stub = SimpleNamespace(hub=hub_stub)

    monkeypatch.setattr(server, "get_app_runtime", lambda: runtime)
    monkeypatch.setattr(server, "get_hub_runtime", lambda: hub_runtime_stub)
    monkeypatch.setattr(
        server,
        "get_boot_config",
        lambda: SimpleNamespace(
            env={"TRADE_MODE": "LIVE"},
            runtime=SimpleNamespace(app_env="production", disable_stream_worker=False),
        ),
    )
    monkeypatch.setattr(
        server,
        "get_settings",
        lambda: SimpleNamespace(
            enable_multi_hub=True,
            log_level="INFO",
            admin_api_key="test-admin",
        ),
    )
    monkeypatch.setattr(server, "strategy_switchboard", StrategySwitchboard())
    monkeypatch.setattr(server, "instrument_controller", InstrumentController())
    monkeypatch.setattr(
        importlib.import_module("app.dashboard.auth"),
        "get_settings",
        lambda: SimpleNamespace(admin_api_key="test-admin"),
    )
    with TestClient(server.app, raise_server_exceptions=False) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 503
    payload = resp.json()
    assert payload["ready"] is False
    assert payload["reason"] == "stream_worker_fatal_error"
    assert payload["stream_worker_expected"] is True
    assert payload["stream_worker_running"] is False
    assert payload["watchdog_running"] is True
    assert payload["stream_worker_failure_state"] == "fatal_error"


def test_readyz_returns_503_when_live_stream_watchdog_is_not_running(monkeypatch):
    """/readyz must fail closed when the worker is expected but the watchdog is down."""
    runtime = DummyAppRuntime()
    runtime.ready = True
    runtime.worker_running_state = True
    runtime.watchdog_running_state = False
    runtime.leader_lease_state = {
        "enabled": True,
        "owned": True,
        "task_running": True,
    }

    hub_stub = SimpleNamespace(
        registered_runner_count=1,
        running_runner_count=1,
        failed_runner_count=0,
    )
    hub_runtime_stub = SimpleNamespace(hub=hub_stub)

    monkeypatch.setattr(server, "get_app_runtime", lambda: runtime)
    monkeypatch.setattr(server, "get_hub_runtime", lambda: hub_runtime_stub)
    monkeypatch.setattr(
        server,
        "get_boot_config",
        lambda: SimpleNamespace(
            env={"TRADE_MODE": "LIVE"},
            runtime=SimpleNamespace(app_env="production", disable_stream_worker=False),
        ),
    )
    monkeypatch.setattr(
        server,
        "get_settings",
        lambda: SimpleNamespace(
            enable_multi_hub=True,
            log_level="INFO",
            admin_api_key="test-admin",
        ),
    )
    monkeypatch.setattr(server, "strategy_switchboard", StrategySwitchboard())
    monkeypatch.setattr(server, "instrument_controller", InstrumentController())
    monkeypatch.setattr(
        importlib.import_module("app.dashboard.auth"),
        "get_settings",
        lambda: SimpleNamespace(admin_api_key="test-admin"),
    )
    with TestClient(server.app, raise_server_exceptions=False) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 503
    payload = resp.json()
    assert payload["ready"] is False
    assert payload["reason"] == "stream_watchdog_not_running"
    assert payload["stream_worker_expected"] is True
    assert payload["stream_worker_running"] is True
    assert payload["watchdog_running"] is False


def test_readyz_allows_disabled_live_stream_worker_path(monkeypatch):
    """/readyz may stay green in LIVE when the stream worker is explicitly disabled."""
    runtime = DummyAppRuntime()
    runtime.ready = True
    runtime.worker_running_state = False
    runtime.watchdog_running_state = False
    runtime.leader_lease_state = {
        "enabled": True,
        "owned": True,
        "task_running": True,
    }

    hub_stub = SimpleNamespace(
        registered_runner_count=1,
        running_runner_count=1,
        failed_runner_count=0,
    )
    hub_runtime_stub = SimpleNamespace(hub=hub_stub)

    monkeypatch.setattr(server, "get_app_runtime", lambda: runtime)
    monkeypatch.setattr(server, "get_hub_runtime", lambda: hub_runtime_stub)
    monkeypatch.setattr(
        server,
        "get_boot_config",
        lambda: SimpleNamespace(
            env={"TRADE_MODE": "LIVE"},
            runtime=SimpleNamespace(app_env="production", disable_stream_worker=True),
        ),
    )
    monkeypatch.setattr(
        server,
        "get_settings",
        lambda: SimpleNamespace(
            enable_multi_hub=True,
            log_level="INFO",
            admin_api_key="test-admin",
        ),
    )
    monkeypatch.setattr(server, "strategy_switchboard", StrategySwitchboard())
    monkeypatch.setattr(server, "instrument_controller", InstrumentController())
    monkeypatch.setattr(
        importlib.import_module("app.dashboard.auth"),
        "get_settings",
        lambda: SimpleNamespace(admin_api_key="test-admin"),
    )
    # Mock the LIVE-mode tradeable-instrument gate (covered separately).
    monkeypatch.setattr(
        importlib.import_module("app.runners.multi_instrument_stream"),
        "get_active_instrument_count",
        lambda: 1,
    )
    with TestClient(server.app) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ready"] is True
    assert payload["stream_worker_expected"] is False
    assert payload["stream_worker_running"] is False


# ---------------------------------------------------------------------------
# Issue #27: /readyz sync freshness checks
# ---------------------------------------------------------------------------

def _make_readyz_stub_with_sync_ts(
    *,
    pos_last_ok_ts: str | None,
    ord_last_ok_ts: str | None,
    pos_interval: str = "30",
    ord_interval: str = "90",
    monkeypatch,
    runtime,
    settings=None,
):
    """Build hub/state_store stubs with controllable sync timestamps and monkeypatch server."""
    state_store = StateStore()
    state_store._ensure_account("ACC1")
    if pos_last_ok_ts is not None:
        state_store.update_positions_status(
            "ACC1",
            status="OK",
            last_ok_ts=pos_last_ok_ts,
            last_count=5,
            error_reason=None,
            blocked_ts=None,
            retry_after_seconds=None,
        )
    if ord_last_ok_ts is not None:
        state_store.update_orders_ok_ts("ACC1", last_ok_ts=ord_last_ok_ts)

    hub_stub = SimpleNamespace(
        runner_count=1,
        running_runner_count=1,
        failed_runner_count=0,
        registered_runner_count=1,
        list_runner_ids=lambda: ["ACC1"],
    )
    hub_runtime_stub = SimpleNamespace(hub=hub_stub, state_store=state_store)

    monkeypatch.setenv("POSITION_SYNC_INTERVAL_SECONDS", pos_interval)
    monkeypatch.setenv("ORDERS_SYNC_INTERVAL_SECONDS", ord_interval)
    monkeypatch.setattr(server, "get_app_runtime", lambda: runtime)
    monkeypatch.setattr(server, "get_hub_runtime", lambda: hub_runtime_stub)
    if settings is None:
        settings = SimpleNamespace(
            enable_multi_hub=True,
            log_level="INFO",
            admin_api_key="test-admin",
        )
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    monkeypatch.setattr(server, "strategy_switchboard", StrategySwitchboard())
    monkeypatch.setattr(server, "instrument_controller", InstrumentController())
    monkeypatch.setattr(
        importlib.import_module("app.dashboard.auth"),
        "get_settings",
        lambda: SimpleNamespace(admin_api_key="test-admin"),
    )


def test_readyz_returns_503_when_position_sync_is_stale(monkeypatch):
    """/readyz must return 503 when the most-recent position sync is older than 2x interval."""
    runtime = DummyAppRuntime()
    runtime.ready = True
    stale_ts = datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat()
    fresh_ts = datetime.now(timezone.utc).isoformat()

    _make_readyz_stub_with_sync_ts(
        pos_last_ok_ts=stale_ts,
        ord_last_ok_ts=fresh_ts,
        monkeypatch=monkeypatch,
        runtime=runtime,
    )
    with TestClient(server.app) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 503
    payload = resp.json()
    assert payload["ready"] is False
    assert payload["reason"] == "position_sync_stale"
    # position_sync_age_seconds was removed from the readyz payload in a prior refactor


def test_readyz_returns_503_when_orders_sync_is_stale(monkeypatch):
    """/readyz must return 503 when orders sync has never run or is overdue."""
    runtime = DummyAppRuntime()
    runtime.ready = True
    fresh_ts = datetime.now(timezone.utc).isoformat()

    _make_readyz_stub_with_sync_ts(
        pos_last_ok_ts=fresh_ts,
        ord_last_ok_ts=None,  # never synced
        monkeypatch=monkeypatch,
        runtime=runtime,
    )
    with TestClient(server.app) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 503
    payload = resp.json()
    assert payload["ready"] is False
    assert payload["reason"] == "orders_sync_stale"


# ---------------------------------------------------------------------------
# Issue #247: /readyz must gate on ACTUAL kill-switch divergence only.
#
# Codex P1 from PR #234 surfaced an earlier draft that inverted the gate:
# the non-divergent (else) branch returned 503 while the actual divergent
# case only emitted a warning. Round-2 of that review fixed it to:
#
#   if div.get("divergent"):  ← only the divergent path fails readiness
#       ... 503 ...
#
# These tests pin that contract so a future refactor cannot silently
# re-invert it. They also pin the policy that a divergence-detection
# FAILURE (e.g. hub raises) degrades gracefully (200) rather than
# flipping /readyz red on an unrelated internal error.
# ---------------------------------------------------------------------------


def _make_readyz_live_stub_for_divergence(
    monkeypatch,
    *,
    div_result=None,
    div_raises=False,
    fails_ready_env: str | None = None,
):
    """Configure server module so /readyz reaches the divergence gate in LIVE.

    ``div_result`` is the dict returned by ``compute_kill_switch_divergence``.
    Pass ``div_raises=True`` to make the call raise — this exercises the
    "divergence pre-check failed" path (must NOT 503).
    """
    runtime = DummyAppRuntime()
    runtime.ready = True
    runtime.worker_running_state = True
    runtime.watchdog_running_state = True
    runtime.leader_lease_state = {
        "enabled": True,
        "owned": True,
        "task_running": True,
    }

    def _div_call():
        if div_raises:
            raise RuntimeError("simulated divergence-check failure")
        return dict(div_result or {})

    hub_stub = SimpleNamespace(
        registered_runner_count=1,
        running_runner_count=1,
        failed_runner_count=0,
        list_runner_ids=lambda: [],
    )
    hub_runtime_stub = SimpleNamespace(
        hub=hub_stub,
        compute_kill_switch_divergence=_div_call,
    )

    monkeypatch.setattr(server, "get_app_runtime", lambda: runtime)
    monkeypatch.setattr(server, "get_hub_runtime", lambda: hub_runtime_stub)
    monkeypatch.setattr(
        server,
        "get_boot_config",
        lambda: SimpleNamespace(
            env={"TRADE_MODE": "LIVE"},
            runtime=SimpleNamespace(app_env="production"),
        ),
    )
    monkeypatch.setattr(
        server,
        "get_settings",
        lambda: SimpleNamespace(
            enable_multi_hub=True,
            log_level="INFO",
            admin_api_key="test-admin",
        ),
    )
    monkeypatch.setattr(server, "strategy_switchboard", StrategySwitchboard())
    monkeypatch.setattr(server, "instrument_controller", InstrumentController())
    monkeypatch.setattr(
        importlib.import_module("app.dashboard.auth"),
        "get_settings",
        lambda: SimpleNamespace(admin_api_key="test-admin"),
    )
    # LIVE-mode tradeable-instrument gate calls into the multi-instrument-
    # stream module; mock so the gate passes deterministically here.
    monkeypatch.setattr(
        importlib.import_module("app.runners.multi_instrument_stream"),
        "get_active_instrument_count",
        lambda: 1,
    )

    if fails_ready_env is None:
        monkeypatch.delenv("KILL_SWITCH_DIVERGENCE_FAILS_READY", raising=False)
    else:
        monkeypatch.setenv(
            "KILL_SWITCH_DIVERGENCE_FAILS_READY", fails_ready_env
        )

    return runtime, hub_runtime_stub


def test_readyz_returns_503_when_kill_switch_divergent_in_live(monkeypatch):
    """Issue #247: /readyz must 503 ONLY when divergent=True (silent-bypass scenario).

    Confirms the fixed gate fires on the actual divergent case
    (legacy_active=True but durable_global_active=False).
    """
    _make_readyz_live_stub_for_divergence(
        monkeypatch,
        div_result={
            "divergent": True,
            "legacy_active": True,
            "durable_global_active": False,
            "legacy_reason": "stream_loss",
            "divergence_age_seconds": 12.5,
            "publisher_seen": True,
        },
    )
    with TestClient(server.app, raise_server_exceptions=False) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 503
    payload = resp.json()
    assert payload["ready"] is False
    assert payload["reason"].startswith("kill_switch_divergence")
    assert "legacy=True durable_global=False" in payload["reason"]
    assert payload["kill_switch_divergence"] is True
    assert payload["kill_switch_legacy_active"] is True


def test_readyz_returns_200_when_kill_switch_not_divergent_in_live(monkeypatch):
    """Issue #247: the non-divergent (healthy) case must NOT trigger the gate.

    This is the regression test for the original Codex finding: an earlier
    draft of PR #234 inverted the condition and 503ed every healthy LIVE
    instance. The reason field must NOT be kill_switch_divergence here.
    """
    _make_readyz_live_stub_for_divergence(
        monkeypatch,
        div_result={
            "divergent": False,
            "legacy_active": False,
            "durable_global_active": False,
            "publisher_seen": True,
        },
    )
    with TestClient(server.app, raise_server_exceptions=False) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["ready"] is True
    assert payload.get("kill_switch_divergence") is False
    # Critical: the kill_switch_divergence reason must NEVER appear in the
    # non-divergent case.
    assert "kill_switch_divergence" not in str(payload.get("reason", ""))


def test_readyz_returns_200_when_divergence_check_raises_in_live(monkeypatch):
    """Issue #247: divergence pre-check failures must degrade gracefully (200).

    Per policy: /readyz must remain fail-CLOSED on GENUINE safety signals
    (active kill switch, divergent state) but should NOT flip not-ready on
    transient signal-collection errors that don't indicate an actual problem.
    A hub-side exception in ``compute_kill_switch_divergence()`` should be
    logged and ignored, not promoted to a 503.
    """
    _make_readyz_live_stub_for_divergence(monkeypatch, div_raises=True)
    with TestClient(server.app, raise_server_exceptions=False) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["ready"] is True
    assert "kill_switch_divergence" not in str(payload.get("reason", ""))


def test_readyz_does_not_503_on_divergence_when_fails_ready_disabled(monkeypatch):
    """Issue #247: KILL_SWITCH_DIVERGENCE_FAILS_READY=false must opt out of the 503.

    Even with divergent=True, when the operator has explicitly opted out
    via env (e.g. during a managed bridge convergence window) /readyz
    surfaces the signal in the payload but does not flip not-ready.
    """
    _make_readyz_live_stub_for_divergence(
        monkeypatch,
        div_result={
            "divergent": True,
            "legacy_active": True,
            "durable_global_active": False,
        },
        fails_ready_env="false",
    )
    with TestClient(server.app, raise_server_exceptions=False) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["ready"] is True
    # Divergence signal is still surfaced (operator visibility) — only the
    # readiness gate itself is suppressed.
    assert payload.get("kill_switch_divergence") is True
