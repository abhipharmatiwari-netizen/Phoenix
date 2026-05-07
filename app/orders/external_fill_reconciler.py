"""Detect broker-side fills that Phoenix did not place itself and ingest
them as TradeEvents so pnl_snapshots stays in sync with broker reality.

Background
----------
``OrderLifecycleService._emit_trade`` only fires for orders Phoenix placed
through ``OrderRouter.submit_order`` — which means manual fills placed
directly via the broker's UI (or any non-Phoenix OMS), broker-side stop
losses, etc., never reach Phoenix's cash-flow ledger. The dashboard then
silently desyncs from the broker's truth.

The 2026-05-07 A1 incident is the canonical example: a manual MARKET
SELL @ 17.30 placed via Angel UI directly closed a long that Phoenix had
opened, but the +₹21,625 credit never landed in pnl_snapshots, so the UI
showed -₹17,875 instead of +₹3,750.

Design
------
On every broker-orders sync (already running ~90 s in
``AccountRunner._sync_orders``), iterate the freshly-fetched order list
and for each terminally-filled order:

  1. Skip if it was placed by Phoenix (``broker_order_id`` is in
     ``order_submission_outbox``). Phoenix's normal emit_trade flow
     handles those.
  2. Skip if already claimed in ``trade_processed_markers`` (idempotency
     across runner restarts).
  3. Otherwise: synthesize a ``TradeEvent`` with the broker's reported
     executed price, feed it to ``pnl_engine.on_trade(...)``, persist
     to ``trades``, and atomically claim ``trade_processed_markers``.

External fills are attributed to a sentinel strategy_id
``__external__`` so they show up in their own ``pnl_snapshots`` row and
don't pollute the strategy's own realized P&L attribution.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, List, Optional, Set

from app.core.identifiers import BrokerAccountId, StrategyId, TenantId
from app.data.trade_records import build_trade_record_from_fill, trade_record_to_bq_row
from app.data.trade_store_postgres import insert_trade_postgres
from app.orders.trade_processed_store import ProcessedTradeStore
from app.pnl.pnl_engine import PnLEngine
from app.pnl.types import TradeEvent

logger = logging.getLogger(__name__)


EXTERNAL_FILL_STRATEGY_ID: StrategyId = StrategyId("__external__")


# Status values broker.get_orders() can return that mean "this order has
# fully executed and no more state changes are expected for it".
_TERMINAL_FILLED_STATUSES = {
    "FILLED",
    "FULL",
    "COMPLETE",
    "COMPLETED",
    "EXECUTED",
}


def _field(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _to_int(value: Any) -> int:
    try:
        return int(float(value)) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return datetime.now(timezone.utc)


@dataclass
class _OrderProbe:
    """Compact view of one broker order for the reconciler's decision."""
    broker_order_id: str
    symbol: str
    side: str  # "BUY" | "SELL"
    filled_qty: int
    price: Optional[float]
    trade_time: datetime


def _extract_probe(order: Any) -> Optional[_OrderProbe]:
    raw_status = str(_field(order, "status") or "").strip().upper()
    if raw_status not in _TERMINAL_FILLED_STATUSES:
        return None
    broker_order_id = (
        str(_field(order, "order_id") or _field(order, "broker_order_id") or "").strip()
    )
    if not broker_order_id:
        return None
    symbol = str(
        _field(order, "symbol")
        or _field(order, "tradingsymbol")
        or _field(order, "trading_symbol")
        or ""
    ).strip()
    side = str(_field(order, "side") or _field(order, "transaction_type") or "").strip().upper()
    if not symbol or side not in ("BUY", "SELL"):
        return None
    # Prefer filled_quantity verbatim — a value of 0 means "no fill" and
    # must not silently fall back to the order's requested quantity.
    raw_filled = _field(order, "filled_quantity")
    if raw_filled is None:
        raw_filled = _field(order, "filled_qty")
    if raw_filled is None:
        raw_filled = _field(order, "quantity")
    filled_qty = _to_int(raw_filled)
    if filled_qty <= 0:
        return None
    price = _to_float(
        _field(order, "average_price")
        or _field(order, "avg_price")
        or _field(order, "price")
    )
    if price is None or price <= 0:
        return None
    return _OrderProbe(
        broker_order_id=broker_order_id,
        symbol=symbol,
        side=side,
        filled_qty=filled_qty,
        price=price,
        trade_time=_parse_dt(_field(order, "updated_at") or _field(order, "trade_time")),
    )


