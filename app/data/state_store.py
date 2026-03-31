"""
In-memory per-account state for the multi-tenant hub.

StateStore tracks:
- Last known Balance per broker account.
- Last known Positions per broker account.
- (Later) last known OrderResponse per hub_order_id or broker_order_id.

This is a hub-scoped singleton in practice, injected into AccountRunners
and OrderRouter.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional
import logging
import os
import threading

from app.brokers.base import Balance, OrderResponse, OrderStatus, Position
from app.core.identifiers import BrokerAccountId

logger = logging.getLogger(__name__)
_DEFAULT_MAX_ORDERS_PER_ACCOUNT = 1000
_MIN_MAX_ORDERS_PER_ACCOUNT = 500
_MAX_MAX_ORDERS_PER_ACCOUNT = 2000


# Per-account in-memory state snapshot.
@dataclass
class AccountState:
    balance: Optional[Balance] = None
    positions: List[Position] = field(default_factory=list)
    # Best-effort "entry time" for positions derived from the first time we saw
    # a position during polling. Keyed by (symbol|product_type) so it survives
    # refreshes until the position disappears.
    positions_first_seen_ts: Dict[str, str] = field(default_factory=dict)
    orders: List[OrderStatus] = field(default_factory=list)
    order_snapshot: List[OrderStatus] = field(default_factory=list)
    last_order_response: Optional[OrderResponse] = None
    positions_status: Optional[str] = None
    positions_last_ok_ts: Optional[str] = None
    positions_last_count: Optional[int] = None
    positions_error_reason: Optional[str] = None
    positions_blocked_ts: Optional[str] = None
    positions_retry_after_seconds: Optional[float] = None
    account_signal_valid: Optional[bool] = None
    strategy_signal_valid: Dict[str, bool] = field(default_factory=dict)


class StateStore:
    """
    Simple in-memory store keyed by BrokerAccountId.

    This is **not** persistent; it is meant for:
    - dashboards (balances/positions for tenants),
    - OrderRouter (capital/risk checks using up-to-date account state),
    - debugging.
    """

    # Initialize the in-memory account map.
    def __init__(self, *, max_orders_per_account: Optional[int] = None) -> None:
        self._registry_lock = threading.Lock()
        self._locks: Dict[BrokerAccountId, threading.RLock] = {}
        self._accounts: Dict[BrokerAccountId, AccountState] = {}
        self._max_orders_per_account = self._resolve_max_orders_per_account(
            max_orders_per_account
        )

    @staticmethod
    def _clone(value):
        try:
            return copy.deepcopy(value)
        except Exception:
            return value

    @staticmethod
    def _resolve_max_orders_per_account(value: Optional[int]) -> int:
        raw: object
        if value is None:
            raw = os.getenv(
                "STATE_STORE_MAX_ORDERS_PER_ACCOUNT",
                str(_DEFAULT_MAX_ORDERS_PER_ACCOUNT),
            )
        else:
            raw = value
        try:
            parsed = int(raw)
        except (TypeError, ValueError):
            parsed = _DEFAULT_MAX_ORDERS_PER_ACCOUNT
        return min(_MAX_MAX_ORDERS_PER_ACCOUNT, max(_MIN_MAX_ORDERS_PER_ACCOUNT, parsed))

    def _cap_orders(self, orders: List[OrderStatus]) -> List[OrderStatus]:
        if len(orders) <= self._max_orders_per_account:
            return list(orders)
        return list(orders[-self._max_orders_per_account :])

    # Get or create per-account lock + state atomically.
    def _ensure_account(
        self, account_id: BrokerAccountId
    ) -> tuple[threading.RLock, AccountState]:
        with self._registry_lock:
            lock = self._locks.get(account_id)
            state = self._accounts.get(account_id)
            if lock is None:
                lock = threading.RLock()
                self._locks[account_id] = lock
            if state is None:
                state = AccountState()
                self._accounts[account_id] = state
            return lock, state

    # --- Balance API ---

    # Store the latest balance for an account.
    def set_balance(self, account_id: BrokerAccountId, balance: Balance) -> None:
        lock, state = self._ensure_account(account_id)
        with lock:
            state.balance = self._clone(balance)

    # Return the latest balance for an account.
    def get_balance(self, account_id: BrokerAccountId) -> Optional[Balance]:
        lock, state = self._ensure_account(account_id)
        with lock:
            return self._clone(state.balance)

    # --- Positions API ---

    # Store the latest positions and update entry timestamps.
    def set_positions(
        self, account_id: BrokerAccountId, positions: List[Position]
    ) -> None:
        lock, state = self._ensure_account(account_id)
        with lock:
            # Maintain a best-effort "first seen" timestamp per open position so the
            # dashboard can show something meaningful (IST entry time) even though
            # broker position payloads often don't include a fill timestamp.
            now_iso = datetime.now(timezone.utc).isoformat()
            positions_copy = self._clone(list(positions))

            def _key(p: Position) -> str:
                pt = getattr(p.product_type, "value", None) or str(p.product_type)
                return f"{p.symbol}|{pt}"

            new_keys = set(_key(p) for p in positions_copy)
            old_keys = set(state.positions_first_seen_ts.keys())
            # Drop keys that are no longer open.
            for k in old_keys - new_keys:
                state.positions_first_seen_ts.pop(k, None)
            # Add first-seen timestamps for newly opened positions.
            for p in positions_copy:
                k = _key(p)
                if k not in state.positions_first_seen_ts:
                    state.positions_first_seen_ts[k] = now_iso
                # populate entry_ts onto Position for API serialization
                try:
                    p.entry_ts = state.positions_first_seen_ts[k]
                except Exception:
                    pass

            state.positions = positions_copy

    # Return a copy of positions for an account.
    def get_positions(self, account_id: BrokerAccountId) -> List[Position]:
        lock, state = self._ensure_account(account_id)
        with lock:
            return self._clone(state.positions)

    # Return latest position sync health metadata for an account.
    def get_positions_status(
        self, account_id: BrokerAccountId
    ) -> Dict[str, Optional[object]]:
        lock, state = self._ensure_account(account_id)
        with lock:
            return {
                "status": state.positions_status,
                "last_ok_ts": state.positions_last_ok_ts,
                "last_count": state.positions_last_count,
                "error_reason": state.positions_error_reason,
                "blocked_ts": state.positions_blocked_ts,
                "retry_after_seconds": state.positions_retry_after_seconds,
            }

    # Update metadata for position sync status.
    def update_positions_status(
        self,
        account_id: BrokerAccountId,
        *,
        status: Optional[str],
        last_ok_ts: Optional[str],
        last_count: Optional[int],
        error_reason: Optional[str],
        blocked_ts: Optional[str],
        retry_after_seconds: Optional[float],
    ) -> None:
        lock, state = self._ensure_account(account_id)
        with lock:
            state.positions_status = status
            if last_ok_ts is not None:
                state.positions_last_ok_ts = last_ok_ts
            if last_count is not None:
                state.positions_last_count = last_count
            state.positions_error_reason = error_reason
            state.positions_blocked_ts = blocked_ts
            state.positions_retry_after_seconds = retry_after_seconds

    # Store the latest orders list for an account.
    def set_orders(self, account_id: BrokerAccountId, orders: List[OrderStatus]) -> None:
        lock, state = self._ensure_account(account_id)
        with lock:
            state.orders = self._cap_orders(self._clone(list(orders)))

    # Return a copy of the latest orders list.
    def get_orders(self, account_id: BrokerAccountId) -> List[OrderStatus]:
        lock, state = self._ensure_account(account_id)
        with lock:
            return self._clone(state.orders)

    # Store the latest full broker order snapshot for an account.
    def set_order_snapshot(
        self, account_id: BrokerAccountId, orders: List[OrderStatus]
    ) -> None:
        lock, state = self._ensure_account(account_id)
        with lock:
            state.order_snapshot = self._cap_orders(self._clone(list(orders)))

    # Return a copy of the latest full broker order snapshot.
    def get_order_snapshot(self, account_id: BrokerAccountId) -> List[OrderStatus]:
        lock, state = self._ensure_account(account_id)
        with lock:
            return self._clone(state.order_snapshot)

    # Insert or update a single order in the list.
    def upsert_order(self, account_id: BrokerAccountId, order: OrderStatus) -> None:
        lock, state = self._ensure_account(account_id)
        with lock:
            order_copy = self._clone(order)
            for idx, existing in enumerate(state.orders):
                if existing.order_id == order_copy.order_id:
                    # Move refreshed order to the end so recency-based capping keeps it.
                    state.orders.pop(idx)
                    state.orders.append(order_copy)
                    return
            state.orders.append(order_copy)
            if len(state.orders) > self._max_orders_per_account:
                state.orders = state.orders[-self._max_orders_per_account :]

    # Insert or update a single order in the full broker snapshot list.
    def upsert_order_snapshot(
        self, account_id: BrokerAccountId, order: OrderStatus
    ) -> None:
        lock, state = self._ensure_account(account_id)
        with lock:
            order_copy = self._clone(order)
            for idx, existing in enumerate(state.order_snapshot):
                if existing.order_id == order_copy.order_id:
                    state.order_snapshot.pop(idx)
                    state.order_snapshot.append(order_copy)
                    return
            state.order_snapshot.append(order_copy)
            if len(state.order_snapshot) > self._max_orders_per_account:
                state.order_snapshot = state.order_snapshot[-self._max_orders_per_account :]

    # --- Orders API (optional for now) ---

    # Store the last order response for an account.
    def set_last_order_response(
        self, account_id: BrokerAccountId, response: OrderResponse
    ) -> None:
        lock, state = self._ensure_account(account_id)
        with lock:
            state.last_order_response = self._clone(response)

    # Return the last order response for an account.
    def get_last_order_response(
        self, account_id: BrokerAccountId
    ) -> Optional[OrderResponse]:
        lock, state = self._ensure_account(account_id)
        with lock:
            return self._clone(state.last_order_response)

    # --- Strategy signal validity API ---

    # Store overall signal validity for an account.
    def set_account_signal_valid(
        self, account_id: BrokerAccountId, is_valid: Optional[bool]
    ) -> None:
        lock, state = self._ensure_account(account_id)
        with lock:
            state.account_signal_valid = is_valid

    # Return overall signal validity for an account.
    def get_account_signal_valid(
        self, account_id: BrokerAccountId
    ) -> Optional[bool]:
        lock, state = self._ensure_account(account_id)
        with lock:
            return state.account_signal_valid

    # Store signal validity for a specific strategy.
    def set_strategy_signal_valid(
        self,
        account_id: BrokerAccountId,
        strategy_id: str,
        is_valid: bool,
    ) -> None:
        lock, state = self._ensure_account(account_id)
        with lock:
            state.strategy_signal_valid[str(strategy_id)] = bool(is_valid)

    # Return signal validity for a strategy or overall account.
    def get_strategy_signal_valid(
        self,
        account_id: BrokerAccountId,
        strategy_id: Optional[str] = None,
    ) -> Optional[bool]:
        lock, state = self._ensure_account(account_id)
        with lock:
            if strategy_id:
                return state.strategy_signal_valid.get(str(strategy_id))
            if state.account_signal_valid is not None:
                return state.account_signal_valid
            if not state.strategy_signal_valid:
                return None
            return any(state.strategy_signal_valid.values())


__all__ = ["StateStore", "AccountState"]
