import importlib
from concurrent.futures import ThreadPoolExecutor

from app.brokers.base import Balance, OrderResponse, OrderStatus, Position, ProductType
from app.data.state_store import StateStore


def test_import_module_succeeds():
    mod = importlib.import_module("app.data.state_store")
    assert hasattr(mod, "StateStore")


def test_state_store_round_trips_state():
    store = StateStore()
    account_id = "acct-1"

    bal = Balance(available=1000.0, utilized=200.0, total=1200.0)
    store.set_balance(account_id, bal)
    assert store.get_balance(account_id) == bal

    positions = [
        Position(symbol="ABC", quantity=1, avg_price=100.0, product_type=ProductType.DELIVERY)
    ]
    store.set_positions(account_id, positions)
    got_positions = store.get_positions(account_id)
    assert len(got_positions) == 1
    assert got_positions[0].symbol == "ABC"
    assert got_positions[0].quantity == 1
    got_positions.append(Position(symbol="MUTATED", quantity=0, avg_price=0, product_type=ProductType.DELIVERY))
    assert len(store.get_positions(account_id)) == 1  # defensive copy

    resp = OrderResponse(broker_order_id="oid", status="ok", message="done")
    store.set_last_order_response(account_id, resp)
    assert store.get_last_order_response(account_id) == resp


def _mk_order(i: int) -> OrderStatus:
    return OrderStatus(
        order_id=f"OID{i}",
        symbol="NIFTY17FEB2625750CE",
        side="BUY",
        status="OPEN",
        order_type="MARKET",
        product_type="INTRADAY",
        quantity=1,
        filled_quantity=0,
        average_price=None,
        price=None,
        exchange="NFO",
        updated_at=f"2026-02-22T11:{i % 60:02d}:00+00:00",
    )


def test_state_store_set_orders_caps_per_account_window():
    store = StateStore(max_orders_per_account=500)
    account_id = "acct-2"

    orders = [_mk_order(i) for i in range(520)]
    store.set_orders(account_id, orders)
    got = store.get_orders(account_id)

    assert len(got) == 500
    assert got[0].order_id == "OID20"
    assert got[-1].order_id == "OID519"


def test_state_store_upsert_order_caps_and_keeps_recent_updates():
    store = StateStore(max_orders_per_account=500)
    account_id = "acct-3"

    for i in range(520):
        store.upsert_order(account_id, _mk_order(i))

    got = store.get_orders(account_id)
    assert len(got) == 500
    assert got[0].order_id == "OID20"
    assert got[-1].order_id == "OID519"

    refreshed = _mk_order(100)
    refreshed.status = "COMPLETE"
    store.upsert_order(account_id, refreshed)
    got = store.get_orders(account_id)

    assert len(got) == 500
    assert got[-1].order_id == "OID100"
    assert got[-1].status == "COMPLETE"


def test_state_store_handles_concurrent_reads_and_updates():
    store = StateStore(max_orders_per_account=500)
    account_id = "acct-threadsafe"

    def _worker(i: int) -> None:
        store.set_balance(
            account_id,
            Balance(available=1000.0 + i, utilized=float(i), total=1000.0 + i),
        )
        store.upsert_order(account_id, _mk_order(i))
        store.set_positions(
            account_id,
            [
                Position(
                    symbol=f"SYM{i % 5}",
                    quantity=i + 1,
                    avg_price=100.0 + i,
                    product_type=ProductType.INTRADAY,
                )
            ],
        )
        store.set_strategy_signal_valid(account_id, "s1", i % 2 == 0)
        _ = store.get_balance(account_id)
        _ = store.get_orders(account_id)
        _ = store.get_positions(account_id)
        _ = store.get_strategy_signal_valid(account_id, "s1")

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(_worker, range(1200)))

    orders = store.get_orders(account_id)
    assert len(orders) <= 500
    assert orders[-1].order_id.startswith("OID")
    assert store.get_balance(account_id) is not None
    assert store.get_positions(account_id)
