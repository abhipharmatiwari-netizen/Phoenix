"""
Order lifecycle tracking service for delayed fill processing.

Tracks broker order submissions and emits trade + PnL updates when fills are
observed in StateStore order snapshots, with exactly-once semantics per
(broker_account_id, broker_order_id).
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from app.brokers.base import OrderRequest, OrderResponse, OrderStatus, Position, ProductType
from app.core.clock import IClock, SystemClock
from app.core.feature_flags import load_stability_feature_flags
from app.core.identifiers import (
    BrokerAccountId,
    HubOrderId,
    HubOrderKey,
    StrategyId,
    TenantId,
    make_hub_order_id,
)
from app.core.logging_utils import log_event
from app.core.position_state import InternalPosition, PositionState
from app.data.bq_persister import insert_trade_record
from app.data.state_store import StateStore
from app.data.trade_records import build_trade_record_from_fill, trade_record_to_bq_row
from app.orders.order_outbox import OrderSubmissionOutbox
from app.orders.order_state import (
    OrderLifecycleState,
    classify_broker_status,
    is_terminal,
    validate_order_transition,
)
from app.orders.position_ownership import ContractKey, PositionOwnershipStore
from app.orders.trade_processed_store import (
    InMemoryProcessedTradeStore,
    ProcessedTradeStore,
    build_processed_trade_store,
)
from app.pnl.pnl_engine import PnLEngine
from app.pnl.types import TradeEvent, TradeOpenEvent, TradeCloseEvent

logger = logging.getLogger(__name__)

_TERMINAL_FILLED_STATUSES = {
    "FILLED",
    "FULL",
    "COMPLETE",
    "COMPLETED",
    "EXECUTED",
}
_TERMINAL_NON_FILL_STATUSES = {
    "REJECTED",
    "FAILED",
    "ERROR",
    "CANCELLED",
    "CANCELED",
    "EXPIRED",
}
_DEFAULT_PROCESSED_TTL_SECONDS = 7 * 24 * 60 * 60
_DEFAULT_PROCESSED_MAX_ENTRIES = 20000
_DEFAULT_RECENT_ENTRY_HINT_TTL_SECONDS = 10 * 60


def _positive_float(value: object) -> Optional[float]:
    try:
        if value is None:
            return None
        fval = float(value)
    except (TypeError, ValueError):
        return None
    if fval <= 0:
        return None
    return fval


def _positive_int(value: object) -> int:
    try:
        if value is None:
            return 0
        ival = int(float(value))
    except (TypeError, ValueError):
        return 0
    if ival <= 0:
        return 0
    return ival


def _parse_utc(value: Optional[str], *, fallback: datetime) -> datetime:
    if not value:
        return fallback
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        return fallback
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class SubmissionContext:
    tenant_id: TenantId
    broker_account_id: BrokerAccountId
    strategy_id: StrategyId
    hub_order_id: str
    broker_order_id: str
    symbol: str
    side: str
    fallback_qty: int
    fallback_price: Optional[float]
    product_type: Optional[str] = None
    contract_key: Optional[ContractKey] = None
    ownership_strategy_id: Optional[StrategyId] = None
    purpose: Optional[str] = None
    position_label: Optional[str] = None
    strategy_context: Optional[Dict[str, Any]] = None
    submitted_at: Optional[datetime] = None
    observed_filled_qty: int = 0
    lifecycle_state: OrderLifecycleState = OrderLifecycleState.CREATED
    position_scope_key: Optional[str] = None


class OrderLifecycleService:
    """Background fill detector for ACK->FILL lifecycle completion."""

    def __init__(
        self,
        *,
        state_store: StateStore,
        pnl_engine: Optional[PnLEngine],
        position_ownership_store: Optional[PositionOwnershipStore] = None,
        processed_store: Optional[ProcessedTradeStore] = None,
        submission_outbox: Optional[OrderSubmissionOutbox] = None,
        clock: Optional[IClock] = None,
    ) -> None:
        self._state_store = state_store
        self._pnl_engine = pnl_engine
        self._clock: IClock = clock or SystemClock()
        self._stability_flags = load_stability_feature_flags()
        self._position_ownership_store = position_ownership_store
        self._contexts: Dict[str, SubmissionContext] = {}
        # Ordered map: order_key -> processed_at (UTC). Oldest entries are pruned first.
        self._processed_orders: "OrderedDict[str, datetime]" = OrderedDict()
        self._processed_ttl_seconds = self._resolve_processed_ttl_seconds()
        self._processed_max_entries = self._resolve_processed_max_entries()
        self._processed_store = processed_store or build_processed_trade_store()
        self._submission_outbox = submission_outbox
        if self._processed_store is None:
            self._processed_store = InMemoryProcessedTradeStore()
        self._recent_entry_submissions: "OrderedDict[str, SubmissionContext]" = OrderedDict()
        self._recent_entry_submission_ttl_seconds = (
            _DEFAULT_RECENT_ENTRY_HINT_TTL_SECONDS
        )
        self._recent_entry_lock = threading.RLock()
        self._task: Optional[asyncio.Task[None]] = None
        self._running = False
        self._poll_seconds = self._resolve_poll_seconds()
        self._last_tracked_snapshot_by_account: Dict[BrokerAccountId, str] = {}
        self._last_seen_updated_at_by_account: Dict[BrokerAccountId, str] = {}
        self._last_order_states: Dict[str, OrderLifecycleState] = {}
        self._position_records: Dict[str, InternalPosition] = {}
        self._position_persist_interval_ticks = self._resolve_position_persist_interval()
        self._position_persist_tick_counter = 0

    @staticmethod
    def _resolve_position_persist_interval() -> int:
        """Every N poll ticks, flush position records to Postgres. Default: every 20 ticks (~60s)."""
        raw = os.getenv("ORDER_LIFECYCLE_POSITION_PERSIST_INTERVAL_TICKS", "20")
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return 20

    @staticmethod
    def _resolve_poll_seconds() -> float:
        raw = os.getenv("ORDER_LIFECYCLE_POLL_SECONDS", "3")
        try:
            return max(0.1, float(raw))
        except (TypeError, ValueError):
            return 3.0

    @staticmethod
    def _resolve_processed_ttl_seconds() -> int:
        raw = os.getenv(
            "ORDER_LIFECYCLE_PROCESSED_TTL_SECONDS",
            str(_DEFAULT_PROCESSED_TTL_SECONDS),
        )
        try:
            return max(60, int(raw))
        except (TypeError, ValueError):
            return _DEFAULT_PROCESSED_TTL_SECONDS

    @staticmethod
    def _resolve_processed_max_entries() -> int:
        raw = os.getenv(
            "ORDER_LIFECYCLE_PROCESSED_MAX_ENTRIES",
            str(_DEFAULT_PROCESSED_MAX_ENTRIES),
        )
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return _DEFAULT_PROCESSED_MAX_ENTRIES

    def _prune_processed_orders(self, *, now: Optional[datetime] = None) -> None:
        if not self._processed_orders:
            return
        ts_now = (now or self._clock.now_utc()).astimezone(timezone.utc)
        cutoff = ts_now - timedelta(seconds=self._processed_ttl_seconds)
        while self._processed_orders:
            oldest_key = next(iter(self._processed_orders))
            oldest_ts = self._processed_orders.get(oldest_key)
            if oldest_ts is None or oldest_ts >= cutoff:
                break
            self._processed_orders.pop(oldest_key, None)
        while len(self._processed_orders) > self._processed_max_entries:
            self._processed_orders.popitem(last=False)

    def _is_processed(self, order_key: str, *, now: Optional[datetime] = None) -> bool:
        self._prune_processed_orders(now=now)
        processed_at = self._processed_orders.get(order_key)
        if processed_at is None:
            return False
        ts_now = (now or self._clock.now_utc()).astimezone(timezone.utc)
        cutoff = ts_now - timedelta(seconds=self._processed_ttl_seconds)
        if processed_at < cutoff:
            self._processed_orders.pop(order_key, None)
            return False
        self._processed_orders.move_to_end(order_key, last=True)
        return True

    def _mark_processed(self, order_key: str, *, processed_at: Optional[datetime] = None) -> None:
        ts = (processed_at or self._clock.now_utc()).astimezone(timezone.utc)
        self._processed_orders[order_key] = ts
        self._processed_orders.move_to_end(order_key, last=True)
        self._prune_processed_orders(now=ts)

    def _prune_recent_entry_submissions(
        self,
        *,
        now: Optional[datetime] = None,
    ) -> None:
        ts_now = (now or self._clock.now_utc()).astimezone(timezone.utc)
        cutoff = ts_now - timedelta(seconds=self._recent_entry_submission_ttl_seconds)
        with self._recent_entry_lock:
            while self._recent_entry_submissions:
                oldest_key = next(iter(self._recent_entry_submissions))
                oldest_ctx = self._recent_entry_submissions.get(oldest_key)
                oldest_ts = getattr(oldest_ctx, "submitted_at", None)
                if oldest_ctx is None or oldest_ts is None or oldest_ts < cutoff:
                    self._recent_entry_submissions.pop(oldest_key, None)
                    continue
                break
            while len(self._recent_entry_submissions) > self._processed_max_entries:
                self._recent_entry_submissions.popitem(last=False)

    def _cache_recent_entry_submission(
        self,
        order_key: str,
        ctx: SubmissionContext,
    ) -> None:
        if str(ctx.purpose or "").upper() != "ENTRY":
            return
        with self._recent_entry_lock:
            self._recent_entry_submissions[order_key] = ctx
            self._recent_entry_submissions.move_to_end(order_key, last=True)
        self._prune_recent_entry_submissions(now=ctx.submitted_at)

    def _drop_recent_entry_submission(self, order_key: str) -> None:
        with self._recent_entry_lock:
            self._recent_entry_submissions.pop(order_key, None)

    def find_recent_entry_submission(
        self,
        *,
        broker_account_id: BrokerAccountId,
        label: Optional[str],
        symbol: Optional[str],
        side: str,
    ) -> Optional[Dict[str, Any]]:
        self._prune_recent_entry_submissions()
        target_account = str(broker_account_id or "").strip()
        target_label = str(label or "").strip()
        target_symbol = str(symbol or "").strip().upper()
        target_side = str(side or "").strip().upper()
        with self._recent_entry_lock:
            recent_contexts = list(self._recent_entry_submissions.values())
        for ctx in reversed(recent_contexts):
            if str(ctx.broker_account_id or "").strip() != target_account:
                continue
            if target_side and str(ctx.side or "").strip().upper() != target_side:
                continue
            ctx_label = str(ctx.position_label or "").strip()
            ctx_symbol = str(ctx.symbol or "").strip().upper()
            if target_label and ctx_label and ctx_label == target_label:
                matched = True
            else:
                matched = bool(target_symbol and ctx_symbol == target_symbol)
            if not matched:
                continue
            strategy_context = copy.deepcopy(ctx.strategy_context)
            return {
                "strategy_name": str(ctx.strategy_id),
                "position_label": ctx_label or None,
                "strategy_context": strategy_context,
                "broker_order_id": str(ctx.broker_order_id or "").strip() or None,
                "hub_order_id": str(ctx.hub_order_id or "").strip() or None,
                "submitted_at": (
                    ctx.submitted_at.astimezone(timezone.utc).isoformat()
                    if isinstance(ctx.submitted_at, datetime)
                    else None
                ),
            }
        return None

    @property
    def processed_count(self) -> int:
        self._prune_processed_orders()
        return len(self._processed_orders)

    @staticmethod
    def _order_key(broker_account_id: BrokerAccountId, broker_order_id: str) -> str:
        return f"{broker_account_id}:{broker_order_id}"

    def _invalidate_account_snapshot_cache(self, broker_account_id: BrokerAccountId) -> None:
        self._last_tracked_snapshot_by_account.pop(broker_account_id, None)
        self._last_seen_updated_at_by_account.pop(broker_account_id, None)

    @staticmethod
    def _build_order_map(orders: list[OrderStatus]) -> tuple[Dict[str, OrderStatus], str]:
        order_map: Dict[str, OrderStatus] = {}
        max_updated_at = ""
        for order in orders:
            broker_order_id = str(order.order_id or "").strip()
            if not broker_order_id:
                continue
            order_map[broker_order_id] = order
            updated_at = str(order.updated_at or "").strip()
            if updated_at and updated_at > max_updated_at:
                max_updated_at = updated_at
        return order_map, max_updated_at

    @staticmethod
    def _tracked_snapshot_signature(
        *,
        tracked_order_ids: set[str],
        order_map: Dict[str, OrderStatus],
        account_max_updated_at: str,
    ) -> str:
        parts = [f"max_updated_at={account_max_updated_at}", f"tracked={len(tracked_order_ids)}"]
        for broker_order_id in sorted(tracked_order_ids):
            order = order_map.get(broker_order_id)
            if order is None:
                parts.append(f"{broker_order_id}|MISSING")
                continue
            parts.append(
                "|".join(
                    [
                        broker_order_id,
                        str(order.status or "").upper(),
                        str(order.filled_quantity if order.filled_quantity is not None else ""),
                        str(order.quantity if order.quantity is not None else ""),
                        str(order.average_price if order.average_price is not None else ""),
                        str(order.price if order.price is not None else ""),
                        str(order.updated_at or "").strip(),
                    ]
                )
        )
        return ";".join(parts)

    def process_observed_order_status(
        self,
        broker_account_id: BrokerAccountId,
        order: OrderStatus,
        *,
        transition_source: str = "webhook",
    ) -> None:
        self._process_order_status(
            broker_account_id,
            order,
            transition_source=transition_source,
        )

    def _snapshot_source_for_reconciliation(
        self,
        broker_account_id: BrokerAccountId,
    ) -> tuple[list[OrderStatus], str]:
        if self._stability_flags.order_lifecycle_use_broker_snapshot:
            snapshot = self._state_store.get_order_snapshot(broker_account_id)
            if snapshot:
                return snapshot, "broker_snapshot"
            active_orders = self._state_store.get_orders(broker_account_id)
            if active_orders:
                return active_orders, "broker_snapshot"
            return snapshot, "broker_snapshot"
        return self._state_store.get_orders(broker_account_id), "broker_snapshot"

    def register_submission(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        hub_order_id: str,
        order_req: OrderRequest,
        response: OrderResponse,
        contract_key: Optional[ContractKey] = None,
        ownership_strategy_id: Optional[StrategyId] = None,
    ) -> None:
        broker_order_id = str(response.broker_order_id or "").strip()
        if not broker_order_id:
            return
        order_key = self._order_key(broker_account_id, broker_order_id)
        if self._is_processed(order_key):
            return

        ownership_strategy = ownership_strategy_id or strategy_id
        fallback_price = (
            _positive_float(response.average_price)
            or _positive_float(order_req.limit_price)
            or _positive_float(order_req.stop_price)
        )
        ctx = SubmissionContext(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
            hub_order_id=str(hub_order_id),
            broker_order_id=broker_order_id,
            symbol=order_req.symbol,
            side=str(order_req.side.value).upper(),
            fallback_qty=_positive_int(order_req.quantity),
            fallback_price=fallback_price,
            product_type=(
                getattr(order_req.product_type, "value", None)
                or (str(order_req.product_type) if order_req.product_type else None)
            ),
            contract_key=contract_key,
            ownership_strategy_id=ownership_strategy,
            purpose=(
                getattr(order_req.purpose, "value", None)
                or (str(order_req.purpose) if order_req.purpose else None)
            ),
            position_label=str(order_req.position_label or "").strip() or None,
            strategy_context=copy.deepcopy(order_req.strategy_context)
            if order_req.strategy_context
            else None,
            submitted_at=self._clock.now_utc(),
        )
        self._contexts[order_key] = ctx
        self._transition_order_state(
            ctx,
            OrderLifecycleState.VALIDATED,
            reason="router_submission_validated",
            transition_source="router_submission",
        )
        self._transition_order_state(
            ctx,
            OrderLifecycleState.SUBMITTING,
            reason="router_submission_dispatching_to_broker",
            transition_source="router_submission",
        )
        response_state = self._response_state(response)
        if response_state == OrderLifecycleState.PARTIAL_FILL:
            self._transition_order_state(
                ctx,
                OrderLifecycleState.OPEN,
                reason="broker_reported_partial_fill_open_state",
                transition_source="placement_response",
            )
        self._transition_order_state(
            ctx,
            response_state,
            reason=f"broker_response_{str(response.status or '').strip().upper() or 'UNKNOWN'}",
            transition_source="placement_response",
        )
        self._prime_position_state(ctx, initial_state=response_state)
        self._cache_recent_entry_submission(order_key, ctx)
        self._invalidate_account_snapshot_cache(broker_account_id)
        self._process_response_fill(response, ctx, response_state=response_state)

    def mark_recovery_pending(self) -> int:
        """Mark all tracked non-terminal order contexts AND internal position records
        as RECOVERY_PENDING before broker reconciliation (Architecture §11.1 step 4).

        §123 / Issue #2: Previously only _contexts (order state machines) were marked.
        _position_records loaded by load_position_records() were left in their persisted
        state (e.g. OPEN), allowing broker reconciliation to treat them as authoritative
        before evidence was evaluated.  Both classes of restored objects must be fenced.

        Returns the total number of contexts + position records marked.
        """
        marked = 0

        # Mark active order lifecycle contexts
        for key, ctx in list(self._contexts.items()):
            current_state = getattr(ctx, "state", None)
            if current_state is not None and not is_terminal(current_state):
                ctx.state = OrderLifecycleState.RECOVERY_PENDING
                marked += 1

        # Mark internal position records (Architecture §11.1 step 4 / §3.3)
        _terminal_position_states = {PositionState.FLAT, PositionState.NONE}
        positions_marked = 0
        for scope_key, rec in list(self._position_records.items()):
            if rec.position_state not in _terminal_position_states:
                if float(rec.net_qty or 0) == 0:
                    # net_qty=0 means the position is already closed in the broker.
                    # Promote directly to FLAT — no broker reconciliation needed and
                    # no ownership record required, so this cannot create an ownership gap.
                    rec.position_state = PositionState.FLAT
                else:
                    rec.position_state = PositionState.RECOVERY_PENDING
                    positions_marked += 1
        marked += positions_marked

        if marked:
            logger.info(
                "startup.recovery_pending_marked_contexts: marked %d non-terminal "
                "order contexts and %d position records as RECOVERY_PENDING",
                marked - positions_marked,
                positions_marked,
            )
        return marked

    # ------------------------------------------------------------------
    # Durable position state persistence (Architecture §3.3 + Issue #48)
    # ------------------------------------------------------------------

    def save_position_records(self, conn) -> int:
        """Upsert all non-FLAT/NONE position records to Postgres.

        Returns the number of rows written. Skips FLAT and NONE records because
        they carry no actionable authority state worth restoring.
        """
        _SKIP_STATES = {"FLAT", "NONE"}
        sql = """
            INSERT INTO internal_position_records (
                scope_key, position_id, tenant_id, account_id, strategy_id,
                ownership_key, contract_key, side,
                net_qty, filled_qty_open, filled_qty_close,
                avg_open_price, avg_close_price,
                realized_pnl, unrealized_pnl,
                position_state, state_reason, opened_by_order_id,
                last_evidence_at, last_reconciled_at,
                updated_at
            ) VALUES (
                %(scope_key)s, %(position_id)s, %(tenant_id)s, %(account_id)s, %(strategy_id)s,
                %(ownership_key)s, %(contract_key)s, %(side)s,
                %(net_qty)s, %(filled_qty_open)s, %(filled_qty_close)s,
                %(avg_open_price)s, %(avg_close_price)s,
                %(realized_pnl)s, %(unrealized_pnl)s,
                %(position_state)s, %(state_reason)s, %(opened_by_order_id)s,
                %(last_evidence_at)s, %(last_reconciled_at)s,
                NOW()
            )
            ON CONFLICT (scope_key) DO UPDATE SET
                position_id        = EXCLUDED.position_id,
                tenant_id          = EXCLUDED.tenant_id,
                account_id         = EXCLUDED.account_id,
                strategy_id        = EXCLUDED.strategy_id,
                ownership_key      = EXCLUDED.ownership_key,
                contract_key       = EXCLUDED.contract_key,
                side               = EXCLUDED.side,
                net_qty            = EXCLUDED.net_qty,
                filled_qty_open    = EXCLUDED.filled_qty_open,
                filled_qty_close   = EXCLUDED.filled_qty_close,
                avg_open_price     = EXCLUDED.avg_open_price,
                avg_close_price    = EXCLUDED.avg_close_price,
                realized_pnl       = EXCLUDED.realized_pnl,
                unrealized_pnl     = EXCLUDED.unrealized_pnl,
                position_state     = EXCLUDED.position_state,
                state_reason       = EXCLUDED.state_reason,
                opened_by_order_id = EXCLUDED.opened_by_order_id,
                last_evidence_at   = EXCLUDED.last_evidence_at,
                last_reconciled_at = EXCLUDED.last_reconciled_at,
                updated_at         = NOW()
        """
        written = 0
        with conn.cursor() as cur:
            for scope_key, rec in list(self._position_records.items()):
                state_val = (
                    rec.position_state.value
                    if isinstance(rec.position_state, PositionState)
                    else str(rec.position_state or "NONE")
                )
                if state_val in _SKIP_STATES:
                    continue
                cur.execute(sql, {
                    "scope_key": scope_key,
                    "position_id": str(rec.position_id or ""),
                    "tenant_id": str(rec.tenant_id or ""),
                    "account_id": str(rec.account_id or ""),
                    "strategy_id": str(rec.strategy_id or ""),
                    "ownership_key": str(rec.ownership_key or ""),
                    "contract_key": str(rec.contract_key or ""),
                    "side": str(rec.side or ""),
                    "net_qty": float(rec.net_qty or 0.0),
                    "filled_qty_open": float(rec.filled_qty_open or 0.0),
                    "filled_qty_close": float(rec.filled_qty_close or 0.0),
                    "avg_open_price": float(rec.avg_open_price or 0.0),
                    "avg_close_price": float(rec.avg_close_price or 0.0),
                    "realized_pnl": float(rec.realized_pnl or 0.0),
                    "unrealized_pnl": float(rec.unrealized_pnl or 0.0),
                    "position_state": state_val,
                    "state_reason": str(rec.state_reason or ""),
                    "opened_by_order_id": str(rec.opened_by_order_id or ""),
                    "last_evidence_at": rec.last_evidence_at,
                    "last_reconciled_at": rec.last_reconciled_at,
                })
                written += 1
        logger.info("position_records.saved: wrote %d records to Postgres", written)
        return written

    def load_position_records(self, conn) -> int:
        """Restore non-terminal position records from Postgres into _position_records.

        Only loads records whose position_state is not FLAT or NONE (terminal /
        empty states carry no authority). Existing in-memory records are NOT
        overwritten — call before any outbox recovery processing.

        Returns the number of rows loaded.
        """
        sql = """
            SELECT scope_key, position_id, tenant_id, account_id, strategy_id,
                   ownership_key, contract_key, side,
                   net_qty, filled_qty_open, filled_qty_close,
                   avg_open_price, avg_close_price,
                   realized_pnl, unrealized_pnl,
                   position_state, state_reason, opened_by_order_id,
                   last_evidence_at, last_reconciled_at
            FROM internal_position_records
            WHERE position_state NOT IN ('FLAT', 'NONE')
              AND net_qty != 0
        """
        # net_qty = 0 records are excluded: they are effectively flat regardless
        # of their position_state (e.g. FLAT_PENDING_CONFIRMATION after a fill).
        # Loading them triggers the ownership gap detector because they have no
        # matching entry in position_ownership_ledger (ownership is only written
        # for open legs).  Skipping them avoids a false gap on every restart.
        loaded = 0
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall() or []
            cols = [desc[0] for desc in cur.description]
        for row in rows:
            r = dict(zip(cols, row))
            scope_key = str(r.get("scope_key") or "")
            if not scope_key:
                continue
            if scope_key in self._position_records:
                continue
            try:
                state = PositionState(str(r.get("position_state") or "NONE"))
            except ValueError:
                state = PositionState.RECONCILING
            rec = InternalPosition(
                position_id=str(r.get("position_id") or ""),
                tenant_id=str(r.get("tenant_id") or ""),
                account_id=str(r.get("account_id") or ""),
                strategy_id=str(r.get("strategy_id") or ""),
                ownership_key=str(r.get("ownership_key") or ""),
                contract_key=str(r.get("contract_key") or ""),
                side=str(r.get("side") or ""),
                net_qty=float(r.get("net_qty") or 0.0),
                filled_qty_open=float(r.get("filled_qty_open") or 0.0),
                filled_qty_close=float(r.get("filled_qty_close") or 0.0),
                avg_open_price=float(r.get("avg_open_price") or 0.0),
                avg_close_price=float(r.get("avg_close_price") or 0.0),
                realized_pnl=float(r.get("realized_pnl") or 0.0),
                unrealized_pnl=float(r.get("unrealized_pnl") or 0.0),
                position_state=state,
                state_reason=str(r.get("state_reason") or ""),
                opened_by_order_id=str(r.get("opened_by_order_id") or ""),
                last_evidence_at=r.get("last_evidence_at"),
                last_reconciled_at=r.get("last_reconciled_at"),
            )
            self._position_records[scope_key] = rec
            loaded += 1
        logger.info(
            "position_records.loaded: restored %d non-terminal records from Postgres", loaded
        )
        return loaded

    @staticmethod
    def force_terminal_positions_by_symbol_pattern(
        *,
        symbol_pattern: str,
        min_age_days: int = 45,
        terminal_state: str = "FLAT",
        state_reason: str = "startup_expired_contract_cleanup",
    ) -> int:
        """Force non-terminal position records older than *min_age_days* to *terminal_state*.

        Intended for startup cleanup of expired-contract position records that produce
        DEGRADED cascades on every restart (Architecture §73 / issue #73).

        NOTE: *symbol_pattern* is accepted for API compatibility but is NOT applied to
        internal_position_records.  The contract_key column stores dates in ISO format
        ('YYYY-MM-DD') whereas the outbox stores broker symbols ('NATURALGAS24MAR26CE').
        For position records the age guard alone is the correct safety criterion: any
        non-terminal position record older than *min_age_days* is definitionally an
        expired contract for Indian equity/commodity options (max tenor ≤ 3 months).

        Returns the count of rows updated.
        """
        from app.data.postgres import connect_with_retry, get_control_plane_dsn
        dsn = get_control_plane_dsn()
        age_days = max(1, int(min_age_days))
        sql = f"""
            UPDATE internal_position_records
            SET position_state = %(terminal_state)s,
                state_reason   = %(state_reason)s,
                last_reconciled_at = NOW()
            WHERE position_state NOT IN ('FLAT', 'NONE')
              AND (
                  last_evidence_at  < NOW() - INTERVAL '{age_days} days'
                  OR last_reconciled_at < NOW() - INTERVAL '{age_days} days'
                  OR (last_evidence_at IS NULL AND last_reconciled_at IS NULL)
              )
        """
        with connect_with_retry(dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, {
                    "terminal_state": terminal_state,
                    "state_reason": state_reason,
                })
                return cur.rowcount or 0

    @staticmethod
    def purge_old_flat_position_records(*, retain_days: int = 90) -> int:
        """Delete FLAT and NONE position records older than *retain_days*.

        These records carry no live authority once they reach a terminal state.
        Removing them prevents unbounded growth of internal_position_records.

        Returns the number of rows deleted.
        """
        from app.data.postgres import connect_with_retry, get_control_plane_dsn
        age_days = max(1, int(retain_days))
        sql = f"""
            DELETE FROM internal_position_records
            WHERE position_state IN ('FLAT', 'NONE')
              AND COALESCE(last_reconciled_at, last_evidence_at)
                  < NOW() - INTERVAL '{age_days} days'
        """
        dsn = get_control_plane_dsn()
        with connect_with_retry(dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                deleted = cur.rowcount or 0
        if deleted:
            logger.info(
                "retention.position_purge: deleted=%d FLAT/NONE records older than %d days",
                deleted, age_days,
            )
        return deleted

    def count_positions_by_state(self) -> dict[str, int]:
        """Return a count of in-memory position records keyed by position_state.

        Used by /readyz to surface DEGRADED and RECONCILING counts so health
        checks can block readiness when positions need operator attention.
        Snapshot is taken under the recent-entry lock for consistency.
        """
        counts: dict[str, int] = {}
        with self._recent_entry_lock:
            records_snapshot = list(self._position_records.values())
        for rec in records_snapshot:
            state_val = (
                rec.position_state.value
                if hasattr(rec.position_state, "value")
                else str(rec.position_state)
            )
            counts[state_val] = counts.get(state_val, 0) + 1
        return counts

    def _try_persist_position_records(self) -> None:
        """Non-fatal periodic flush of position records to Postgres."""
        try:
            from app.data.postgres import connect_with_retry, get_control_plane_dsn
            dsn = get_control_plane_dsn()
            with connect_with_retry(dsn, autocommit=True) as conn:
                self.save_position_records(conn)
        except Exception:
            logger.debug("position_records.periodic_persist failed (non-fatal)", exc_info=True)

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(
            self._poll_loop(),
            name="order-lifecycle-poll",
        )

    async def stop(self) -> None:
        self._running = False
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self._tick_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("OrderLifecycleService poll tick failed")
            self._position_persist_tick_counter += 1
            if self._position_persist_tick_counter >= self._position_persist_interval_ticks:
                self._position_persist_tick_counter = 0
                self._try_persist_position_records()
            await asyncio.sleep(self._poll_seconds)

    async def _tick_once(self) -> None:
        self._prune_processed_orders()
        if not self._contexts:
            self._last_tracked_snapshot_by_account.clear()
            self._last_seen_updated_at_by_account.clear()
            return
        tracked_order_ids_by_account: Dict[BrokerAccountId, set[str]] = {}
        for ctx in self._contexts.values():
            tracked_order_ids_by_account.setdefault(ctx.broker_account_id, set()).add(
                ctx.broker_order_id
            )

        tracked_accounts = set(tracked_order_ids_by_account.keys())
        for stale_account_id in list(self._last_tracked_snapshot_by_account.keys()):
            if stale_account_id not in tracked_accounts:
                self._invalidate_account_snapshot_cache(stale_account_id)

        for account_id, tracked_order_ids in tracked_order_ids_by_account.items():
            if not tracked_order_ids:
                continue
            orders, transition_source = self._snapshot_source_for_reconciliation(account_id)
            order_map, account_max_updated_at = self._build_order_map(orders)
            snapshot_signature = self._tracked_snapshot_signature(
                tracked_order_ids=tracked_order_ids,
                order_map=order_map,
                account_max_updated_at=account_max_updated_at,
            )
            if self._last_tracked_snapshot_by_account.get(account_id) == snapshot_signature:
                continue
            self._last_tracked_snapshot_by_account[account_id] = snapshot_signature
            self._last_seen_updated_at_by_account[account_id] = account_max_updated_at
            for broker_order_id in tracked_order_ids:
                order = order_map.get(broker_order_id)
                if order is None:
                    continue
                self._process_order_status(
                    account_id,
                    order,
                    transition_source=transition_source,
                )

    def _is_terminal_fill(self, status: str) -> bool:
        return str(status or "").upper() in _TERMINAL_FILLED_STATUSES

    def _is_terminal_non_fill(self, status: str) -> bool:
        return str(status or "").upper() in _TERMINAL_NON_FILL_STATUSES

    @staticmethod
    def _is_exit_like(ctx: SubmissionContext) -> bool:
        purpose = str(ctx.purpose or "").strip().upper()
        return purpose in {"EXIT", "ADJUST"}

    def _maybe_infer_exit_from_sign(
        self,
        ctx: SubmissionContext,
        *,
        fill_qty_hint: Optional[int] = None,
    ) -> bool:
        """Issue #204 Layer 2: if the fill direction reduces |record.net_qty|
        but ``ctx.purpose`` is not EXIT/ADJUST, reclassify the fill as exit by
        mutating ``ctx.purpose = "EXIT"`` so every downstream consumer
        (``_record_observed_fill_progress``, ``_apply_position_fill``,
        ``_emit_trade``'s control-PnL routing) sees the corrected purpose
        without needing per-callsite plumbing.

        Idempotent: a second call after ``ctx.purpose`` has been corrected is
        a no-op. Returns True iff the inference was applied on this call.
        """
        if self._is_exit_like(ctx):
            return False
        scope = self._position_scope_key(ctx)
        if not scope:
            return False
        record = self._position_records.get(scope)
        if record is None:
            return False
        prior_net_qty = float(record.net_qty)
        if abs(prior_net_qty) <= 0.0001:
            return False
        side = str(ctx.side or "").upper()
        if side not in ("BUY", "SELL"):
            return False
        fill_qty = int(
            fill_qty_hint if fill_qty_hint is not None
            else ctx.observed_filled_qty or ctx.fallback_qty or 0
        )
        if fill_qty <= 0:
            return False
        signed_qty = float(fill_qty if side == "BUY" else -fill_qty)
        opposes = (
            (prior_net_qty > 0.0 and signed_qty < 0.0)
            or (prior_net_qty < 0.0 and signed_qty > 0.0)
        )
        if not opposes:
            return False

        prior_purpose = str(ctx.purpose or "")
        logger.warning(
            "position_fill_exit_inferred_from_sign — fill reduces "
            "|net_qty|; reclassifying as EXIT despite ctx.purpose=%r. "
            "broker_order_id=%s symbol=%s side=%s prior_net_qty=%.2f "
            "signed_qty=%.2f",
            prior_purpose,
            ctx.broker_order_id,
            ctx.symbol,
            side,
            prior_net_qty,
            signed_qty,
        )
        log_event(
            logger,
            event_type="POSITION_FILL_EXIT_INFERRED_FROM_SIGN",
            message=(
                "Fill direction opposes existing net_qty; reclassified "
                "as exit despite missing/incorrect ctx.purpose."
            ),
            level=logging.WARNING,
            tenant_id=ctx.tenant_id,
            broker_account_id=ctx.broker_account_id,
            strategy_id=ctx.strategy_id,
            instrument=ctx.symbol,
            broker_order_id=ctx.broker_order_id,
            position_id=record.position_id,
            scope_key=record.ownership_key,
            prior_purpose=prior_purpose,
            inferred_purpose="EXIT",
            prior_net_qty=prior_net_qty,
            fill_signed_qty=signed_qty,
        )
        # Mutate ctx so every downstream consumer of `ctx.purpose` -- the
        # ownership-store partial-fill observer, the position-state machine
        # transition, the control-PnL routing in _emit_trade -- sees EXIT.
        # Codex P2 #2 + #4 (PR #207).
        ctx.purpose = "EXIT"
        return True

    def _position_scope_key(self, ctx: SubmissionContext) -> Optional[str]:
        if ctx.position_scope_key:
            return ctx.position_scope_key
        contract_key = ctx.contract_key
        if contract_key is not None:
            scope = (
                f"{ctx.tenant_id}:{ctx.broker_account_id}:"
                f"{ctx.ownership_strategy_id or ctx.strategy_id}:"
                f"{contract_key.product_type}:{contract_key.as_storage_key()}"
            )
            ctx.position_scope_key = scope
            return scope
        symbol = str(ctx.symbol or "").strip()
        if not symbol:
            return None
        scope = (
            f"{ctx.tenant_id}:{ctx.broker_account_id}:"
            f"{ctx.ownership_strategy_id or ctx.strategy_id}:"
            f"symbol:{symbol}"
        )
        ctx.position_scope_key = scope
        return scope

    def _ensure_position_record(self, ctx: SubmissionContext) -> Optional[InternalPosition]:
        scope_key = self._position_scope_key(ctx)
        if not scope_key:
            return None
        record = self._position_records.get(scope_key)
        if record is not None:
            return record
        contract_key = (
            repr(ctx.contract_key.as_storage_key())
            if ctx.contract_key is not None
            else str(ctx.symbol or "")
        )
        record = InternalPosition(
            tenant_id=str(ctx.tenant_id),
            account_id=str(ctx.broker_account_id),
            strategy_id=str(ctx.ownership_strategy_id or ctx.strategy_id),
            ownership_key=scope_key,
            contract_key=contract_key,
            side=str(ctx.side or "").upper(),
            opened_by_order_id=str(ctx.broker_order_id or ctx.hub_order_id or ""),
        )
        self._position_records[scope_key] = record
        return record

    def get_order_state(
        self,
        *,
        broker_account_id: BrokerAccountId,
        broker_order_id: str,
    ) -> Optional[OrderLifecycleState]:
        return self._last_order_states.get(
            self._order_key(broker_account_id, broker_order_id)
        )

    def get_position_record(self, *, scope_key: str) -> Optional[InternalPosition]:
        record = self._position_records.get(str(scope_key))
        return copy.deepcopy(record) if record is not None else None

    def list_position_records(self) -> list[InternalPosition]:
        """Return a stable snapshot of in-memory position records for admin evidence."""
        with self._recent_entry_lock:
            records_snapshot = list(self._position_records.values())
        return [copy.deepcopy(record) for record in records_snapshot]

    def force_clear_position_record(
        self,
        *,
        scope_key: str,
        reason: str,
    ) -> Optional[InternalPosition]:
        """Forcibly clear a stuck position record by setting it to FLAT.

        Bypasses the state-machine transition table because the typical caller
        is an operator-driven admin endpoint cleaning up records that the
        machine cannot drain on its own (e.g. flip-fill RECOVERY_PENDING
        residue). The caller is responsible for confirming the broker side is
        actually flat *before* invoking this.

        Returns a deepcopy of the prior record (for audit), or None if no such
        record exists.
        """
        record = self._position_records.get(str(scope_key))
        if record is None:
            return None
        prior = copy.deepcopy(record)
        record.position_state = PositionState.FLAT
        record.net_qty = 0.0
        record.unrealized_pnl = 0.0
        record.state_reason = str(reason or "force_cleared_by_admin")
        record.last_reconciled_at = self._clock.now_utc()
        return prior

    def _transition_order_state(
        self,
        ctx: SubmissionContext,
        target: OrderLifecycleState,
        *,
        reason: str,
        transition_source: str,
    ) -> None:
        current = ctx.lifecycle_state
        if current == target:
            self._last_order_states[
                self._order_key(ctx.broker_account_id, ctx.broker_order_id)
            ] = target
            return
        _valid_transition = True
        try:
            validate_order_transition(current, target)
        except Exception:
            _valid_transition = False
            # §101: In LIVE mode log at ERROR level — an invalid order-state
            # transition indicates a state machine bug that must not be silently
            # ignored.  The transition is still forced so that reconciliation
            # can converge (stopping mid-flight is worse), but the ERROR log
            # makes it visible for operator investigation.
            _live = str(os.getenv("TRADE_MODE", "PAPER") or "PAPER").strip().upper() == "LIVE"
            _level = logging.ERROR if _live else logging.WARNING
            logger.log(
                _level,
                "OrderLifecycleService: invalid order-state transition "
                "broker_order_id=%s current=%s target=%s source=%s reason=%s — "
                "forcing transition; investigate state machine logic",
                ctx.broker_order_id,
                current.value,
                target.value,
                transition_source,
                reason,
            )
        ctx.lifecycle_state = target
        self._last_order_states[
            self._order_key(ctx.broker_account_id, ctx.broker_order_id)
        ] = target
        log_event(
            logger,
            event_type="ORDER_LIFECYCLE_STATE",
            message="Order lifecycle state updated.",
            tenant_id=ctx.tenant_id,
            broker_account_id=ctx.broker_account_id,
            strategy_id=ctx.strategy_id,
            instrument=ctx.symbol,
            level=logging.INFO,
            transition_source=transition_source,
            broker_order_id=ctx.broker_order_id,
            previous_state=current.value,
            state=target.value,
            reason=reason,
        )

    def _transition_position_state(
        self,
        record: InternalPosition,
        target: PositionState,
        *,
        reason: str,
    ) -> None:
        if record.position_state == target:
            record.state_reason = reason
            return
        try:
            record.transition_to(target, reason)
        except Exception:
            if record.position_state != PositionState.RECONCILING:
                try:
                    record.transition_to(
                        PositionState.RECONCILING,
                        f"{reason}_reconciling_handoff",
                    )
                    if target != PositionState.RECONCILING:
                        record.transition_to(target, reason)
                    return
                except Exception:
                    pass
            from_state = record.position_state
            logger.warning(
                "OrderLifecycleService: illegal transition blocked "
                "position_id=%s ownership_key=%s from_state=%s "
                "attempted_target=%s reason=%s; escalating to DEGRADED",
                record.position_id,
                record.ownership_key,
                from_state.value,
                target.value,
                reason,
            )
            try:
                record.transition_to(
                    PositionState.DEGRADED,
                    f"illegal_transition_blocked:{from_state.value}_to_{target.value}:{reason}",
                )
            except Exception:
                # Already DEGRADED or another terminal path — leave state as-is.
                pass
            # §85: Notify DegradedScopeManager so the scope count is
            # observable from dashboards and alert rules.
            try:
                from app.core.degraded_scope_manager import (
                    DegradedReason,
                    degraded_scope_manager,
                )
                degraded_scope_manager.enter_degraded(
                    scope_key=str(record.ownership_key or record.position_id),
                    reason=DegradedReason.LIFECYCLE_STUCK,
                    metadata={
                        "position_id": str(record.position_id),
                        "from_state": from_state.value,
                        "attempted_target": target.value,
                        "trigger_reason": reason,
                    },
                )
            except Exception:
                pass

    def _prime_position_state(
        self,
        ctx: SubmissionContext,
        *,
        initial_state: OrderLifecycleState,
    ) -> None:
        record = self._ensure_position_record(ctx)
        if record is None:
            return
        if self._is_exit_like(ctx):
            if record.position_state == PositionState.NONE:
                self._transition_position_state(
                    record,
                    PositionState.RECONCILING,
                    reason="exit_submitted_without_known_internal_position",
                )
            else:
                self._transition_position_state(
                    record,
                    PositionState.EXIT_PENDING,
                    reason="exit_submitted",
                )
            return
        if record.position_state == PositionState.NONE:
            self._transition_position_state(
                record,
                PositionState.ENTRY_PENDING,
                reason="entry_submitted",
            )
        if initial_state in {
            OrderLifecycleState.SUBMITTED,
            OrderLifecycleState.ACKED,
            OrderLifecycleState.OPEN,
            OrderLifecycleState.PARTIAL_FILL,
        }:
            self._transition_position_state(
                record,
                PositionState.OPENING,
                reason="entry_awaiting_durable_fill",
            )

    def _record_observed_fill_progress(
        self,
        ctx: SubmissionContext,
        *,
        cumulative_filled_qty: int,
        transition_source: str,
    ) -> None:
        filled_qty = max(int(cumulative_filled_qty or 0), int(ctx.observed_filled_qty or 0))
        if filled_qty <= int(ctx.observed_filled_qty or 0):
            return
        ctx.observed_filled_qty = filled_qty
        # Issue #204 Codex P2 #2: run sign-aware exit inference *before* this
        # method classifies the fill (and especially before it transitions
        # OPEN -> OPENING via the entry path), so a mis-tagged BUY closing a
        # SHORT does not get briefly mis-routed and then "fixed" only on the
        # terminal-fill path. The helper mutates ctx.purpose so the
        # _is_exit_like checks below see the corrected value.
        self._maybe_infer_exit_from_sign(ctx, fill_qty_hint=filled_qty)
        if self._position_ownership_store is not None:
            try:
                self._position_ownership_store.observe_fill_progress(
                    tenant_id=ctx.tenant_id,
                    broker_account_id=ctx.broker_account_id,
                    contract_key=ctx.contract_key,
                    strategy_id=ctx.ownership_strategy_id or ctx.strategy_id,
                    is_exit_order=self._is_exit_like(ctx),
                    filled_qty=filled_qty,
                )
            except Exception:
                logger.exception(
                    "OrderLifecycleService: ownership partial-fill observation failed for broker_order_id=%s",
                    ctx.broker_order_id,
                )
        record = self._ensure_position_record(ctx)
        if record is not None:
            target = (
                PositionState.PARTIALLY_EXITED
                if self._is_exit_like(ctx)
                else PositionState.OPENING
            )
            self._transition_position_state(
                record,
                target,
                reason=f"partial_fill_observed_via_{transition_source}",
            )

    def _apply_position_fill(
        self,
        ctx: SubmissionContext,
        *,
        filled_qty: int,
        price: float,
        trade_time: datetime,
    ) -> None:
        record = self._ensure_position_record(ctx)
        if record is None:
            return
        signed_qty = float(filled_qty if ctx.side == "BUY" else -filled_qty)
        record.last_evidence_at = trade_time
        # Issue #204 Layer 2: run sign-aware exit inference. The helper
        # mutates ``ctx.purpose`` to "EXIT" when the fill direction reduces
        # |record.net_qty|, so this branch (and downstream control-PnL
        # routing in ``_emit_trade``) reads the corrected purpose. Idempotent
        # if ``_record_observed_fill_progress`` already reclassified earlier
        # in the lifecycle.
        self._maybe_infer_exit_from_sign(ctx, fill_qty_hint=int(filled_qty))
        if self._is_exit_like(ctx):
            # Flip detection: an exit fill whose signed qty would carry net_qty
            # from one sign through zero to the other (e.g. SELL -1250 plus a
            # BUY +2500 closing fill -> +1250) violates the partial-exit
            # invariant filled_close <= filled_open. Auto-splitting the fill
            # into a close leg + a fresh entry leg is correct but requires
            # spawning a new position_id with the opposite side, which is a
            # design change deserving its own PR.  For now we surface the
            # situation safely: park the record in RECONCILING with a
            # structured reason so the operator can square the broker leg
            # manually and clear state via the admin endpoint, and record an
            # audit event with both legs' implied quantities for follow-up.
            prior_net_qty = float(record.net_qty)
            new_net_qty = prior_net_qty + signed_qty
            if (
                abs(prior_net_qty) > 0.0001
                and (
                    (prior_net_qty > 0 and new_net_qty < -0.0001)
                    or (prior_net_qty < 0 and new_net_qty > 0.0001)
                )
            ):
                close_leg_qty = abs(prior_net_qty)
                open_leg_qty = abs(float(filled_qty)) - close_leg_qty
                logger.warning(
                    "OrderLifecycleService: flip-fill detected — exit fill "
                    "would carry net_qty=%.2f through zero to %.2f. Routing "
                    "position %s to RECONCILING for operator action; record "
                    "is NOT mutated by this fill (close_leg=%.2f open_leg=%.2f).",
                    prior_net_qty,
                    new_net_qty,
                    record.position_id,
                    close_leg_qty,
                    open_leg_qty,
                )
                log_event(
                    logger,
                    event_type="POSITION_FLIP_FILL_DETECTED",
                    message=(
                        "Single exit fill exceeds open quantity (flip-the-trade); "
                        "record routed to RECONCILING."
                    ),
                    level=logging.WARNING,
                    tenant_id=ctx.tenant_id,
                    broker_account_id=ctx.broker_account_id,
                    strategy_id=ctx.strategy_id,
                    instrument=ctx.symbol,
                    broker_order_id=ctx.broker_order_id,
                    position_id=record.position_id,
                    scope_key=record.ownership_key,
                    prior_net_qty=prior_net_qty,
                    fill_signed_qty=signed_qty,
                    close_leg_qty=close_leg_qty,
                    open_leg_qty=open_leg_qty,
                    fill_price=float(price),
                )
                self._transition_position_state(
                    record,
                    PositionState.RECONCILING,
                    reason=(
                        f"flip_fill_blocked:close_leg={close_leg_qty:.0f}:"
                        f"open_leg={open_leg_qty:.0f}:awaiting_operator_action"
                    ),
                )
                # Project the LAST KNOWN good state to state_store so downstream
                # readers (sweep/EOD/trailing-lock) see the pre-fill view, not
                # half-updated counters.
                self._project_state_store_position(
                    ctx,
                    net_qty=prior_net_qty,
                    avg_open_price=record.avg_open_price,
                    trade_time=trade_time,
                )
                return

            if record.position_state == PositionState.NONE:
                self._transition_position_state(
                    record,
                    PositionState.RECONCILING,
                    reason="exit_fill_without_known_position",
                )
            record.filled_qty_close += abs(float(filled_qty))
            if record.avg_close_price <= 0:
                record.avg_close_price = float(price)
            else:
                total_close = max(record.filled_qty_close, 0.0)
                prior_close = max(total_close - abs(float(filled_qty)), 0.0)
                if total_close > 0:
                    record.avg_close_price = (
                        (record.avg_close_price * prior_close)
                        + (float(price) * abs(float(filled_qty)))
                    ) / total_close
            record.net_qty += signed_qty
            if abs(record.net_qty) <= 0.0001:
                record.net_qty = 0.0
                self._transition_position_state(
                    record,
                    PositionState.FLAT_PENDING_CONFIRMATION,
                    reason="exit_fill_observed_pending_broker_flat_confirmation",
                )
            else:
                self._transition_position_state(
                    record,
                    PositionState.PARTIALLY_EXITED,
                    reason="partial_exit_fill_observed",
                )
            self._project_state_store_position(
                ctx,
                net_qty=record.net_qty,
                avg_open_price=record.avg_open_price,
                trade_time=trade_time,
            )
            return

        record.side = str(ctx.side or "").upper()
        record.filled_qty_open += abs(float(filled_qty))
        if record.avg_open_price <= 0:
            record.avg_open_price = float(price)
        else:
            total_open = max(record.filled_qty_open, 0.0)
            prior_open = max(total_open - abs(float(filled_qty)), 0.0)
            if total_open > 0:
                record.avg_open_price = (
                    (record.avg_open_price * prior_open)
                    + (float(price) * abs(float(filled_qty)))
                ) / total_open
        record.net_qty += signed_qty
        self._transition_position_state(
            record,
            PositionState.OPEN,
            reason="entry_fill_observed",
        )
        self._project_state_store_position(
            ctx,
            net_qty=record.net_qty,
            avg_open_price=record.avg_open_price,
            trade_time=trade_time,
        )

    @staticmethod
    def _coerce_product_type(value: object) -> ProductType:
        normalized = str(
            getattr(value, "value", value) or ProductType.INTRADAY.value
        ).strip().upper()
        try:
            return ProductType(normalized)
        except Exception:
            return ProductType.INTRADAY

    @staticmethod
    def _position_matches(
        pos: object,
        *,
        symbol: str,
        product_type: ProductType,
    ) -> bool:
        current_symbol = str(
            getattr(pos, "symbol", None)
            or (pos.get("symbol") if isinstance(pos, dict) else "")
            or ""
        ).strip()
        current_product_type = (
            getattr(pos, "product_type", None)
            if not isinstance(pos, dict)
            else pos.get("product_type") or pos.get("producttype")
        )
        return (
            current_symbol == symbol
            and OrderLifecycleService._coerce_product_type(current_product_type) == product_type
        )

    def _project_state_store_position(
        self,
        ctx: SubmissionContext,
        *,
        net_qty: float,
        avg_open_price: float,
        trade_time: datetime,
    ) -> None:
        symbol = str(ctx.symbol or "").strip()
        if not symbol:
            return
        product_type = self._coerce_product_type(ctx.product_type)
        entry_ts = trade_time.isoformat()
        projected: list[Position] = []
        current_positions = list(self._state_store.get_positions(ctx.broker_account_id) or [])
        matched_existing = False

        for pos in current_positions:
            if not self._position_matches(pos, symbol=symbol, product_type=product_type):
                if isinstance(pos, Position):
                    projected.append(copy.deepcopy(pos))
                elif isinstance(pos, dict):
                    projected.append(
                        Position(
                            symbol=str(pos.get("symbol") or "").strip(),
                            quantity=int(float(pos.get("quantity") or pos.get("qty") or 0)),
                            avg_price=float(
                                pos.get("avg_price")
                                or pos.get("avgPrice")
                                or pos.get("average_price")
                                or pos.get("avgprice")
                                or 0.0
                            ),
                            product_type=self._coerce_product_type(
                                pos.get("product_type") or pos.get("producttype")
                            ),
                            entry_ts=pos.get("entry_ts") or pos.get("entryTs"),
                        )
                    )
                continue

            matched_existing = True
            existing_entry_ts = getattr(pos, "entry_ts", None)
            if existing_entry_ts:
                entry_ts = str(existing_entry_ts)

        rounded_qty = int(round(float(net_qty or 0.0)))
        if rounded_qty != 0:
            projected.append(
                Position(
                    symbol=symbol,
                    quantity=rounded_qty,
                    avg_price=float(avg_open_price or 0.0),
                    product_type=product_type,
                    entry_ts=entry_ts,
                )
            )
        elif not matched_existing:
            return

        self._state_store.set_positions(ctx.broker_account_id, projected)

    @staticmethod
    def _response_state(response: OrderResponse) -> OrderLifecycleState:
        details = response.details if isinstance(response.details, dict) else {}
        if bool(details.get("pending_reconciliation")):
            return OrderLifecycleState.TIMEOUT
        return classify_broker_status(response.status)

    def _release_pending_lock(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        contract_key: Optional[ContractKey],
    ) -> None:
        if self._position_ownership_store is None or contract_key is None:
            return
        try:
            self._position_ownership_store.release_pending(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                contract_key=contract_key,
                strategy_id=strategy_id,
            )
        except Exception:
            logger.exception(
                "OrderLifecycleService: pending ownership release failed for broker_account_id=%s strategy_id=%s",
                broker_account_id,
                strategy_id,
            )

    def _mark_outbox_terminal(
        self,
        *,
        broker_account_id: BrokerAccountId,
        broker_order_id: str,
        terminal_status: str,
        recovery_action: Optional[str] = None,
        updated_at: Optional[datetime] = None,
    ) -> None:
        if self._submission_outbox is None:
            return
        try:
            self._submission_outbox.mark_terminal_by_broker_order(
                broker_account_id=broker_account_id,
                broker_order_id=broker_order_id,
                terminal_status=terminal_status,
                updated_at=(updated_at or self._clock.now_utc()),
                recovery_action=recovery_action,
            )
        except Exception:
            logger.exception(
                "OrderLifecycleService: outbox terminal update failed for broker_order_id=%s",
                broker_order_id,
            )

    def _process_response_fill(
        self,
        response: OrderResponse,
        ctx: SubmissionContext,
        *,
        response_state: Optional[OrderLifecycleState] = None,
    ) -> None:
        lifecycle_state = response_state or self._response_state(response)
        broker_order_id = str(response.broker_order_id or "").strip()
        if not broker_order_id:
            return
        order_key = self._order_key(ctx.broker_account_id, broker_order_id)
        if self._is_processed(order_key):
            return

        observed_filled_qty = _positive_int(response.filled_quantity)
        if lifecycle_state == OrderLifecycleState.PARTIAL_FILL and observed_filled_qty > 0:
            self._record_observed_fill_progress(
                ctx,
                cumulative_filled_qty=observed_filled_qty,
                transition_source="placement_response",
            )
            return
        if lifecycle_state not in {
            OrderLifecycleState.FILLED,
            OrderLifecycleState.REJECTED,
            OrderLifecycleState.CANCELLED,
            OrderLifecycleState.EXPIRED,
            OrderLifecycleState.FAILED,
        }:
            if observed_filled_qty > 0:
                self._record_observed_fill_progress(
                    ctx,
                    cumulative_filled_qty=observed_filled_qty,
                    transition_source="placement_response",
                )
            return

        if observed_filled_qty > 0:
            self._record_observed_fill_progress(
                ctx,
                cumulative_filled_qty=observed_filled_qty,
                transition_source="placement_response",
            )

        filled_qty = max(observed_filled_qty, int(ctx.observed_filled_qty or 0))
        if lifecycle_state == OrderLifecycleState.FILLED and filled_qty <= 0:
            filled_qty = ctx.fallback_qty
        if filled_qty <= 0:
            if lifecycle_state != OrderLifecycleState.FILLED:
                record = self._ensure_position_record(ctx)
                if record is not None:
                    target = (
                        PositionState.RECONCILING
                        if self._is_exit_like(ctx)
                        else (
                            PositionState.NONE
                            if record.position_state == PositionState.ENTRY_PENDING
                            else PositionState.DEGRADED
                        )
                    )
                    self._transition_position_state(
                        record,
                        target,
                        reason=f"placement_terminal_non_fill_{lifecycle_state.value.lower()}",
                    )
                self._drop_recent_entry_submission(order_key)
                self._release_pending_lock(
                    tenant_id=ctx.tenant_id,
                    broker_account_id=ctx.broker_account_id,
                    strategy_id=ctx.ownership_strategy_id or ctx.strategy_id,
                    contract_key=ctx.contract_key,
                )
                self._contexts.pop(order_key, None)
                self._invalidate_account_snapshot_cache(ctx.broker_account_id)
            return

        price = (
            _positive_float(response.average_price)
            or ctx.fallback_price
        )
        if price is None:
            logger.warning(
                "OrderLifecycleService: missing immediate fill price for broker_order_id=%s",
                broker_order_id,
            )
            return
        self._emit_trade(
            ctx,
            filled_qty=filled_qty,
            price=price,
            trade_time=self._clock.now_utc(),
            transition_source="placement_response",
            terminal_status=(
                "FILLED"
                if lifecycle_state == OrderLifecycleState.FILLED
                else str(response.status or "")
            ),
            lifecycle_state=lifecycle_state,
        )

    def _process_order_status(
        self,
        broker_account_id: BrokerAccountId,
        order: OrderStatus,
        *,
        transition_source: str,
    ) -> None:
        broker_order_id = str(order.order_id or "").strip()
        if not broker_order_id:
            return
        order_key = self._order_key(broker_account_id, broker_order_id)
        if self._is_processed(order_key):
            return
        ctx = self._contexts.get(order_key)
        if ctx is None:
            return
        observed_state = classify_broker_status(order.status)
        observed_filled_qty = _positive_int(order.filled_quantity)
        if observed_filled_qty > 0:
            self._record_observed_fill_progress(
                ctx,
                cumulative_filled_qty=observed_filled_qty,
                transition_source=transition_source,
            )
        if observed_state == OrderLifecycleState.PARTIAL_FILL:
            self._transition_order_state(
                ctx,
                OrderLifecycleState.PARTIAL_FILL,
                reason="broker_reported_partial_fill",
                transition_source=transition_source,
            )
            return
        if observed_state in {
            OrderLifecycleState.SUBMITTED,
            OrderLifecycleState.ACKED,
            OrderLifecycleState.OPEN,
            OrderLifecycleState.CANCEL_REQUESTED,
            OrderLifecycleState.TIMEOUT,
            OrderLifecycleState.UNKNOWN,
            OrderLifecycleState.RECONCILING,
        }:
            self._transition_order_state(
                ctx,
                observed_state,
                reason=f"broker_reported_{observed_state.value.lower()}",
                transition_source=transition_source,
            )
            if observed_state == OrderLifecycleState.UNKNOWN:
                record = self._ensure_position_record(ctx)
                if record is not None:
                    self._transition_position_state(
                        record,
                        PositionState.RECONCILING,
                        reason="broker_order_status_unknown",
                    )
            return
        if observed_state in {
            OrderLifecycleState.REJECTED,
            OrderLifecycleState.CANCELLED,
            OrderLifecycleState.EXPIRED,
            OrderLifecycleState.FAILED,
        }:
            self._transition_order_state(
                ctx,
                observed_state,
                reason=f"broker_reported_terminal_{observed_state.value.lower()}",
                transition_source=transition_source,
            )
            terminal_filled_qty = max(
                observed_filled_qty,
                int(ctx.observed_filled_qty or 0),
            )
            price = (
                _positive_float(order.average_price)
                or _positive_float(order.price)
                or ctx.fallback_price
            )
            if terminal_filled_qty > 0 and price is not None:
                self._emit_trade(
                    ctx,
                    filled_qty=terminal_filled_qty,
                    price=price,
                    trade_time=_parse_utc(
                        order.updated_at,
                        fallback=self._clock.now_utc(),
                    ),
                    transition_source=transition_source,
                    terminal_status=str(order.status or ""),
                    lifecycle_state=observed_state,
                )
                return
            self._release_pending_lock(
                tenant_id=ctx.tenant_id,
                broker_account_id=ctx.broker_account_id,
                strategy_id=ctx.ownership_strategy_id or ctx.strategy_id,
                contract_key=ctx.contract_key,
            )
            self._mark_outbox_terminal(
                broker_account_id=ctx.broker_account_id,
                broker_order_id=broker_order_id,
                terminal_status=str(order.status or ""),
                recovery_action=transition_source,
                updated_at=_parse_utc(order.updated_at, fallback=self._clock.now_utc()),
            )
            # Issue #224: surface broker-side rejection text + error
            # code for terminal non-fills. Without these, REJECTED /
            # CANCELLED orders observed via the snapshot poll path leave
            # operators with only the abstract status enum value and no
            # actionable diagnostic. The fields are optional on
            # ``OrderStatus`` (default None) so adapters that don't yet
            # populate them are silently tolerated.
            #
            # PR #235 round-1 review (P2): broker rejection text
            # is free-form and typically contains spaces, ``=``,
            # and commas (e.g. ``RMS:Margin Exceeds,
            # Required:75000 Available:50000``). ``log_event``
            # formats extras as flat ``key=value`` tokens joined
            # by spaces, so even a JSON-quoted value gets split
            # across the line during downstream parsing.
            #
            # PR #235 round-2 review (P2): use ``urllib.parse.quote``
            # with empty ``safe`` to percent-encode spaces, ``=``,
            # commas, and quotes — guarantees the value is a single
            # whitespace-free token that flat parsers can keep
            # together. Downstream consumers can ``unquote`` for
            # human display.
            #
            # PR #235 round-2 review (P2): skip None values entirely
            # so absent diagnostics don't appear as
            # ``broker_status_message=None`` in logs (which would
            # match "field exists" filters and obscure real
            # diagnostics).
            from urllib.parse import quote as _url_quote
            raw_status_message = getattr(order, "status_message", None)
            raw_error_code = getattr(order, "error_code", None)
            extra_log_fields: dict[str, Any] = {}
            if raw_status_message:
                extra_log_fields["broker_status_message"] = _url_quote(
                    str(raw_status_message), safe="",
                )
            if raw_error_code:
                extra_log_fields["broker_error_code"] = _url_quote(
                    str(raw_error_code), safe="",
                )
            log_event(
                logger,
                event_type="ORDER_LIFECYCLE_TERMINAL_NON_FILL",
                message="Observed terminal non-fill order state during reconciliation.",
                tenant_id=ctx.tenant_id,
                broker_account_id=ctx.broker_account_id,
                strategy_id=ctx.strategy_id,
                instrument=ctx.symbol,
                level=logging.INFO,
                transition_source=transition_source,
                broker_order_id=broker_order_id,
                status=observed_state.value,
                mode=self._stability_flags.order_lifecycle_use_broker_snapshot,
                **extra_log_fields,
            )
            record = self._ensure_position_record(ctx)
            if record is not None:
                target = (
                    PositionState.RECONCILING
                    if self._is_exit_like(ctx)
                    else (
                        PositionState.NONE
                        if record.position_state == PositionState.ENTRY_PENDING
                        else PositionState.DEGRADED
                    )
                )
                self._transition_position_state(
                    record,
                    target,
                    reason=f"terminal_non_fill_{observed_state.value.lower()}",
                )
            self._drop_recent_entry_submission(order_key)
            self._contexts.pop(order_key, None)
            self._invalidate_account_snapshot_cache(ctx.broker_account_id)
            return
        if observed_state != OrderLifecycleState.FILLED:
            return

        filled_qty = (
            _positive_int(order.filled_quantity)
            or _positive_int(order.quantity)
            or ctx.fallback_qty
        )
        if filled_qty <= 0:
            return

        self._transition_order_state(
            ctx,
            OrderLifecycleState.FILLED,
            reason="broker_reported_terminal_fill",
            transition_source=transition_source,
        )
        price = (
            _positive_float(order.average_price)
            or _positive_float(order.price)
            or ctx.fallback_price
        )
        if price is None:
            logger.warning(
                "OrderLifecycleService: missing fill price for broker_order_id=%s status=%s",
                broker_order_id,
                order.status,
            )
            return

        self._emit_trade(
            ctx,
            filled_qty=filled_qty,
            price=price,
            trade_time=_parse_utc(order.updated_at, fallback=self._clock.now_utc()),
            transition_source=transition_source,
            terminal_status="FILLED",
            lifecycle_state=OrderLifecycleState.FILLED,
        )

    def _emit_trade(
        self,
        ctx: SubmissionContext,
        *,
        filled_qty: int,
        price: float,
        trade_time: datetime,
        transition_source: str,
        terminal_status: str,
        lifecycle_state: OrderLifecycleState,
    ) -> None:
        broker_order_id = ctx.broker_order_id
        order_key = self._order_key(ctx.broker_account_id, broker_order_id)
        if self._is_processed(order_key, now=trade_time):
            return
        ctx.observed_filled_qty = max(int(ctx.observed_filled_qty or 0), int(filled_qty or 0))
        self._transition_order_state(
            ctx,
            lifecycle_state,
            reason=f"terminal_trade_emitted_{lifecycle_state.value.lower()}",
            transition_source=transition_source,
        )

        claimed = True
        try:
            claimed = self._processed_store.claim_processed(
                tenant_id=ctx.tenant_id,
                broker_account_id=ctx.broker_account_id,
                strategy_id=ctx.strategy_id,
                broker_order_id=broker_order_id,
                hub_order_id=ctx.hub_order_id,
                symbol=ctx.symbol,
                side=ctx.side,
                processed_at=trade_time,
            )
        except Exception:
            logger.exception(
                "OrderLifecycleService: durable marker check failed for broker_order_id=%s",
                broker_order_id,
            )
            claimed = True

        self._mark_processed(order_key, processed_at=trade_time)
        self._contexts.pop(order_key, None)
        self._invalidate_account_snapshot_cache(ctx.broker_account_id)
        self._mark_outbox_terminal(
            broker_account_id=ctx.broker_account_id,
            broker_order_id=broker_order_id,
            terminal_status=str(terminal_status or lifecycle_state.value),
            recovery_action=transition_source,
            updated_at=trade_time,
        )
        self._apply_position_fill(
            ctx,
            filled_qty=filled_qty,
            price=price,
            trade_time=trade_time,
        )
        if not claimed:
            return

        log_event(
            logger,
            event_type="ORDER_LIFECYCLE_TERMINAL_FILL",
            message="Observed terminal order execution state.",
            tenant_id=ctx.tenant_id,
            broker_account_id=ctx.broker_account_id,
            strategy_id=ctx.strategy_id,
            instrument=ctx.symbol,
            level=logging.INFO,
            transition_source=transition_source,
            broker_order_id=broker_order_id,
            terminal_status=str(terminal_status or lifecycle_state.value).upper(),
            lifecycle_state=lifecycle_state.value,
            filled_qty=filled_qty,
            price=price,
            mode=self._stability_flags.order_lifecycle_use_broker_snapshot,
        )

        if self._position_ownership_store is not None:
            ownership_strategy = ctx.ownership_strategy_id or ctx.strategy_id
            signed_qty = filled_qty if ctx.side == "BUY" else -filled_qty
            try:
                self._position_ownership_store.apply_fill(
                    tenant_id=ctx.tenant_id,
                    broker_account_id=ctx.broker_account_id,
                    contract_key=ctx.contract_key,
                    strategy_id=ownership_strategy,
                    signed_qty=signed_qty,
                )
            except Exception:
                logger.exception(
                    "OrderLifecycleService: ownership fill apply failed for broker_order_id=%s",
                    broker_order_id,
                )

        try:
            canonical_hub_order_id: Optional[HubOrderId] = make_hub_order_id(
                HubOrderKey(
                    tenant_id=ctx.tenant_id,
                    broker_account_id=ctx.broker_account_id,
                    broker_order_id=broker_order_id,
                )
            )
        except Exception:
            canonical_hub_order_id = HubOrderId(ctx.hub_order_id)

        pnl_strategy_id = ctx.ownership_strategy_id or ctx.strategy_id
        record = build_trade_record_from_fill(
            trade_id=broker_order_id,
            hub_order_id=str(canonical_hub_order_id or ctx.hub_order_id),
            tenant_id=ctx.tenant_id,
            broker_account_id=ctx.broker_account_id,
            strategy_id=pnl_strategy_id,
            symbol=ctx.symbol,
            side=ctx.side,
            qty=filled_qty,
            price=price,
            trade_time=trade_time,
            realized_pnl=None,
            fees=None,
            entry_price=None,
            exit_price=None,
            entry_time=None,
            exit_time=None,
        )
        try:
            bq_row = trade_record_to_bq_row(record)
            insert_id = f"{ctx.broker_account_id}:{broker_order_id}"
            try:
                insert_trade_record(bq_row, insert_id=insert_id)
            except TypeError:
                # Backward-compatible for tests/mocks that still expose single-arg callables.
                insert_trade_record(bq_row)
        except Exception:
            logger.exception(
                "OrderLifecycleService: trade insert failed for broker_order_id=%s",
                broker_order_id,
            )

        # Per-fill Postgres trade ledger. The /me/trades endpoint reads from
        # this table when BigQuery is not configured (the OCI-only deploy).
        # Without this write, /me/trades is permanently empty even though
        # pnl_snapshots.realized_pnl accumulates correctly via on_trade.
        # ON CONFLICT DO NOTHING handles duplicate fill notifications.
        try:
            from app.data.trade_store_postgres import insert_trade_postgres
            pg_record = dict(bq_row)
            pg_record.setdefault("execution_mode", "LIVE")
            pg_record.setdefault("virtual", False)
            insert_trade_postgres(pg_record)
        except Exception:
            logger.exception(
                "OrderLifecycleService: Postgres trade insert failed for broker_order_id=%s",
                broker_order_id,
            )

        if self._pnl_engine is not None:
            signed_qty = filled_qty if ctx.side == "BUY" else -filled_qty
            try:
                self._pnl_engine.on_trade(
                    TradeEvent(
                        tenant_id=ctx.tenant_id,
                        broker_account_id=ctx.broker_account_id,
                        strategy_id=pnl_strategy_id,
                        symbol=ctx.symbol,
                        qty=signed_qty,
                        price=price,
                        trade_time=trade_time,
                        fees=0.0,
                    )
                )
            except Exception:
                logger.exception(
                    "OrderLifecycleService: PnL update failed for broker_order_id=%s",
                    broker_order_id,
                )
            # Wire control PnL: on_open_position for SELL-ENTRY, on_close_position for BUY-EXIT.
            # purpose is stored on ctx; side alone is used as safe fallback.
            _purpose = str(ctx.purpose or "").strip().upper()
            _side = str(ctx.side or "").strip().upper()
            try:
                if _side == "SELL" and _purpose in ("ENTRY", ""):
                    self._pnl_engine.on_open_position(
                        TradeOpenEvent(
                            tenant_id=ctx.tenant_id,
                            broker_account_id=ctx.broker_account_id,
                            strategy_id=pnl_strategy_id,
                            symbol=ctx.symbol,
                            qty=filled_qty,
                            entry_price=price,
                            lot_size=1,  # filled_qty already reflects broker units
                            trade_time=trade_time,
                        )
                    )
                elif _side == "BUY" and _purpose in ("EXIT", "ADJUST", ""):
                    self._pnl_engine.on_close_position(
                        TradeCloseEvent(
                            tenant_id=ctx.tenant_id,
                            broker_account_id=ctx.broker_account_id,
                            strategy_id=pnl_strategy_id,
                            symbol=ctx.symbol,
                            qty=filled_qty,
                            exit_price=price,
                            lot_size=1,
                            trade_time=trade_time,
                        )
                    )
            except Exception:
                logger.exception(
                    "OrderLifecycleService: control PnL update failed for broker_order_id=%s",
                    broker_order_id,
                )


__all__ = ["OrderLifecycleService", "SubmissionContext"]
