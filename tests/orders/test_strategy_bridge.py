import importlib
import types

import pytest

from app.brokers.base import (
    OrderRequest,
    OrderResponse,
    OrderSide,
    OrderType,
    ProductType,
    TimeInForce,
)
from app.core.identifiers import StrategyId
from app.core.signal_metrics import get_signal_summary_metrics
from app.hub.routing_table import HubRoute
from app.orders.replay_context import isolated_replay_flag, isolated_replay_order_sink
import app.runtime.app_runtime as _app_runtime_mod

MODULE_PATH = "app.orders.strategy_bridge"

_READY_RUNTIME = types.SimpleNamespace(ready=True)
_NOT_READY_RUNTIME = types.SimpleNamespace(ready=False)


def _order_req() -> OrderRequest:
    return OrderRequest(
        symbol="NG_FUT",
        quantity=1,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
    )


def test_import_module_succeeds():
    mod = importlib.import_module(MODULE_PATH)
    assert isinstance(mod, types.ModuleType)
    assert hasattr(mod, "place_order_via_bridge")


def test_bridge_router_path_counts_fired_and_submitted(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)
    metrics = get_signal_summary_metrics()
    metrics.reset_for_tests()

    async def _submit(**_kwargs):
        return "hub-order-1", OrderResponse(
            broker_order_id="OID-1",
            status="PLACED",
            message="ok",
            filled_quantity=0,
            average_price=None,
        )

    runtime = types.SimpleNamespace(
        order_router=types.SimpleNamespace(submit_order=_submit)
    )
    routes = [HubRoute(tenant_id="t1", broker_account_id="b1")]
    monkeypatch.setattr(_app_runtime_mod, "get_app_runtime", lambda: _READY_RUNTIME)
    monkeypatch.setattr(mod, "get_hub_runtime", lambda: runtime)
    monkeypatch.setattr(
        mod,
        "get_global_routing_table",
        lambda: types.SimpleNamespace(get_routes_for_strategy=lambda _sid: routes),
    )

    response = mod.place_order_via_bridge(
        strategy_id=StrategyId("ema20_strategy"),
        order_req=_order_req(),
    )
    snap = metrics.snapshot()
    assert response.status == "PLACED"
    assert snap.total_signals_fired == 1
    assert snap.total_orders_submitted == 1


def test_bridge_router_rejected_counts_fired_only(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)
    metrics = get_signal_summary_metrics()
    metrics.reset_for_tests()

    async def _submit(**_kwargs):
        return "hub-order-2", OrderResponse(
            broker_order_id="",
            status="REJECTED",
            message="blocked",
            filled_quantity=0,
            average_price=None,
        )

    runtime = types.SimpleNamespace(
        order_router=types.SimpleNamespace(submit_order=_submit)
    )
    routes = [HubRoute(tenant_id="t1", broker_account_id="b1")]
    monkeypatch.setattr(_app_runtime_mod, "get_app_runtime", lambda: _READY_RUNTIME)
    monkeypatch.setattr(mod, "get_hub_runtime", lambda: runtime)
    monkeypatch.setattr(
        mod,
        "get_global_routing_table",
        lambda: types.SimpleNamespace(get_routes_for_strategy=lambda _sid: routes),
    )

    response = mod.place_order_via_bridge(
        strategy_id=StrategyId("ema20_strategy"),
        order_req=_order_req(),
    )
    snap = metrics.snapshot()
    assert response.status == "REJECTED"
    assert snap.total_signals_fired == 1
    assert snap.total_orders_submitted == 0


def test_bridge_rejects_when_no_routes(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)
    metrics = get_signal_summary_metrics()
    metrics.reset_for_tests()

    runtime = types.SimpleNamespace(
        order_router=types.SimpleNamespace(submit_order=lambda **_kwargs: None)
    )
    monkeypatch.setattr(_app_runtime_mod, "get_app_runtime", lambda: _READY_RUNTIME)
    monkeypatch.setattr(mod, "get_hub_runtime", lambda: runtime)
    monkeypatch.setattr(
        mod,
        "get_global_routing_table",
        lambda: types.SimpleNamespace(get_routes_for_strategy=lambda _sid: []),
    )

    with pytest.raises(ValueError):
        mod.place_order_via_bridge(
            strategy_id=StrategyId("ema20_strategy"),
            order_req=_order_req(),
        )

    snap = metrics.snapshot()
    assert snap.total_signals_fired == 1
    assert snap.total_orders_submitted == 0


