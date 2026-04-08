from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.brokers.base import (
    OrderPurpose,
    OrderResponse,
    OrderSide,
    ProductType,
)
from app.core.dashboard_bus import dashboard_bus
from app.core.identifiers import BrokerAccountId, StrategyId, TenantId
from app.dashboard.auth import AdminContext, AdminRole
from app.dashboard.admin_routes import (
    BreakGlassFlattenRequest,
    break_glass_flatten,
    router,
)
from app.orders.position_ownership import (
    PositionOwnershipStore,
    derive_contract_key_from_position,
)
from app.orders.router import OrderRouter


def _build_settings() -> SimpleNamespace:
    return SimpleNamespace(
        default_time_zone="Asia/Kolkata",
        enable_capital_checks=False,
        capital_auto_resize_enabled=False,
        enable_risk_checks=False,
        enable_profit_checks=False,
        position_ownership_enabled=True,
        position_ownership_unknown_mode="block_entries",
        position_ownership_allow_system_exit=True,
        order_router_enforce_idempotency=False,
        order_router_enforce_global_kill_switch=False,
    )


def _sample_position(*, token: str = "12345") -> dict[str, object]:
    return {
        "symbol": "NIFTY27MAR2522500CE",
        "quantity": 50,
        "product_type": "INTRADAY",
        "exchange": "NFO",
        "symbol_token": token,
        "lot_size": 25,
        "avg_price": 100.0,
    }


def _sample_payload() -> BreakGlassFlattenRequest:
    return BreakGlassFlattenRequest(
        tenant_id="tenant-1",
        broker_account_id="account-1",
        underlying="NIFTY",
        expiry="27MAR25",
        strike="22500",
        option_right="CE",
        product_type="INTRADAY",
        reason="Emergency flatten due to broker issue",
    )


def _seed_owned_contract(
    store: PositionOwnershipStore,
    position: dict[str, object],
) -> None:
    contract_key, reason = derive_contract_key_from_position(position)
    assert contract_key is not None, reason
    decision = store.try_acquire(
        tenant_id=TenantId("tenant-1"),
        broker_account_id=BrokerAccountId("account-1"),
        contract_key=contract_key,
        strategy_id=StrategyId("strategy-a"),
        is_exit_order=False,
        unknown_mode="block_entries",
    )
    assert decision.allowed is True
    store.observe_fill_progress(
        tenant_id=TenantId("tenant-1"),
        broker_account_id=BrokerAccountId("account-1"),
        contract_key=contract_key,
        strategy_id=StrategyId("strategy-a"),
        is_exit_order=False,
        filled_qty=1,
    )


def _build_runtime(monkeypatch, *, positions: list[dict[str, object]]):
    audit_events: list[dict] = []
    runner = MagicMock()
    runner.is_running = True
    runner.place_order = AsyncMock(
        return_value=OrderResponse(
            broker_order_id="OID-BG-1",
            status="OPEN",
            message="accepted",
            filled_quantity=0,
            average_price=None,
        )
    )
    hub = MagicMock()
    hub.get_runner.return_value = runner

    state_store = MagicMock()
    state_store.get_balance.return_value = None
    state_store.get_positions.return_value = positions

    settings = _build_settings()
    monkeypatch.setattr("app.orders.router.get_settings", lambda: settings)
    monkeypatch.setattr(
        "app.orders.router.get_runtime_settings",
        lambda **kwargs: kwargs["base_settings"],
    )

    store = PositionOwnershipStore()
    router_obj = OrderRouter(
        hub=hub,
        capital_engine=None,
        risk_engine=None,
        profit_engine=None,
        state_store=state_store,
        position_ownership_store=store,
    )
    real_submit = router_obj.submit_order
    router_obj.submit_order = AsyncMock(side_effect=real_submit)

    runtime = SimpleNamespace(
        position_ownership_store=store,
        order_router=router_obj,
        hub=hub,
        state_store=state_store,
    )
    monkeypatch.setattr("app.dashboard.admin_routes.get_hub_runtime", lambda: runtime)
    monkeypatch.setattr("app.dashboard.admin_routes.check_rate_limit", lambda _request: None)

    def _capture_audit_event(**kwargs):
        audit_events.append(dict(kwargs))
        return dict(kwargs)

    monkeypatch.setattr("app.dashboard.admin_routes.emit_audit_event", _capture_audit_event)
    return runtime, store, runner, audit_events