def _phoenix_placed_order_ids(
    *,
    broker_account_id: BrokerAccountId,
    broker_order_ids: List[str],
    dsn: Optional[str] = None,
) -> Set[str]:
    """Return the subset of broker_order_ids that appear in
    ``order_submission_outbox`` for this account — i.e. orders Phoenix
    placed itself. The reconciler skips these to avoid double-counting.
    """
    if not broker_order_ids:
        return set()
    if dsn is None:
        try:
            from app.data.postgres import get_control_plane_dsn
            dsn = get_control_plane_dsn()
        except Exception as exc:
            logger.debug("ExternalFillReconciler: cannot resolve DSN: %s", exc)
            return set()
    try:
        from app.data.postgres import connect_with_retry
        with connect_with_retry(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT broker_order_id FROM order_submission_outbox "
                    "WHERE broker_account_id = %s AND broker_order_id = ANY(%s)",
                    (str(broker_account_id), broker_order_ids),
                )
                return {str(row[0]) for row in cur.fetchall() if row and row[0]}
    except Exception as exc:
        logger.debug("ExternalFillReconciler: outbox lookup failed: %s", exc)
        # Fail-closed: treat all as Phoenix-placed when we can't tell, so
        # we never risk double-booking when the DB is unavailable.
        return set(broker_order_ids)


@dataclass
class ExternalFillReconciler:
    """Stateless coordinator. Single instance shared across all runners."""

    pnl_engine: PnLEngine
    processed_store: ProcessedTradeStore

    def reconcile_orders(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        orders: Iterable[Any],
    ) -> int:
        """Ingest any external fills found in ``orders``. Returns the count
        of newly-ingested external fills (0 in the steady state)."""
        probes: List[_OrderProbe] = []
        for order in orders or []:
            probe = _extract_probe(order)
            if probe is not None:
                probes.append(probe)
        if not probes:
            return 0

        phoenix_ids = _phoenix_placed_order_ids(
            broker_account_id=broker_account_id,
            broker_order_ids=[p.broker_order_id for p in probes],
        )

        ingested = 0
        for probe in probes:
            if probe.broker_order_id in phoenix_ids:
                continue
            try:
                claimed = self.processed_store.claim_processed(
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    strategy_id=EXTERNAL_FILL_STRATEGY_ID,
                    broker_order_id=probe.broker_order_id,
                    hub_order_id=probe.broker_order_id,
                    symbol=probe.symbol,
                    side=probe.side,
                    processed_at=probe.trade_time,
                )
            except Exception as exc:
                logger.debug(
                    "ExternalFillReconciler: claim_processed failed for %s: %s",
                    probe.broker_order_id, exc,
                )
                continue
            if not claimed:
                continue  # already booked elsewhere

            signed_qty = probe.filled_qty if probe.side == "BUY" else -probe.filled_qty
            try:
                self.pnl_engine.on_trade(
                    TradeEvent(
                        tenant_id=tenant_id,
                        broker_account_id=broker_account_id,
                        strategy_id=EXTERNAL_FILL_STRATEGY_ID,
                        symbol=probe.symbol,
                        qty=signed_qty,
                        price=probe.price or 0.0,
                        trade_time=probe.trade_time,
                        fees=0.0,
                    )
                )
            except Exception:
                logger.exception(
                    "ExternalFillReconciler: on_trade failed for %s",
                    probe.broker_order_id,
                )
                continue

            try:
                record = build_trade_record_from_fill(
                    trade_id=probe.broker_order_id,
                    hub_order_id=probe.broker_order_id,
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    strategy_id=EXTERNAL_FILL_STRATEGY_ID,
                    symbol=probe.symbol,
                    side=probe.side,
                    qty=probe.filled_qty,
                    price=probe.price or 0.0,
                    trade_time=probe.trade_time,
                    realized_pnl=None,
                    fees=None,
                    entry_price=None,
                    exit_price=None,
                    entry_time=None,
                    exit_time=None,
                )
                pg_record = dict(trade_record_to_bq_row(record))
                pg_record.setdefault("execution_mode", "EXTERNAL")
                pg_record.setdefault("virtual", False)
                insert_trade_postgres(pg_record)
            except Exception:
                logger.exception(
                    "ExternalFillReconciler: trade insert failed for %s",
                    probe.broker_order_id,
                )

            ingested += 1
            logger.warning(
                "external_fill_ingested tenant=%s account=%s broker_order_id=%s "
                "symbol=%s side=%s qty=%d price=%.4f trade_time=%s",
                str(tenant_id),
                str(broker_account_id),
                probe.broker_order_id,
                probe.symbol,
                probe.side,
                probe.filled_qty,
                probe.price or 0.0,
                probe.trade_time.isoformat(),
            )

        return ingested


__all__ = [
    "EXTERNAL_FILL_STRATEGY_ID",
    "ExternalFillReconciler",
]