def test_bridge_blocks_order_when_runtime_not_ready(monkeypatch):
    """Order submission must be blocked while the runtime readiness latch is False."""
    mod = importlib.import_module(MODULE_PATH)
    metrics = get_signal_summary_metrics()
    metrics.reset_for_tests()

    def _boom():
        raise AssertionError("get_hub_runtime must not be called when runtime is not ready")

    monkeypatch.setattr(_app_runtime_mod, "get_app_runtime", lambda: _NOT_READY_RUNTIME)
    monkeypatch.setattr(mod, "get_hub_runtime", _boom)

    with pytest.raises(RuntimeError, match="runtime not ready"):
        mod.place_order_via_bridge(
            strategy_id=StrategyId("ema20_strategy"),
            order_req=_order_req(),
        )

    # signal_fired is counted before the readiness gate so callers can observe the drop
    snap = metrics.snapshot()
    assert snap.total_signals_fired == 1
    assert snap.total_orders_submitted == 0


def test_bridge_replay_bypasses_readiness_gate(monkeypatch):
    """Replay path must not be gated on runtime readiness — it bypasses live infra."""
    mod = importlib.import_module(MODULE_PATH)
    metrics = get_signal_summary_metrics()
    metrics.reset_for_tests()

    class _ReplaySink:
        def place_order(self, **kwargs):
            return OrderResponse(
                broker_order_id="REPLAY-RG",
                status="COMPLETE",
                message="replay_local_fill",
                filled_quantity=1,
                average_price=100.0,
                execution_mode="replay_local",
                virtual=True,
            )

    def _boom_runtime():
        raise AssertionError("get_app_runtime must not be called during replay")

    monkeypatch.setattr(_app_runtime_mod, "get_app_runtime", _boom_runtime)
    monkeypatch.setattr(
        mod,
        "get_hub_runtime",
        lambda: (_ for _ in ()).throw(AssertionError("get_hub_runtime must not be called during replay")),
    )

    with isolated_replay_order_sink(_ReplaySink()):
        response = mod.place_order_via_bridge(
            strategy_id=StrategyId("ema20_strategy"),
            order_req=_order_req(),
        )

    assert response.execution_mode == "replay_local"
    snap = metrics.snapshot()
    assert snap.total_signals_fired == 1
    assert snap.total_orders_submitted == 1


def test_bridge_replay_short_circuits_before_hub_runtime(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)
    metrics = get_signal_summary_metrics()
    metrics.reset_for_tests()

    class _ReplaySink:
        def __init__(self) -> None:
            self.calls = []

        def place_order(self, **kwargs):
            self.calls.append(kwargs)
            return OrderResponse(
                broker_order_id="REPLAY-1",
                status="COMPLETE",
                message="replay_local_fill",
                filled_quantity=1,
                average_price=123.45,
                execution_mode="replay_local",
                virtual=True,
            )

    sink = _ReplaySink()

    def _boom():
        raise AssertionError("get_hub_runtime must not be called during replay")

    monkeypatch.setattr(mod, "get_hub_runtime", _boom)

    with isolated_replay_order_sink(sink):
        response = mod.place_order_via_bridge(
            strategy_id=StrategyId("ema20_strategy"),
            order_req=_order_req(),
        )

    snap = metrics.snapshot()
    assert response.execution_mode == "replay_local"
    assert response.virtual is True
    assert len(sink.calls) == 1
    assert snap.total_signals_fired == 1
    assert snap.total_orders_submitted == 1


def test_bridge_replay_flag_without_sink_fails_fast(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)

    def _boom():
        raise AssertionError("get_hub_runtime must not be called when replay is isolated")

    monkeypatch.setattr(mod, "get_hub_runtime", _boom)

    with isolated_replay_flag(True):
        with pytest.raises(RuntimeError, match="no replay-local order sink is active"):
            mod.place_order_via_bridge(
                strategy_id=StrategyId("ema20_strategy"),
                order_req=_order_req(),
            )
