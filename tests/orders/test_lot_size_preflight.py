from app.brokers.base import OrderRequest, OrderSide, OrderType, ProductType, TimeInForce
from app.core.dashboard_bus import dashboard_bus
from app.orders.router import _preflight_order_quantity


def _base_order(symbol: str, qty: int) -> OrderRequest:
    return OrderRequest(
        symbol=symbol,
        quantity=qty,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
    )


def test_preflight_converts_lots_to_broker_qty():
    original = dict(getattr(dashboard_bus, "_instrument_meta", {}))
    try:
        dashboard_bus.set_instrument_meta(
            {
                "TEST_LABEL": {
                    "symbol": "TESTSYM",
                    "token": "123",
                    "lot_size": 25,
                    "underlying": "TEST",
                }
            }
        )
        order_req = _base_order("TESTSYM", 1)
        ok, reason, lots, lot_size, broker_qty, underlying = _preflight_order_quantity(order_req)
        assert ok is True
        assert reason == "ok"
        assert lots == 1
        assert lot_size == 25
        assert broker_qty == 25
        assert underlying == "TEST"
    finally:
        dashboard_bus.set_instrument_meta(original)


def test_preflight_rejects_missing_lot_size():
    original = dict(getattr(dashboard_bus, "_instrument_meta", {}))
    try:
        dashboard_bus.set_instrument_meta(
            {"TEST_LABEL": {"symbol": "ZZZ_UNMATCHED", "token": "999"}}
        )
        order_req = _base_order("ZZZ_UNMATCHED", 1)
        ok, reason, lots, lot_size, broker_qty, underlying = _preflight_order_quantity(order_req)
        assert ok is False
        assert reason == "lot_size_missing"
        assert lots == 1
        assert lot_size is None
        assert broker_qty is None
        assert underlying is None
    finally:
        dashboard_bus.set_instrument_meta(original)
