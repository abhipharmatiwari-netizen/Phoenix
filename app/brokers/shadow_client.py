"""
Shadow broker adapter.

Provides execution-time simulation without calling real broker APIs.
Fills are computed from dashboard LTP with configurable latency/slippage.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.brokers.base import (
    Balance,
    BrokerClient,
    OrderRequest,
    OrderResponse,
    OrderSide,
    OrderStatus,
    Position,
    ProductType,
)
from app.brokers.positions_types import PositionsFetchResult, PositionsStatus
from app.core.dashboard_bus import dashboard_bus
from app.tenants.models import BrokerAccountModel

logger = logging.getLogger(__name__)


def _parse_int_env(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.getenv(name, str(default))
    try:
        return max(minimum, int(float(raw)))
    except (TypeError, ValueError):
        return int(default)


def _parse_float_env(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.getenv(name, str(default))
    try:
        return max(minimum, float(raw))
    except (TypeError, ValueError):
        return float(default)


def _clamp_probability(value: object, *, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed < 0:
        return 0.0
    if parsed > 1:
        return 1.0
    return parsed


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_utc(value: object) -> Optional[datetime]:
    if isinstance(value, datetime):
        ts = value
    elif isinstance(value, str):
        txt = value.strip()
        if not txt:
            return None
        try:
            ts = datetime.fromisoformat(txt.replace("Z", "+00:00"))
        except Exception:
            return None
    else:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _safe_float(value: object) -> Optional[float]:
    try:
        if value is None:
            return None
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _safe_positive_float(value: object) -> Optional[float]:
    parsed = _safe_float(value)
    if parsed is None or parsed <= 0:
        return None
    return parsed


def _safe_int(value: object, *, minimum: int = 0) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return minimum
    return max(minimum, parsed)


class ShadowBrokerClient(BrokerClient):
    """
    Broker adapter for SHADOW runtime mode.

    Key invariants:
    - Never performs outbound broker API calls.
    - Uses market LTP + configurable latency/slippage for virtual fills.
    - Persists virtual order/position state for restart warm start.
    """

    def __init__(self, account: BrokerAccountModel) -> None:
        self._account = account
        self._lock = threading.RLock()
        self._orders: dict[str, OrderStatus] = {}
        self._order_sequence = 0
        self._positions: dict[str, Position] = {}
        self._orders_submitted = 0
        self._orders_filled = 0
        self._orders_rejected = 0
        self._fills_partial = 0
        self._fills_full = 0
        self._gross_realized_pnl = 0.0
        self._fees_total = 0.0
        self._started_at = _utc_now_iso()

        self._virtual_balance_base = _parse_float_env(
            "SHADOW_INITIAL_BALANCE", 1_00_00_000.0, minimum=0.0
        )
        self._latency_ms = _parse_int_env("SHADOW_FILL_LATENCY_MS", 150, minimum=0)
        self._slippage_bps = _parse_float_env(
            "SHADOW_SLIPPAGE_BPS", 2.5, minimum=0.0
        )
        self._slippage_abs = _parse_float_env(
            "SHADOW_SLIPPAGE_ABS", 0.0, minimum=0.0
        )
        self._slippage_min_ticks = _parse_int_env(
            "SHADOW_SLIPPAGE_MIN_TICKS", 1, minimum=0
        )
        self._tick_size = _parse_float_env("SHADOW_TICK_SIZE", 0.05, minimum=0.0001)
        self._partial_fill_prob = _clamp_probability(
            os.getenv("SHADOW_PARTIAL_FILL_PROB", "0.0"),
            default=0.0,
        )
        self._max_ltp_age_seconds = _parse_float_env(
            "SHADOW_MAX_LTP_AGE_SECONDS", 120.0, minimum=0.0
        )
        self._fee_per_order = _parse_float_env(
            "SHADOW_FEE_PER_ORDER", 0.0, minimum=0.0
        )
        self._fee_per_unit = _parse_float_env(
            "SHADOW_FEE_PER_UNIT", 0.0, minimum=0.0
        )

        seed_raw = str(os.getenv("SHADOW_RNG_SEED", "") or "").strip()
        rng_seed: Optional[int] = None
        if seed_raw:
            try:
                rng_seed = int(seed_raw)
            except ValueError:
                rng_seed = abs(hash(seed_raw))
            rng_seed = (
                int(rng_seed)
                + abs(hash(str(self._account.broker_account_id)))
            ) % (2**31 - 1)
        self._rng = random.Random(rng_seed)

        root = Path(__file__).resolve().parents[2]
        state_dir_default = root / "logs" / "shadow_state"
        state_dir = Path(os.getenv("SHADOW_STATE_DIR", str(state_dir_default)))
        account_key = str(self._account.broker_account_id or "default")
        safe_key = "".join(
            ch if (ch.isalnum() or ch in {"_", "-", "."}) else "_"
            for ch in account_key
        )
        self._state_path = state_dir / f"{safe_key}.json"
        self._load_state()

    async def login(self) -> None:
        logger.info(
            "ShadowBrokerClient login noop for broker_account_id=%s execution_mode=SHADOW broker_call_blocked=true",
            self._account.broker_account_id,
        )

    async def get_balance(self) -> Balance:
        with self._lock:
            net_realized = self._gross_realized_pnl - self._fees_total
            total = float(self._virtual_balance_base + net_realized)
        return Balance(available=total, utilized=0.0, total=total)

    async def get_positions(self) -> PositionsFetchResult:
        with self._lock:
            positions = list(self._positions.values())
        return PositionsFetchResult(
            status=PositionsStatus.OK,
            positions=positions,
            reason="ok",
        )

    async def get_orders(self) -> list[OrderStatus]:
        with self._lock:
            rows = sorted(
                self._orders.values(),
                key=lambda row: (str(row.updated_at or ""), str(row.order_id or "")),
            )
            return list(rows)

    async def place_order(self, request: OrderRequest) -> OrderResponse:
        with self._lock:
            self._orders_submitted += 1
        if self._latency_ms > 0:
            await asyncio.sleep(self._latency_ms / 1000.0)

        qty = _safe_int(request.quantity, minimum=0)
        if qty <= 0:
            with self._lock:
                self._orders_rejected += 1
            return OrderResponse(
                broker_order_id="",
                status="REJECTED",
                message="shadow_invalid_quantity",
                filled_quantity=0,
                average_price=None,
                execution_mode="SHADOW",
                virtual=True,
                details={
                    "execution_mode": "SHADOW",
                    "broker_call_blocked": True,
                    "reason": "invalid_quantity",
                },
            )

        raw_ltp, ltp_source = self._resolve_reference_ltp(request=request)
        if raw_ltp is None or raw_ltp <= 0:
            with self._lock:
                self._orders_rejected += 1
            logger.warning(
                "shadow_fill_rejected broker_account_id=%s symbol=%s reason=ltp_unavailable execution_mode=SHADOW broker_call_blocked=true",
                self._account.broker_account_id,
                request.symbol,
            )
            return OrderResponse(
                broker_order_id="",
                status="REJECTED",
                message="shadow_ltp_unavailable",
                filled_quantity=0,
                average_price=None,
                execution_mode="SHADOW",
                virtual=True,
                details={
                    "execution_mode": "SHADOW",
                    "broker_call_blocked": True,
                    "reason": "ltp_unavailable",
                    "ltp_source": ltp_source,
                },
            )

        side_txt = str(request.side.value if hasattr(request.side, "value") else request.side).upper()
        side = OrderSide.BUY if side_txt == "BUY" else OrderSide.SELL
        fill_price, slippage = self._apply_slippage(ltp=raw_ltp, side=side)
        filled_qty = qty
        status = "FILLED"
        if qty > 1 and self._partial_fill_prob > 0.0 and self._rng.random() < self._partial_fill_prob:
            status = "PARTIALLY_FILLED"
            filled_qty = max(1, qty // 2)

        broker_order_id = self._next_order_id()
        now_iso = _utc_now_iso()
        gross_realized_delta = 0.0
        fees = float(self._fee_per_order + (self._fee_per_unit * filled_qty))
        if filled_qty > 0:
            gross_realized_delta = self._apply_fill_to_positions(
                symbol=request.symbol,
                product_type=request.product_type,
                side=side,
                qty=filled_qty,
                fill_price=fill_price,
            )

        order_status = OrderStatus(
            order_id=broker_order_id,
            symbol=request.symbol,
            side=side.value,
            status=status,
            order_type=request.order_type.value,
            product_type=request.product_type.value,
            quantity=qty,
            filled_quantity=filled_qty,
            average_price=fill_price,
            price=fill_price,
            exchange=request.exchange or "SHADOW",
            variety="VIRTUAL",
            updated_at=now_iso,
            execution_mode="SHADOW",
            virtual=True,
        )
        with self._lock:
            self._orders[broker_order_id] = order_status
            self._gross_realized_pnl += gross_realized_delta
            self._fees_total += fees
            if status == "FILLED":
                self._orders_filled += 1
                self._fills_full += 1
            elif status == "PARTIALLY_FILLED":
                self._orders_filled += 1
                self._fills_partial += 1
        self._persist_state()

        details = {
            "execution_mode": "SHADOW",
            "broker_call_blocked": True,
            "raw_ltp": raw_ltp,
            "ltp_source": ltp_source,
            "latency_ms": self._latency_ms,
            "slippage_bps": self._slippage_bps,
            "slippage_abs": self._slippage_abs,
            "slippage_min_ticks": self._slippage_min_ticks,
            "applied_slippage": slippage,
            "final_fill_price": fill_price,
            "filled_qty": filled_qty,
            "requested_qty": qty,
            "fees": fees,
        }
        logger.info(
            "shadow_fill_model_applied broker_account_id=%s symbol=%s side=%s status=%s requested_qty=%s filled_qty=%s raw_ltp=%.6f fill_price=%.6f applied_slippage=%.6f latency_ms=%s execution_mode=SHADOW broker_call_blocked=true",
            self._account.broker_account_id,
            request.symbol,
            side.value,
            status,
            qty,
            filled_qty,
            raw_ltp,
            fill_price,
            slippage,
            self._latency_ms,
        )
        return OrderResponse(
            broker_order_id=broker_order_id,
            status=status,
            message="shadow_virtual_fill",
            filled_quantity=filled_qty,
            average_price=fill_price,
            requested_quantity=qty,
            execution_mode="SHADOW",
            virtual=True,
            details=details,
        )

    async def cancel_order(
        self,
        broker_order_id: str,
        *,
        symbol: str | None = None,
        variety: str | None = None,
    ) -> OrderResponse:
        _ = (symbol, variety)
        oid = str(broker_order_id or "").strip()
        if not oid:
            return OrderResponse(
                broker_order_id="",
                status="REJECTED",
                message="shadow_cancel_missing_order_id",
                filled_quantity=0,
                average_price=None,
                execution_mode="SHADOW",
                virtual=True,
                details={
                    "execution_mode": "SHADOW",
                    "broker_call_blocked": True,
                    "reason": "missing_order_id",
                },
            )
        with self._lock:
            row = self._orders.get(oid)
            if row is not None:
                row.status = "CANCELLED"
                row.updated_at = _utc_now_iso()
                row.execution_mode = "SHADOW"
                row.virtual = True
                self._orders[oid] = row
        self._persist_state()
        return OrderResponse(
            broker_order_id=oid,
            status="CANCELLED",
            message="shadow_virtual_cancel",
            filled_quantity=0,
            average_price=None,
            execution_mode="SHADOW",
            virtual=True,
            details={
                "execution_mode": "SHADOW",
                "broker_call_blocked": True,
            },
        )

    def shadow_metrics_snapshot(self) -> dict[str, Any]:
        with self._lock:
            open_positions = list(self._positions.values())
            open_qty = sum(abs(int(p.quantity or 0)) for p in open_positions)
            return {
                "execution_mode": "SHADOW",
                "virtual": True,
                "orders_submitted": self._orders_submitted,
                "orders_filled": self._orders_filled,
                "orders_rejected": self._orders_rejected,
                "fills_full": self._fills_full,
                "fills_partial": self._fills_partial,
                "open_positions": len(open_positions),
                "open_qty": open_qty,
                "gross_realized_pnl": self._gross_realized_pnl,
                "fees_total": self._fees_total,
                "net_realized_pnl": self._gross_realized_pnl - self._fees_total,
                "started_at": self._started_at,
            }

    def shadow_summary(self) -> dict[str, Any]:
        return self.shadow_metrics_snapshot()

    def _next_order_id(self) -> str:
        with self._lock:
            self._order_sequence += 1
            seq = self._order_sequence
        return f"SHADOW-{self._account.broker_account_id}-{seq:08d}"

    def _resolve_reference_ltp(self, *, request: OrderRequest) -> tuple[Optional[float], str]:
        def _from_label(label: str) -> tuple[Optional[float], str]:
            tick = dashboard_bus.get_last_tick(label)
            if not tick:
                return None, f"{label}:missing"
            price = _safe_positive_float(tick.get("price"))
            if price is None:
                return None, f"{label}:invalid_price"
            if self._max_ltp_age_seconds > 0:
                ts = _coerce_utc(tick.get("ts"))
                if ts is not None:
                    age = (datetime.now(timezone.utc) - ts).total_seconds()
                    if age > self._max_ltp_age_seconds:
                        return None, f"{label}:stale"
            return price, label

        meta = dashboard_bus.resolve_instrument_meta(
            symbol=request.symbol,
            token=request.symbol_token,
        )
        label = str(meta.get("label")) if meta and meta.get("label") else ""
        if label:
            ltp, source = _from_label(label)
            if ltp is not None:
                return ltp, source
        ltp_symbol, symbol_source = _from_label(request.symbol)
        if ltp_symbol is not None:
            return ltp_symbol, symbol_source
        fallback_price = _safe_positive_float(request.limit_price) or _safe_positive_float(
            request.stop_price
        )
        if fallback_price is not None:
            return fallback_price, "request_fallback"
        return None, label or request.symbol

    def _apply_slippage(self, *, ltp: float, side: OrderSide) -> tuple[float, float]:
        bps_slippage = abs(float(ltp)) * (self._slippage_bps / 10000.0)
        min_tick_slippage = float(self._slippage_min_ticks) * float(self._tick_size)
        slippage = max(bps_slippage + self._slippage_abs, min_tick_slippage)
        if side == OrderSide.BUY:
            raw_fill = ltp + slippage
            rounded = math.ceil(raw_fill / self._tick_size) * self._tick_size
        else:
            raw_fill = ltp - slippage
            rounded = math.floor(raw_fill / self._tick_size) * self._tick_size
        if rounded <= 0:
            rounded = self._tick_size
        return float(rounded), float(abs(rounded - ltp))

    def _apply_fill_to_positions(
        self,
        *,
        symbol: str,
        product_type: ProductType,
        side: OrderSide,
        qty: int,
        fill_price: float,
    ) -> float:
        if qty <= 0 or fill_price <= 0:
            return 0.0
        signed_delta = qty if side == OrderSide.BUY else -qty
        with self._lock:
            current = self._positions.get(symbol)
            current_qty = int(current.quantity) if current is not None else 0
            current_avg = float(current.avg_price) if current is not None else 0.0
            gross_realized = 0.0

            if current_qty == 0:
                new_qty = signed_delta
                new_avg = fill_price
            elif (current_qty > 0 and signed_delta > 0) or (current_qty < 0 and signed_delta < 0):
                new_qty = current_qty + signed_delta
                total_units = abs(current_qty) + abs(signed_delta)
                if total_units <= 0:
                    new_avg = 0.0
                else:
                    new_avg = (
                        (abs(current_qty) * current_avg) + (abs(signed_delta) * fill_price)
                    ) / total_units
            else:
                close_qty = min(abs(current_qty), abs(signed_delta))
                side_mult = 1.0 if current_qty > 0 else -1.0
                gross_realized = (fill_price - current_avg) * close_qty * side_mult
                new_qty = current_qty + signed_delta
                if new_qty == 0:
                    new_avg = 0.0
                elif (current_qty > 0 and new_qty > 0) or (current_qty < 0 and new_qty < 0):
                    new_avg = current_avg
                else:
                    new_avg = fill_price

            if new_qty == 0:
                self._positions.pop(symbol, None)
            else:
                self._positions[symbol] = Position(
                    symbol=symbol,
                    quantity=new_qty,
                    avg_price=float(new_avg),
                    product_type=product_type,
                    entry_ts=_utc_now_iso(),
                    execution_mode="SHADOW",
                    virtual=True,
                )
            return float(gross_realized)

    def _load_state(self) -> None:
        path = self._state_path
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(
                "ShadowBrokerClient failed to read state for broker_account_id=%s path=%s error=%s",
                self._account.broker_account_id,
                path,
                exc,
            )
            return

        with self._lock:
            self._order_sequence = _safe_int(payload.get("order_sequence"), minimum=0)

            raw_orders = payload.get("orders") if isinstance(payload, dict) else []
            self._orders.clear()
            if isinstance(raw_orders, list):
                for row in raw_orders:
                    if not isinstance(row, dict):
                        continue
                    oid = str(row.get("order_id") or "").strip()
                    if not oid:
                        continue
                    self._orders[oid] = OrderStatus(
                        order_id=oid,
                        symbol=str(row.get("symbol") or ""),
                        side=str(row.get("side") or ""),
                        status=str(row.get("status") or ""),
                        order_type=str(row.get("order_type") or ""),
                        product_type=str(row.get("product_type") or ""),
                        quantity=_safe_int(row.get("quantity"), minimum=0),
                        filled_quantity=_safe_int(row.get("filled_quantity"), minimum=0),
                        average_price=_safe_positive_float(row.get("average_price")),
                        price=_safe_positive_float(row.get("price")),
                        exchange=str(row.get("exchange") or "") or None,
                        variety=str(row.get("variety") or "") or None,
                        updated_at=str(row.get("updated_at") or "") or None,
                        execution_mode=str(row.get("execution_mode") or "SHADOW"),
                        virtual=bool(row.get("virtual", True)),
                    )

            raw_positions = payload.get("positions") if isinstance(payload, dict) else []
            self._positions.clear()
            if isinstance(raw_positions, list):
                for row in raw_positions:
                    if not isinstance(row, dict):
                        continue
                    symbol = str(row.get("symbol") or "").strip()
                    if not symbol:
                        continue
                    try:
                        product_type = ProductType(str(row.get("product_type") or "INTRADAY"))
                    except Exception:
                        product_type = ProductType.INTRADAY
                    self._positions[symbol] = Position(
                        symbol=symbol,
                        quantity=int(row.get("quantity") or 0),
                        avg_price=float(row.get("avg_price") or 0.0),
                        product_type=product_type,
                        entry_ts=str(row.get("entry_ts") or "") or None,
                        execution_mode=str(row.get("execution_mode") or "SHADOW"),
                        virtual=bool(row.get("virtual", True)),
                    )

            stats = payload.get("stats") if isinstance(payload, dict) else {}
            if isinstance(stats, dict):
                self._orders_submitted = _safe_int(stats.get("orders_submitted"), minimum=0)
                self._orders_filled = _safe_int(stats.get("orders_filled"), minimum=0)
                self._orders_rejected = _safe_int(stats.get("orders_rejected"), minimum=0)
                self._fills_partial = _safe_int(stats.get("fills_partial"), minimum=0)
                self._fills_full = _safe_int(stats.get("fills_full"), minimum=0)
                self._gross_realized_pnl = float(stats.get("gross_realized_pnl") or 0.0)
                self._fees_total = float(stats.get("fees_total") or 0.0)
                started_at = str(stats.get("started_at") or "").strip()
                if started_at:
                    self._started_at = started_at

        logger.info(
            "ShadowBrokerClient restored state for broker_account_id=%s orders=%d positions=%d path=%s execution_mode=SHADOW",
            self._account.broker_account_id,
            len(self._orders),
            len(self._positions),
            path,
        )

    def _persist_state(self) -> None:
        path = self._state_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                payload = {
                    "version": 1,
                    "broker_account_id": str(self._account.broker_account_id),
                    "updated_at": _utc_now_iso(),
                    "order_sequence": self._order_sequence,
                    "orders": [
                        {
                            "order_id": row.order_id,
                            "symbol": row.symbol,
                            "side": row.side,
                            "status": row.status,
                            "order_type": row.order_type,
                            "product_type": row.product_type,
                            "quantity": row.quantity,
                            "filled_quantity": row.filled_quantity,
                            "average_price": row.average_price,
                            "price": row.price,
                            "exchange": row.exchange,
                            "variety": row.variety,
                            "updated_at": row.updated_at,
                            "execution_mode": row.execution_mode or "SHADOW",
                            "virtual": bool(row.virtual if row.virtual is not None else True),
                        }
                        for row in sorted(
                            self._orders.values(),
                            key=lambda item: (str(item.updated_at or ""), str(item.order_id or "")),
                        )
                    ],
                    "positions": [
                        {
                            "symbol": pos.symbol,
                            "quantity": pos.quantity,
                            "avg_price": pos.avg_price,
                            "product_type": (
                                pos.product_type.value
                                if hasattr(pos.product_type, "value")
                                else str(pos.product_type)
                            ),
                            "entry_ts": pos.entry_ts,
                            "execution_mode": pos.execution_mode or "SHADOW",
                            "virtual": bool(pos.virtual if pos.virtual is not None else True),
                        }
                        for pos in self._positions.values()
                    ],
                    "stats": {
                        "orders_submitted": self._orders_submitted,
                        "orders_filled": self._orders_filled,
                        "orders_rejected": self._orders_rejected,
                        "fills_partial": self._fills_partial,
                        "fills_full": self._fills_full,
                        "gross_realized_pnl": self._gross_realized_pnl,
                        "fees_total": self._fees_total,
                        "started_at": self._started_at,
                    },
                }
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
                encoding="utf-8",
            )
            tmp_path.replace(path)
        except Exception as exc:
            logger.warning(
                "ShadowBrokerClient failed to persist state for broker_account_id=%s path=%s error=%s",
                self._account.broker_account_id,
                path,
                exc,
            )


__all__ = ["ShadowBrokerClient"]
