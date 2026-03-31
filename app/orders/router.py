"""
OrderRouter: tenant-aware order dispatcher for the hub.

- Receives orders tagged with (tenant_id, broker_account_id, strategy_id).
- Runs capital and risk checks.
- Forwards to the appropriate AccountRunner execution port.

Phase 1:
- No BigQuery or PnL integration yet.
- Focus is on structure and safe routing.
"""

from __future__ import annotations

import atexit
import csv
import logging
import os
import queue
import threading
from uuid import uuid4

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Tuple, TYPE_CHECKING
from zoneinfo import ZoneInfo

from app.brokers.base import OrderPurpose, OrderRequest, OrderResponse
from app.config.runtime_config import get_runtime_settings
from app.config.settings import get_settings
from app.core.clock import IClock, SystemClock
from app.core.dashboard_bus import dashboard_bus
from app.core.identifiers import (
    BrokerAccountId,
    HubOrderId,
    StrategyId,
    TenantId,
    HubOrderKey,
    make_hub_order_id,
)
from app.core.lot_size import lot_size_for_symbol_optional
from app.core.logging_utils import log_event
from app.data.bq_persister import insert_order_record, insert_trade_record
from app.data.trade_records import (
    build_trade_record_from_fill,
    trade_record_to_bq_row,
)
from app.data.state_store import StateStore
from app.hub.hub import Hub
from app.orders.interceptors import (
    OrderInterceptionContext,
    OrderInterceptor,
    build_default_interceptors,
)
from app.orders.order_idempotency_store import InMemoryOrderIdempotencyStore
from app.orders.order_outbox import (
    OrderOutboxRecord,
    OrderSubmissionOutbox,
    build_order_submission_outbox,
)
from app.orders.order_state import (
    classify_broker_status,
    OrderLifecycleState,
    is_terminal,
    is_terminal_fill,
    TERMINAL_NON_FILL_STATES,
    to_outbox_status,
)
from app.orders.position_ownership import ContractKey, PositionOwnershipStore
from app.pnl.pnl_engine import PnLEngine
from app.pnl.types import TradeEvent
from app.risk.capital_engine import CapitalEngine
from app.risk.circuit_breaker import TradingCircuitBreaker
from app.risk.risk_engine import RiskEngine
from app.risk.profit_engine import ProfitEngine

logger = logging.getLogger(__name__)
_POSITION_LOCK_RELEASE_STATUSES = {
    "REJECTED",
    "FAILED",
    "ERROR",
    "CANCELLED",
    "CANCELED",
    "EXPIRED",
}

if TYPE_CHECKING:
    from app.orders.order_lifecycle import OrderLifecycleService


def _csv_enabled(env_key: str) -> bool:
    return str(os.getenv(env_key, "")).strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return int(default)


