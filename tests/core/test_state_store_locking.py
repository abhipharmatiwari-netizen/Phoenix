from concurrent.futures import ThreadPoolExecutor
import threading

from app.brokers.base import Balance, OrderStatus
from app.data.state_store import StateStore


def _order(i: int) -> OrderStatus:
    return OrderStatus(
        order_id=f"OID-{i}",
        symbol="SBIN-EQ",
        side="BUY",
        status="OPEN",
        order_type="MARKET",
        product_type="INTRADAY",
        quantity=1,
        filled_quantity=0,
        average_price=None,
        price=None,
        exchange="NSE",
        updated_at=f"2026-03-05T10:{i % 60:02d}:00+00:00",
    )


def test_state_store_different_accounts_are_not_blocked_by_other_account_lock():
    store = StateStore()
    lock_a, _ = store._ensure_account("acct-a")
    lock_a.acquire()
    done = threading.Event()

    def _worker() -> None:
        try:
            store.set_balance("acct-b", Balance(available=1.0, utilized=0.0, total=1.0))
        finally:
            done.set()

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    assert done.wait(timeout=0.5)
    lock_a.release()
    thread.join(timeout=1.0)


def test_state_store_first_access_race_safe_lock_count_matches_account_count():
    store = StateStore()
    account_count = 32
    errors: list[Exception] = []

    def _worker(i: int) -> None:
        account_id = f"acct-{i % account_count}"
        try:
            store.set_balance(
                account_id,
                Balance(available=float(i), utilized=0.0, total=float(i)),
            )
            store.upsert_order(account_id, _order(i))
            _ = store.get_balance(account_id)
            _ = store.get_orders(account_id)
        except Exception as exc:  # pragma: no cover - debugging aid
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=24) as pool:
        list(pool.map(_worker, range(2000)))

    assert not errors
    assert len(store._accounts) == account_count
    assert len(store._locks) == account_count