@pytest.fixture(autouse=True)
def _restore_dashboard_meta():
    original_meta = dict(getattr(dashboard_bus, "_instrument_meta", {}))
    yield
    dashboard_bus.set_instrument_meta(original_meta)


class TestBreakGlassRequestModel:
    def test_valid_request(self):
        req = _sample_payload()
        assert req.tenant_id == "tenant-1"
        assert req.reason == "Emergency flatten due to broker issue"

    def test_reason_required(self):
        req = _sample_payload()
        req.reason = ""
        assert req.reason == ""


class TestBreakGlassEndpointExists:
    def test_endpoint_registered(self):
        routes = [r.path for r in router.routes]
        assert "/admin/break-glass/flatten" in routes

    def test_endpoint_method_is_post(self):
        for route_obj in router.routes:
            if getattr(route_obj, "path", None) == "/admin/break-glass/flatten":
                assert "POST" in route_obj.methods
                break
        else:
            pytest.fail("break-glass/flatten route not found")


class TestBreakGlassRuntime:
    def test_break_glass_flatten_routes_real_exit_and_updates_ownership(
        self,
        monkeypatch,
    ):
        position = _sample_position()
        dashboard_bus.set_instrument_meta(
            {
                "NIFTY-22500-CE": {
                    "symbol": position["symbol"],
                    "token": position["symbol_token"],
                    "lot_size": 25,
                    "underlying": "NIFTY",
                    "expiry": "2025-03-27",
                    "strike": "22500",
                    "kind": "CE",
                    "exchange": "NFO",
                }
            }
        )
        runtime, store, runner, audit_events = _build_runtime(
            monkeypatch,
            positions=[position],
        )
        _seed_owned_contract(store, position)

        request = MagicMock(headers={"X-Request-Id": "req-break-glass-1"})
        result = break_glass_flatten(
            request=request,
            payload=_sample_payload(),
            ctx=AdminContext(caller="ops-admin", role=AdminRole.ADMIN),
        )

        submit_kwargs = runtime.order_router.submit_order.await_args.kwargs
        submitted_order = submit_kwargs["order_req"]
        assert submit_kwargs["tenant_id"] == "tenant-1"
        assert submit_kwargs["broker_account_id"] == "account-1"
        assert str(submit_kwargs["strategy_id"]) == "break_glass_flatten"
        assert submitted_order.symbol == "NIFTY27MAR2522500CE"
        assert submitted_order.quantity == 2
        assert submitted_order.side == OrderSide.SELL
        assert submitted_order.exchange == "NFO"
        assert submitted_order.symbol_token == "12345"
        assert submitted_order.product_type == ProductType.INTRADAY
        assert submitted_order.purpose == OrderPurpose.EXIT
        assert submitted_order.position_ownership_bypass is True
        assert submitted_order.exit_reason == "BREAK_GLASS"

        broker_order_req = runner.place_order.await_args.args[0]
        assert broker_order_req.symbol == "NIFTY27MAR2522500CE"
        assert broker_order_req.quantity == 50
        assert broker_order_req.side == OrderSide.SELL
        assert broker_order_req.exchange == "NFO"
        assert broker_order_req.symbol_token == "12345"

        assert result["hub_order_id"]
        assert result["order"]["broker_order_id"] == "OID-BG-1"
        assert result["order"]["status"] == "OPEN"
        assert result["order"]["requested_quantity"] == 50
        assert result["position"]["requested_lots"] == 2
        assert result["position"]["broker_units"] == 50
        assert result["position"]["exit_side"] == "SELL"
        assert result["ownership"]["state"] == "RELEASING"
        assert result["ownership"]["owner_strategy_id"] == "strategy-a"
        assert result["ownership"]["break_glass_override_id"].startswith("break_glass_")
        assert "break_glass_override:" in result["ownership"]["state_reason"]

        assert audit_events
        audit_after = audit_events[-1]["after"]
        assert audit_after["hub_order_id"] == result["hub_order_id"]
        assert audit_after["order"]["status"] == "OPEN"
        assert audit_after["ownership"]["state"] == "RELEASING"

    def test_break_glass_flatten_rejects_missing_live_position(self, monkeypatch):
        dashboard_bus.set_instrument_meta({})
        runtime, _store, _runner, audit_events = _build_runtime(
            monkeypatch,
            positions=[],
        )

        request = MagicMock(headers={})
        with pytest.raises(HTTPException) as exc_info:
            break_glass_flatten(
                request=request,
                payload=_sample_payload(),
                ctx=AdminContext(caller="ops-admin", role=AdminRole.ADMIN),
            )

        assert exc_info.value.status_code == 404
        assert exc_info.value.detail["error"] == "break_glass_position_not_found"
        assert runtime.order_router.submit_order.await_count == 0
        assert audit_events[-1]["after"]["error"] == "break_glass_position_not_found"

    def test_break_glass_flatten_rejects_ambiguous_live_positions(self, monkeypatch):
        position_a = _sample_position(token="12345")
        position_b = dict(position_a)
        position_b["symbol_token"] = "67890"
        dashboard_bus.set_instrument_meta(
            {
                "NIFTY-22500-CE-A": {
                    "symbol": position_a["symbol"],
                    "token": position_a["symbol_token"],
                    "lot_size": 25,
                    "underlying": "NIFTY",
                    "expiry": "2025-03-27",
                    "strike": "22500",
                    "kind": "CE",
                    "exchange": "NFO",
                },
                "NIFTY-22500-CE-B": {
                    "symbol": position_b["symbol"],
                    "token": position_b["symbol_token"],
                    "lot_size": 25,
                    "underlying": "NIFTY",
                    "expiry": "2025-03-27",
                    "strike": "22500",
                    "kind": "CE",
                    "exchange": "NFO",
                },
            }
        )
        runtime, _store, _runner, _audit_events = _build_runtime(
            monkeypatch,
            positions=[position_a, position_b],
        )

        request = MagicMock(headers={})
        with pytest.raises(HTTPException) as exc_info:
            break_glass_flatten(
                request=request,
                payload=_sample_payload(),
                ctx=AdminContext(caller="ops-admin", role=AdminRole.ADMIN),
            )

        assert exc_info.value.status_code == 409
        assert exc_info.value.detail["error"] == "break_glass_position_ambiguous"
        assert runtime.order_router.submit_order.await_count == 0

    def test_break_glass_flatten_rejects_matching_position_missing_runtime_fields(
        self,
        monkeypatch,
    ):
        position = _sample_position(token="")
        dashboard_bus.set_instrument_meta(
            {
                "NIFTY-22500-CE": {
                    "symbol": position["symbol"],
                    "lot_size": 25,
                    "underlying": "NIFTY",
                    "expiry": "2025-03-27",
                    "strike": "22500",
                    "kind": "CE",
                    "exchange": "NFO",
                }
            }
        )
        runtime, _store, _runner, _audit_events = _build_runtime(
            monkeypatch,
            positions=[position],
        )

        request = MagicMock(headers={})
        with pytest.raises(HTTPException) as exc_info:
            break_glass_flatten(
                request=request,
                payload=_sample_payload(),
                ctx=AdminContext(caller="ops-admin", role=AdminRole.ADMIN),
            )

        assert exc_info.value.status_code == 409
        assert (
            exc_info.value.detail["error"]
            == "break_glass_position_missing_runtime_fields"
        )
        assert exc_info.value.detail["reason"] == "symbol_token_missing"
        assert runtime.order_router.submit_order.await_count == 0