class _OrderTradeCsvWriter:
    def __init__(
        self,
        *,
        base_dir: Path,
        tz_name: str,
        clock: Optional[IClock] = None,
    ) -> None:
        self._base_dir = base_dir
        self._tz = self._resolve_tz(tz_name)
        self._clock: IClock = clock or SystemClock()
        self._orders_enabled = _csv_enabled("ORDER_CSV_ENABLED")
        self._trades_enabled = _csv_enabled("TRADE_CSV_ENABLED")

    @staticmethod
    def _resolve_tz(tz_name: str) -> ZoneInfo:
        try:
            return ZoneInfo(tz_name)
        except Exception:
            return ZoneInfo("UTC")

    def _daily_dir(self) -> Path:
        day = self._clock.now_local(self._tz).date().isoformat()
        return self._base_dir / day

    @staticmethod
    def _write_row(path: Path, header: list[str], row: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        need_header = (not path.exists()) or path.stat().st_size == 0
        with open(path, mode="a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=header)
            if need_header:
                writer.writeheader()
            writer.writerow({key: row.get(key) for key in header})

    def write_order(self, record: dict) -> None:
        if not self._orders_enabled:
            return
        path = self._daily_dir() / "orders.csv"
        header = [
            "created_at",
            "hub_order_id",
            "tenant_id",
            "broker_account_id",
            "strategy_id",
            "symbol",
            "side",
            "order_type",
            "product_type",
            "time_in_force",
            "quantity",
            "limit_price",
            "stop_price",
            "status",
            "message",
            "broker_order_id",
            "filled_quantity",
            "average_price",
            "execution_mode",
            "virtual",
        ]
        self._write_row(path, header, record)

    def write_trade(self, record: dict) -> None:
        if not self._trades_enabled:
            return
        path = self._daily_dir() / "trades.csv"
        header = [
            "trade_id",
            "hub_order_id",
            "tenant_id",
            "broker_account_id",
            "strategy_id",
            "symbol",
            "side",
            "qty",
            "price",
            "trade_time",
            "realized_pnl",
            "fees",
            "entry_price",
            "exit_price",
            "entry_time",
            "exit_time",
            "execution_mode",
            "virtual",
        ]
        self._write_row(path, header, record)

    @property
    def enabled(self) -> bool:
        return bool(self._orders_enabled or self._trades_enabled)


class _AsyncOrderTradeCsvWriter:
    """
    Bounded async CSV dispatcher for order/trade records.

    Keeps file writes off the OrderRouter submit_order critical path.
    """

    def __init__(self, *, writer: _OrderTradeCsvWriter, max_queue_size: int) -> None:
        self._writer = writer
        self._queue: "queue.Queue[Optional[tuple[str, dict[str, Any], str]]]" = queue.Queue(
            maxsize=max(1, int(max_queue_size))
        )
        self._closed = False
        self._lock = threading.Lock()
        self._worker = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="order-trade-csv-writer",
        )
        self._worker.start()
        atexit.register(self.shutdown)

    def enqueue_order(self, record: dict[str, Any], *, hub_order_id: str) -> bool:
        return self._enqueue(kind="order", record=record, hub_order_id=hub_order_id)

    def enqueue_trade(self, record: dict[str, Any], *, hub_order_id: str) -> bool:
        return self._enqueue(kind="trade", record=record, hub_order_id=hub_order_id)

    def _enqueue(
        self,
        *,
        kind: str,
        record: dict[str, Any],
        hub_order_id: str,
    ) -> bool:
        with self._lock:
            if self._closed:
                return False
        try:
            self._queue.put_nowait((kind, dict(record), str(hub_order_id)))
            return True
        except queue.Full:
            logger.warning(
                "OrderRouter: csv queue full; dropping %s record hub_order_id=%s",
                kind,
                hub_order_id,
            )
            return False

    def _run_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                break
            kind, record, hub_order_id = item
            try:
                if kind == "order":
                    self._writer.write_order(record)
                else:
                    self._writer.write_trade(record)
            except Exception:
                logger.exception(
                    "OrderRouter: async csv write failed kind=%s hub_order_id=%s",
                    kind,
                    hub_order_id,
                )
            finally:
                self._queue.task_done()

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            try:
                self._queue.put(None, timeout=0.5)
            except Exception:
                return
        if self._worker.is_alive():
            self._worker.join(timeout=2.0)


# Coerce a lots value into a positive int (reject fractional/invalid values).
def _coerce_lots(value: object) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        fval = float(value)
    except (TypeError, ValueError):
        return None
    if not fval.is_integer():
        return None
    ival = int(fval)
    if ival <= 0:
        return None
    return ival


# Resolve lot size from instrument metadata (token/symbol) or universe patterns.
def _resolve_lot_size(order_req: OrderRequest) -> tuple[Optional[int], Optional[str]]:
    meta = dashboard_bus.resolve_instrument_meta(
        symbol=order_req.symbol,
        token=order_req.symbol_token,
    )
    underlying = None
    lot_size = None
    if meta:
        underlying = (
            meta.get("underlying")
            or meta.get("underlying_name")
            or meta.get("name")
        )
        lot_size = meta.get("lot_size")
    if lot_size is None:
        lot_size = lot_size_for_symbol_optional(order_req.symbol)
    try:
        if lot_size is None:
            return None, underlying
        lot_size_int = int(lot_size)
        if lot_size_int <= 0:
            return None, underlying
        return lot_size_int, underlying
    except (TypeError, ValueError):
        return None, underlying


# Resolve a fallback price for capital checks using stop price or latest LTP.
def _resolve_reference_price(order_req: OrderRequest) -> Optional[float]:
    try:
        if order_req.limit_price is not None and float(order_req.limit_price) > 0:
            return None
    except (TypeError, ValueError):
        pass
    try:
        if order_req.stop_price is not None and float(order_req.stop_price) > 0:
            return float(order_req.stop_price)
    except (TypeError, ValueError):
        pass
    meta = dashboard_bus.resolve_instrument_meta(
        symbol=order_req.symbol,
        token=order_req.symbol_token,
    )
    label = meta.get("label") if meta else None
    if not label:
        return None
    ltp = dashboard_bus.get_last_price(label)
    if ltp is None or ltp <= 0:
        return None
    return float(ltp)


# Preflight lots-based quantities into broker quantities.
def _preflight_order_quantity(
    order_req: OrderRequest,
) -> tuple[bool, str, Optional[int], Optional[int], Optional[int], Optional[str]]:
    lots = _coerce_lots(order_req.quantity)
    lot_size, underlying = _resolve_lot_size(order_req)
    if lot_size is None:
        return False, "lot_size_missing", lots, None, None, underlying
    if lots is None:
        return False, "invalid_lots", None, lot_size, None, underlying
    broker_qty = lots * lot_size
    if broker_qty <= 0 or broker_qty % lot_size != 0:
        return False, "quantity_not_multiple_of_lot_size", lots, lot_size, broker_qty, underlying
    return True, "ok", lots, lot_size, broker_qty, underlying

# Tenant-aware order dispatcher with capital/risk/profit checks.
class OrderRouter:
    """
    Tenant-aware order router.

    Phase 1:
    - Validate routing preconditions (runner exists & is running).
    - Run CapitalEngine and RiskEngine checks.
    - Forward to AccountRunner.place_order() via execution adapter.

    Later:
    - Record orders/trades to BigQuery.
    - Update PnL and richer risk state.
    """

    # Initialize dependencies for routing and checks.
    def __init__(
        self,
        hub: Hub,
        capital_engine: Optional[CapitalEngine],
        risk_engine: Optional[RiskEngine],
        profit_engine: Optional[ProfitEngine],
        state_store: StateStore,
        pnl_engine: Optional[PnLEngine] = None,
        order_lifecycle: Optional["OrderLifecycleService"] = None,
        interceptors: Optional[tuple[OrderInterceptor, ...] | list[OrderInterceptor]] = None,
        position_ownership_store: Optional[PositionOwnershipStore] = None,
        idempotency_store: Optional[InMemoryOrderIdempotencyStore] = None,
        submission_outbox: Optional[OrderSubmissionOutbox] = None,
        clock: Optional[IClock] = None,
        circuit_breaker: Optional[TradingCircuitBreaker] = None,
    ) -> None:
        self._hub = hub
        self._capital_engine = capital_engine
        self._risk_engine = risk_engine
        self._profit_engine = profit_engine
        self._state_store = state_store
        self._pnl_engine = pnl_engine
        self._order_lifecycle = order_lifecycle
        self._circuit_breaker = circuit_breaker
        self._position_ownership_store = (
            position_ownership_store or PositionOwnershipStore()
        )
        self._clock: IClock = clock or SystemClock()
        base_dir = Path(__file__).resolve().parents[2] / "logs"
        try:
            tz_name = getattr(get_settings(), "default_time_zone", None) or "UTC"
        except Exception:
            tz_name = "UTC"
        self._csv_writer = _OrderTradeCsvWriter(
            base_dir=base_dir,
            tz_name=tz_name,
            clock=self._clock,
        )
        self._async_csv_writer: Optional[_AsyncOrderTradeCsvWriter] = None
        if self._csv_writer.enabled:
            self._async_csv_writer = _AsyncOrderTradeCsvWriter(
                writer=self._csv_writer,
                max_queue_size=_int_env("ORDER_TRADE_CSV_QUEUE_SIZE", 2000),
            )
        self._policy_pipeline: tuple[OrderInterceptor, ...] = (
            tuple(interceptors)
            if interceptors is not None
            else build_default_interceptors(
                position_ownership_store=self._position_ownership_store
            )
        )
        self._idempotency_store = idempotency_store or InMemoryOrderIdempotencyStore()
        self._submission_outbox = submission_outbox or build_order_submission_outbox()
        self._last_recovery_summary: dict[str, Any] = {}
        self._startup_recovery_block_new_entries = False
        self._startup_recovery_reason: Optional[str] = None

    @staticmethod
    def _pending_idempotency_response() -> OrderResponse:
        return OrderResponse(
            broker_order_id="",
            status="DUPLICATE_PENDING",
            message="Duplicate idempotency key is already being processed",
            filled_quantity=0,
            average_price=None,
        )

    @staticmethod
    def _requires_broker_reconciliation(response: OrderResponse) -> bool:
        details = response.details if isinstance(response.details, dict) else {}
        return bool(details.get("pending_reconciliation"))

    @staticmethod
    def _is_risk_reducing_order(order_req: OrderRequest) -> bool:
        return bool(getattr(order_req, "is_exit_order", False)) or (
            getattr(order_req, "purpose", None) == OrderPurpose.ADJUST
        )

    def set_startup_recovery_gate(
        self,
        *,
        block_new_entries: bool,
        reason: Optional[str] = None,
        summary: Optional[dict[str, Any]] = None,
    ) -> None:
        self._startup_recovery_block_new_entries = bool(block_new_entries)
        self._startup_recovery_reason = (
            str(reason or "").strip() or None
        )
        if summary is not None:
            self._last_recovery_summary = dict(summary)

    def startup_recovery_status(self) -> dict[str, Any]:
        return {
            "blocked_new_entries": bool(self._startup_recovery_block_new_entries),
            "reason": self._startup_recovery_reason,
            "summary": dict(self._last_recovery_summary),
        }

    def last_recovery_summary(self) -> dict[str, Any]:
        return dict(self._last_recovery_summary)

    @staticmethod
    def _submission_key(
        *,
        hub_order_id: str,
        idempotency_key: str,
        enforce_idempotency: bool,
    ) -> str:
        if enforce_idempotency and idempotency_key:
            return idempotency_key
        return hub_order_id

    @staticmethod
    def _parse_recovery_timestamp(
        value: object,
        *,
        fallback: datetime,
    ) -> datetime:
        text = str(value or "").strip()
        if not text:
            return fallback
        normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return fallback
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _build_rehydration_response(
        record: OrderOutboxRecord,
        *,
        broker_order_id: Optional[str] = None,
        message: str = "recovered_from_outbox",
    ) -> OrderResponse:
        stored = record.as_order_response()
        if stored is not None and str(stored.broker_order_id or "").strip():
            return stored
        return OrderResponse(
            broker_order_id=(
                str(broker_order_id or record.broker_order_id or "").strip()
            ),
            status="OPEN",
            message=message,
            filled_quantity=0,
            average_price=None,
        )

    def _record_outbox_response(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        submission_key: str,
        response: OrderResponse,
        recovery_action: Optional[str] = None,
    ) -> Optional[OrderOutboxRecord]:
        if self._submission_outbox is None:
            return None
        # Log the canonical outbox status for observability / future migration.
        classified = classify_broker_status(response.status)
        canonical_outbox_status = to_outbox_status(classified)
        logger.debug(
            "OrderRouter: outbox response submission_key=%s raw_status=%s classified=%s canonical_outbox_status=%s",
            submission_key, response.status, classified.value, canonical_outbox_status,
        )
        try:
            return self._submission_outbox.record_response(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                strategy_id=strategy_id,
                submission_key=submission_key,
                response=response,
                updated_at=self._clock.now_utc(),
                recovery_action=recovery_action,
            )
        except Exception:
            logger.exception(
                "OrderRouter: failed to persist outbox response submission_key=%s",
                submission_key,
            )
            return None

    def _record_outbox_submit_error(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        submission_key: str,
        error_text: str,
    ) -> None:
        if self._submission_outbox is None:
            return
        try:
            self._submission_outbox.record_submit_error(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                strategy_id=strategy_id,
                submission_key=submission_key,
                error_text=error_text,
                updated_at=self._clock.now_utc(),
            )
        except Exception:
            logger.exception(
                "OrderRouter: failed to persist outbox submit error submission_key=%s",
                submission_key,
            )

    def _match_recovery_order(
        self,
        *,
        record: OrderOutboxRecord,
        orders: list[Any],
    ) -> Optional[Any]:
        broker_order_id = str(record.broker_order_id or "").strip()
        if broker_order_id:
            for order in orders or []:
                if str(getattr(order, "order_id", "") or "").strip() == broker_order_id:
                    return order

        request = record.as_order_request()
        created_at = record.created_at.astimezone(timezone.utc)
        cutoff = created_at - timedelta(minutes=15)
        candidates: list[Any] = []
        for order in orders or []:
            if str(getattr(order, "symbol", "") or "").strip().upper() != str(
                request.symbol or ""
            ).strip().upper():
                continue
            if str(getattr(order, "side", "") or "").strip().upper() != str(
                request.side.value
            ).strip().upper():
                continue
            try:
                quantity = int(getattr(order, "quantity", 0) or 0)
            except (TypeError, ValueError):
                continue
            if quantity != int(request.quantity or 0):
                continue
            updated_at = self._parse_recovery_timestamp(
                getattr(order, "updated_at", None),
                fallback=created_at,
            )
            if updated_at < cutoff:
                continue
            candidates.append(order)
        if len(candidates) == 1:
            return candidates[0]
        return None

    def _rehydrate_lifecycle_from_outbox(
        self,
        *,
        record: OrderOutboxRecord,
        snapshot_order: Optional[Any] = None,
    ) -> None:
        if self._order_lifecycle is None:
            return
        response = self._build_rehydration_response(
            record,
            broker_order_id=(
                str(getattr(snapshot_order, "order_id", "") or "").strip()
                if snapshot_order is not None
                else None
            ),
            message=(
                "recovered_from_broker_snapshot"
                if snapshot_order is not None
                else "recovered_from_outbox"
            ),
        )
        try:
            self._order_lifecycle.register_submission(
                tenant_id=TenantId(record.tenant_id),
                broker_account_id=BrokerAccountId(record.broker_account_id),
                strategy_id=StrategyId(record.strategy_id),
                hub_order_id=record.hub_order_id,
                order_req=record.as_order_request(),
                response=response,
                contract_key=record.as_contract_key(),
                ownership_strategy_id=(
                    StrategyId(record.ownership_strategy_id)
                    if record.ownership_strategy_id
                    else None
                ),
            )
            if snapshot_order is not None:
                self._order_lifecycle.process_observed_order_status(
                    BrokerAccountId(record.broker_account_id),
                    snapshot_order,
                    transition_source="startup_recovery",
                )
        except Exception:
            logger.exception(
                "OrderRouter: failed to rehydrate lifecycle from outbox submission_key=%s",
                record.submission_key,
            )

    # Record order metadata to BigQuery (best effort).
    def _record_order_to_bq(
        self,
        hub_order_id: str,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        order_req: OrderRequest,
        response: OrderResponse,
    ) -> None:
        """
        Best-effort recording of order metadata to BigQuery.
        """
        try:
            now = self._clock.now_utc()
            record = {
                "hub_order_id": hub_order_id,
                "tenant_id": str(tenant_id),
                "broker_account_id": str(broker_account_id),
                "strategy_id": str(strategy_id),
                "symbol": order_req.symbol,
                "side": order_req.side.value,
                "order_type": order_req.order_type.value,
                "product_type": order_req.product_type.value,
                "time_in_force": order_req.time_in_force.value,
                "quantity": order_req.quantity,
                "limit_price": order_req.limit_price,
                "stop_price": order_req.stop_price,
                "status": response.status,
                "message": response.message,
                "broker_order_id": response.broker_order_id,
                "filled_quantity": response.filled_quantity,
                "average_price": response.average_price,
                "created_at": now.isoformat(),
                "execution_mode": str(
                    getattr(response, "execution_mode", None) or ""
                ).upper()
                or "UNKNOWN",
                "virtual": bool(getattr(response, "virtual", False)),
            }
            if self._async_csv_writer is not None:
                self._async_csv_writer.enqueue_order(
                    record,
                    hub_order_id=hub_order_id,
                )
            else:
                try:
                    self._csv_writer.write_order(record)
                except Exception:
                    logger.exception(
                        "OrderRouter: failed to write order CSV for hub_order_id=%s",
                        hub_order_id,
                    )
            bq_record = {
                "hub_order_id": record.get("hub_order_id"),
                "tenant_id": record.get("tenant_id"),
                "broker_account_id": record.get("broker_account_id"),
                "strategy_id": record.get("strategy_id"),
                "symbol": record.get("symbol"),
                "side": record.get("side"),
                "order_type": record.get("order_type"),
                "product_type": record.get("product_type"),
                "time_in_force": record.get("time_in_force"),
                "quantity": record.get("quantity"),
                "limit_price": record.get("limit_price"),
                "stop_price": record.get("stop_price"),
                "status": record.get("status"),
                "message": record.get("message"),
                "broker_order_id": record.get("broker_order_id"),
                "filled_quantity": record.get("filled_quantity"),
                "average_price": record.get("average_price"),
                "created_at": record.get("created_at"),
            }
            insert_order_record(
                bq_record,
                insert_id=(
                    str(hub_order_id).strip()
                    or str(response.broker_order_id or "").strip()
                    or None
                ),
            )
        except Exception:
            logger.exception(
                "OrderRouter: failed to record order to BigQuery for hub_order_id=%s",
                hub_order_id,
            )

    # Record trade and update PnL if the order was filled.
    def _record_trade_and_pnl(
        self,
        hub_order_id: str,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        order_req: OrderRequest,
        response: OrderResponse,
        contract_key: Optional[ContractKey] = None,
        ownership_strategy_id: Optional[StrategyId] = None,
    ) -> None:
        """
        Best-effort trade + PnL recording.

        Phase 1:
        - Treat fully filled orders as a single trade.
        - Use average_price if available, else limit_price.
        - Emit TradeEvent to PnLEngine if provided.
        """
        status_upper = str(response.status or "").upper()
        classified = classify_broker_status(response.status)
        filled_status = status_upper in {
            "FILLED",
            "FULL",
            "COMPLETE",
            "COMPLETED",
            "EXECUTED",
        } or is_terminal_fill(classified)
        partial_status = status_upper in {"PARTIALLY_FILLED", "PARTIAL"} or classified == OrderLifecycleState.PARTIAL_FILL
        logger.debug(
            "OrderRouter: _record_trade_and_pnl hub_order_id=%s raw_status=%s classified=%s filled=%s partial=%s",
            hub_order_id, status_upper, classified.value, filled_status, partial_status,
        )
        filled_qty = int(response.filled_quantity or 0)
        if filled_qty <= 0 and filled_status:
            filled_qty = int(order_req.quantity or 0)
        if filled_qty <= 0:
            return

        try:
            def _pick_price(*values: object) -> Optional[float]:
                for val in values:
                    try:
                        fval = float(val) if val is not None else 0.0
                    except (TypeError, ValueError):
                        continue
                    if fval > 0:
                        return fval
                return None

            price = _pick_price(
                response.average_price,
                order_req.limit_price,
                order_req.stop_price,
            )
            if price is None:
                price = _resolve_reference_price(order_req)
            if price is None or price <= 0:
                logger.warning(
                    "OrderRouter: missing price for filled order hub_order_id=%s status=%s filled_qty=%s",
                    hub_order_id,
                    status_upper,
                    filled_qty,
                )
                return

            signed_qty = filled_qty if order_req.side.value == "BUY" else -filled_qty
            trade_time = self._clock.now_utc()

            try:
                hk = HubOrderKey(
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    broker_order_id=response.broker_order_id or hub_order_id,
                )
                canonical_hub_order_id: Optional[HubOrderId] = make_hub_order_id(hk)
            except Exception:
                canonical_hub_order_id = HubOrderId(hub_order_id)

            record = build_trade_record_from_fill(
                trade_id=response.broker_order_id,
                hub_order_id=(
                    str(canonical_hub_order_id)
                    if canonical_hub_order_id
                    else hub_order_id
                ),
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                strategy_id=strategy_id,
                symbol=order_req.symbol,
                side=order_req.side.value,
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
            trade_row = trade_record_to_bq_row(record)
            trade_row_csv = dict(trade_row)
            trade_row_csv["execution_mode"] = str(
                getattr(response, "execution_mode", None) or ""
            ).upper() or "UNKNOWN"
            trade_row_csv["virtual"] = bool(getattr(response, "virtual", False))
            if self._async_csv_writer is not None:
                self._async_csv_writer.enqueue_trade(
                    trade_row_csv,
                    hub_order_id=hub_order_id,
                )
            else:
                try:
                    self._csv_writer.write_trade(trade_row_csv)
                except Exception:
                    logger.exception(
                        "OrderRouter: failed to write trade CSV for hub_order_id=%s",
                        hub_order_id,
                    )
            trade_insert_id = (
                f"{broker_account_id}:{trade_row.get('trade_id')}"
                if trade_row.get("trade_id")
                else (
                    f"{broker_account_id}:{response.broker_order_id}"
                    if response.broker_order_id
                    else None
                )
            )
            insert_trade_record(trade_row, insert_id=trade_insert_id)

            if self._pnl_engine is not None:
                event = TradeEvent(
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    strategy_id=strategy_id,
                    symbol=order_req.symbol,
                    qty=signed_qty,
                    price=price,
                    trade_time=trade_time,
                    fees=0.0,
                )
                pnl_before = self._pnl_engine.get_current_realized_pnl(
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    strategy_id=None,
                )
                self._pnl_engine.on_trade(event)
                # RISK-5.4: Feed trade result into circuit breaker for loss-streak tracking.
                if self._circuit_breaker is not None and signed_qty < 0:
                    # Only count closing trades (exits) for loss-streak detection.
                    pnl_after = self._pnl_engine.get_current_realized_pnl(
                        tenant_id=tenant_id,
                        broker_account_id=broker_account_id,
                        strategy_id=None,
                    )
                    if pnl_before is not None and pnl_after is not None:
                        trade_pnl_delta = float(pnl_after) - float(pnl_before)
                        self._circuit_breaker.record_trade_result(
                            is_loss=trade_pnl_delta < 0
                        )

            self._position_ownership_store.apply_fill(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                contract_key=contract_key,
                strategy_id=ownership_strategy_id or strategy_id,
                signed_qty=signed_qty,
            )
        except Exception:
            logger.exception(
                "OrderRouter: failed to record trade/PnL for hub_order_id=%s",
                hub_order_id,
            )

    def _release_position_ownership_pending(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        policy_ctx: OrderInterceptionContext,
    ) -> None:
        if not bool(policy_ctx.position_ownership_lock_acquired):
            return
        lock_strategy = (
            policy_ctx.position_ownership_fill_owner_strategy_id or strategy_id
        )
        try:
            self._position_ownership_store.release_pending(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                contract_key=policy_ctx.position_ownership_contract_key,
                strategy_id=lock_strategy,
            )
        finally:
            policy_ctx.position_ownership_lock_acquired = False

    # Submit an order through the hub with all checks applied.
    async def submit_order(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        order_req: OrderRequest,
    ) -> Tuple[str, OrderResponse]:
        """
        Main entrypoint for order submission.

        Returns:
            (hub_order_id, OrderResponse)

        Phase 1 hub_order_id is a simple string; later we can use a richer
        HubOrderKey / make_hub_order_id helper.
        """
        base_settings = get_settings()
        settings = get_runtime_settings(
            base_settings=base_settings,
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
        )
        idempotency_key = str(getattr(order_req, "idempotency_key", "") or "").strip()
        if getattr(order_req, "idempotency_key", None):
            hub_order_id = str(order_req.idempotency_key)
        else:
            hub_order_id = f"{tenant_id}:{broker_account_id}:{strategy_id}:{uuid4().hex}"
        if (
            self._startup_recovery_block_new_entries
            and not self._is_risk_reducing_order(order_req)
        ):
            reason = (
                self._startup_recovery_reason
                or "startup_recovery_degraded_block_new_entries"
            )
            log_event(
                logger,
                event_type="ORDER_STARTUP_RECOVERY_BLOCKED",
                message="Order blocked because startup recovery is degraded.",
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                strategy_id=strategy_id,
                instrument=order_req.symbol,
                level=logging.WARNING,
                reason=reason,
                summary=dict(self._last_recovery_summary),
            )
            return (
                hub_order_id,
                OrderResponse(
                    broker_order_id="STARTUP_RECOVERY_DEGRADED",
                    status="REJECTED",
                    message=reason,
                    filled_quantity=0,
                    average_price=None,
                ),
            )
        enforce_idempotency = bool(
            getattr(
                settings,
                "order_router_enforce_idempotency",
                getattr(base_settings, "order_router_enforce_idempotency", False),
            )
        )
        submission_key = self._submission_key(
            hub_order_id=hub_order_id,
            idempotency_key=idempotency_key,
            enforce_idempotency=enforce_idempotency,
        )
        correlation_id = idempotency_key or hub_order_id
        request_id = hub_order_id
        idempotency_claimed = False

        def _finalize_response(response: OrderResponse) -> Tuple[str, OrderResponse]:
            self._state_store.set_last_order_response(broker_account_id, response)
            if idempotency_claimed and self._submission_outbox is None:
                self._idempotency_store.complete(
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    strategy_id=strategy_id,
                    idempotency_key=idempotency_key,
                    response=response,
                )
            return hub_order_id, response

        if self._submission_outbox is None and enforce_idempotency and idempotency_key:
            claimed, replay_response = self._idempotency_store.claim(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                strategy_id=strategy_id,
                idempotency_key=idempotency_key,
            )
            if not claimed:
                duplicate = replay_response or self._pending_idempotency_response()
                log_event(
                    logger,
                    event_type="ORDER_IDEMPOTENCY_SUPPRESSED",
                    message="duplicate idempotency key suppressed",
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    strategy_id=strategy_id,
                    correlation_id=correlation_id,
                    request_id=request_id,
                    instrument=order_req.symbol,
                    level=logging.INFO,
                    hub_order_id=hub_order_id,
                    idempotency_key=idempotency_key,
                )
                return _finalize_response(duplicate)
            idempotency_claimed = True

        runner = self._hub.get_runner(broker_account_id)
        if runner is None or not runner.is_running:
            msg = (
                f"No active runner for broker_account_id={broker_account_id}; "
                f"cannot route order."
            )
            log_event(
                logger,
                event_type="ORDER_REJECTED",
                message=msg,
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                strategy_id=strategy_id,
                correlation_id=correlation_id,
                request_id=request_id,
                instrument=order_req.symbol,
                level=logging.WARNING,
            )
            response = OrderResponse(
                broker_order_id="",
                status="REJECTED",
                message=msg,
                filled_quantity=0,
                average_price=None,
            )
            return _finalize_response(response)

        ok_preflight, preflight_reason, lots, lot_size, broker_qty, underlying = _preflight_order_quantity(
            order_req
        )
        log_event(
            logger,
            event_type="ORDER_PREFLIGHT",
            message=preflight_reason,
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
            correlation_id=correlation_id,
            request_id=request_id,
            instrument=order_req.symbol,
            level=logging.INFO if ok_preflight else logging.WARNING,
            underlying=underlying,
            symbol=order_req.symbol,
            lots=lots,
            lot_size=lot_size,
            broker_qty=broker_qty,
        )
        if not ok_preflight or broker_qty is None:
            response = OrderResponse(
                broker_order_id="",
                status="REJECTED",
                message=preflight_reason,
                filled_quantity=0,
                average_price=None,
            )
            return _finalize_response(response)

        order_req_exec = replace(order_req, quantity=int(broker_qty))

        balance = self._state_store.get_balance(broker_account_id)
        positions = self._state_store.get_positions(broker_account_id)

        is_exit_order = False
        try:
            is_exit_order = bool(getattr(order_req, "is_exit_order", False))
        except Exception:
            is_exit_order = False

        reference_price = _resolve_reference_price(order_req_exec)
        instrument_meta = dashboard_bus.resolve_instrument_meta(
            symbol=order_req_exec.symbol,
            token=order_req_exec.symbol_token,
        )
        policy_ctx = OrderInterceptionContext(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
            correlation_id=correlation_id,
            request_id=request_id,
            order_req=order_req_exec,
            settings=settings,
            is_exit_order=is_exit_order,
            lot_size=lot_size,
            instrument_meta=instrument_meta,
            reference_price=reference_price,
            balance=balance,
            positions=positions,
            capital_engine=self._capital_engine,
            risk_engine=self._risk_engine,
            profit_engine=self._profit_engine,
            position_ownership_store=self._position_ownership_store,
            circuit_breaker=self._circuit_breaker,
        )
        for policy in self._policy_pipeline:
            rejected = policy.evaluate(policy_ctx)
            if rejected is not None:
                self._release_position_ownership_pending(
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    strategy_id=strategy_id,
                    policy_ctx=policy_ctx,
                )
                return _finalize_response(rejected)
        order_req_exec = policy_ctx.order_req

        if self._submission_outbox is not None:
            claimed, existing_record = self._submission_outbox.reserve_submission(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                strategy_id=strategy_id,
                submission_key=submission_key,
                hub_order_id=hub_order_id,
                order_req=order_req_exec,
                contract_key=policy_ctx.position_ownership_contract_key,
                ownership_strategy_id=(
                    policy_ctx.position_ownership_fill_owner_strategy_id
                    or strategy_id
                ),
                created_at=self._clock.now_utc(),
            )
            if not claimed:
                hub_order_id = str(existing_record.hub_order_id or hub_order_id)
                duplicate = (
                    existing_record.as_order_response()
                    or self._pending_idempotency_response()
                )
                log_event(
                    logger,
                    event_type="ORDER_IDEMPOTENCY_SUPPRESSED",
                    message="duplicate submission suppressed by durable outbox",
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    strategy_id=strategy_id,
                    correlation_id=correlation_id,
                    request_id=hub_order_id,
                    instrument=order_req.symbol,
                    level=logging.INFO,
                    hub_order_id=hub_order_id,
                    idempotency_key=idempotency_key or submission_key,
                    outbox_status=existing_record.status,
                )
                return _finalize_response(duplicate)
            self._submission_outbox.mark_submitting(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                strategy_id=strategy_id,
                submission_key=submission_key,
                updated_at=self._clock.now_utc(),
            )

        log_event(
            logger,
            event_type="ORDER_PASSED_RISK",
            message="order passed capital/risk checks",
            level=logging.INFO,
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
            correlation_id=correlation_id,
            request_id=request_id,
            instrument=order_req.symbol,
        )
        try:
            response = await runner.place_order(order_req_exec)
        except Exception as exc:
            self._record_outbox_submit_error(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                strategy_id=strategy_id,
                submission_key=submission_key,
                error_text=str(exc),
            )
            raise
        try:
            response.requested_quantity = int(order_req_exec.quantity)
        except Exception:
            response.requested_quantity = None
        self._record_outbox_response(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
            submission_key=submission_key,
            response=response,
        )
        logger.info(
            "OrderRouter: order routed hub_order_id=%s broker_order_id=%s status=%s",
            hub_order_id,
            response.broker_order_id,
            response.status,
        )
        log_event(
            logger,
            event_type="ORDER_PLACED",
            message="Order routed to broker.",
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
            correlation_id=correlation_id,
            request_id=request_id,
            instrument=order_req.symbol,
            level=logging.INFO,
            hub_order_id=hub_order_id,
            broker_order_id=response.broker_order_id,
            status=response.status,
            execution_mode=str(
                getattr(response, "execution_mode", None)
                or getattr(runner, "runtime_mode", "LIVE")
            ).upper(),
            virtual=bool(getattr(response, "virtual", False)),
            broker_call_blocked=bool(getattr(response, "virtual", False)),
        )
        status_upper = str(response.status or "").upper()
        classified_state = classify_broker_status(response.status)
        logger.info(
            "OrderRouter: classified broker status hub_order_id=%s raw_status=%s classified_state=%s terminal=%s terminal_fill=%s",
            hub_order_id,
            response.status,
            classified_state.value,
            is_terminal(classified_state),
            is_terminal_fill(classified_state),
        )
        filled_qty = int(getattr(response, "filled_quantity", 0) or 0)
        # Use canonical classification alongside legacy string set (backwards-compatible).
        is_terminal_non_fill = classified_state in TERMINAL_NON_FILL_STATES
        if (status_upper in _POSITION_LOCK_RELEASE_STATUSES or is_terminal_non_fill) and filled_qty <= 0:
            self._release_position_ownership_pending(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                strategy_id=strategy_id,
                policy_ctx=policy_ctx,
            )
        _finalize_response(response)
        # RISK-5.4: Feed order outcome into circuit breaker for reject-rate tracking.
        if self._circuit_breaker is not None:
            is_reject = status_upper in _POSITION_LOCK_RELEASE_STATUSES or is_terminal_non_fill
            self._circuit_breaker.record_order_result(is_reject=is_reject)
        self._record_order_to_bq(
            hub_order_id=hub_order_id,
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
            order_req=order_req_exec,
            response=response,
        )
        broker_order_id = str(response.broker_order_id or "").strip()
        requires_reconciliation = self._requires_broker_reconciliation(response)
        if requires_reconciliation:
            logger.warning(
                "OrderRouter: lifecycle registration deferred pending broker reconciliation hub_order_id=%s broker_order_id=%s status=%s",
                hub_order_id,
                broker_order_id,
                response.status,
            )
        if (
            self._order_lifecycle is not None
            and broker_order_id
            and not requires_reconciliation
        ):
            try:
                self._order_lifecycle.register_submission(
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    strategy_id=strategy_id,
                    hub_order_id=hub_order_id,
                    order_req=order_req_exec,
                    response=response,
                    contract_key=policy_ctx.position_ownership_contract_key,
                    ownership_strategy_id=(
                        policy_ctx.position_ownership_fill_owner_strategy_id
                        or strategy_id
                    ),
                )
            except Exception:
                logger.exception(
                    "OrderRouter: lifecycle registration failed for hub_order_id=%s broker_order_id=%s",
                    hub_order_id,
                    broker_order_id,
                )
                self._record_trade_and_pnl(
                    hub_order_id=hub_order_id,
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    strategy_id=strategy_id,
                    order_req=order_req_exec,
                    response=response,
                    contract_key=policy_ctx.position_ownership_contract_key,
                    ownership_strategy_id=(
                        policy_ctx.position_ownership_fill_owner_strategy_id
                        or strategy_id
                    ),
                )
        else:
            self._record_trade_and_pnl(
                hub_order_id=hub_order_id,
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                strategy_id=strategy_id,
                order_req=order_req_exec,
                response=response,
                contract_key=policy_ctx.position_ownership_contract_key,
                ownership_strategy_id=(
                    policy_ctx.position_ownership_fill_owner_strategy_id
                    or strategy_id
                ),
            )
        return hub_order_id, response

    async def recover_submission_outbox(self) -> dict[str, Any]:
        summary = {
            "scanned": 0,
            "rehydrated": 0,
            "adopted": 0,
            "replayed": 0,
            "deferred": 0,
            "failed": 0,
        }
        tenant_record_counts: dict[str, int] = {}
        broker_account_record_counts: dict[str, int] = {}
        if self._submission_outbox is None:
            self._last_recovery_summary = {
                **summary,
                "unresolved_active": 0,
                "tenant_count": 0,
                "broker_account_count": 0,
                "tenant_record_counts": {},
                "broker_account_record_counts": {},
            }
            return summary

        try:
            records = self._submission_outbox.list_active_submissions()
        except Exception:
            logger.exception("OrderRouter: failed to list active submission outbox records")
            summary["failed"] += 1
            self._last_recovery_summary = {
                **summary,
                "unresolved_active": 0,
                "tenant_count": 0,
                "broker_account_count": 0,
                "tenant_record_counts": {},
                "broker_account_record_counts": {},
            }
            return summary

        for record in records:
            summary["scanned"] += 1
            tenant_record_counts[record.tenant_id] = (
                int(tenant_record_counts.get(record.tenant_id, 0)) + 1
            )
            broker_account_record_counts[record.broker_account_id] = (
                int(broker_account_record_counts.get(record.broker_account_id, 0)) + 1
            )
            tenant_id = TenantId(record.tenant_id)
            broker_account_id = BrokerAccountId(record.broker_account_id)
            strategy_id = StrategyId(record.strategy_id)
            runner = self._hub.get_runner(broker_account_id)
            if runner is None or not runner.is_running:
                summary["deferred"] += 1
                continue

            snapshot_orders = (
                self._state_store.get_order_snapshot(broker_account_id)
                or self._state_store.get_orders(broker_account_id)
            )
            matched_order = self._match_recovery_order(
                record=record,
                orders=list(snapshot_orders or []),
            )
            if matched_order is not None:
                adopted_response = OrderResponse(
                    broker_order_id=str(getattr(matched_order, "order_id", "") or "").strip(),
                    status=str(getattr(matched_order, "status", "") or "OPEN"),
                    message="recovered_from_broker_snapshot",
                    filled_quantity=int(getattr(matched_order, "filled_quantity", 0) or 0),
                    average_price=getattr(matched_order, "average_price", None),
                )
                updated_record = (
                    self._record_outbox_response(
                        tenant_id=tenant_id,
                        broker_account_id=broker_account_id,
                        strategy_id=strategy_id,
                        submission_key=record.submission_key,
                        response=adopted_response,
                        recovery_action="broker_snapshot_adopted",
                    )
                    or record
                )
                self._rehydrate_lifecycle_from_outbox(
                    record=updated_record,
                    snapshot_order=matched_order,
                )
                summary["adopted"] += 1
                continue

            if record.status == "PENDING":
                try:
                    self._submission_outbox.mark_submitting(
                        tenant_id=tenant_id,
                        broker_account_id=broker_account_id,
                        strategy_id=strategy_id,
                        submission_key=record.submission_key,
                        updated_at=self._clock.now_utc(),
                    )
                    response = await runner.place_order(record.as_order_request())
                    try:
                        response.requested_quantity = int(
                            record.as_order_request().quantity
                        )
                    except Exception:
                        response.requested_quantity = None
                    updated_record = (
                        self._record_outbox_response(
                            tenant_id=tenant_id,
                            broker_account_id=broker_account_id,
                            strategy_id=strategy_id,
                            submission_key=record.submission_key,
                            response=response,
                            recovery_action="startup_replay",
                        )
                        or record
                    )
                    self._rehydrate_lifecycle_from_outbox(record=updated_record)
                    summary["replayed"] += 1
                except Exception as exc:
                    self._record_outbox_submit_error(
                        tenant_id=tenant_id,
                        broker_account_id=broker_account_id,
                        strategy_id=strategy_id,
                        submission_key=record.submission_key,
                        error_text=str(exc),
                    )
                    logger.exception(
                        "OrderRouter: failed to replay pending outbox submission_key=%s",
                        record.submission_key,
                    )
                    summary["failed"] += 1
                continue

            if record.status == "ACKED":
                self._rehydrate_lifecycle_from_outbox(record=record)
                summary["rehydrated"] += 1
                continue

            summary["deferred"] += 1

        full_summary = {
            **summary,
            "unresolved_active": int(summary["deferred"]),
            "tenant_count": len(tenant_record_counts),
            "broker_account_count": len(broker_account_record_counts),
            "tenant_record_counts": dict(sorted(tenant_record_counts.items())),
            "broker_account_record_counts": dict(
                sorted(broker_account_record_counts.items())
            ),
        }
        self._last_recovery_summary = dict(full_summary)

        log_event(
            logger,
            event_type="ORDER_OUTBOX_RECOVERY",
            message="Recovered durable order submission outbox.",
            level=logging.INFO if int(summary["failed"]) == 0 else logging.WARNING,
            **full_summary,
        )
        return full_summary


# Profit guard note:
# - OrderRequest now carries a purpose (ENTRY/EXIT/ADJUST).
# - ProfitEngine is skipped for EXIT orders; exit/square-off orders always bypass profit blocking.

__all__ = ["OrderRouter"]
