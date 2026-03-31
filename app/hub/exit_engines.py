"""
Hub-level profit sweep and end-of-day exit orchestrators.
Coordinates exits across runners based on PnL targets and schedules.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
import logging
from zoneinfo import ZoneInfo
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.config.settings import Settings
from app.core.clock import IClock, SystemClock
from app.core.dashboard_bus import dashboard_bus
from app.core.identifiers import BrokerAccountId, StrategyId, TenantId
from app.core.lot_size import lot_size_for_symbol_optional
from app.core.logging_utils import log_event
from app.data.state_store import StateStore
from app.pnl.pnl_engine import PnLEngine
from app.pnl.profit_engine import ProfitEngine as SweepProfitEngine
from app.pnl.profit_lock import ProfitLockManager
from app.orders.router import OrderRouter
from app.orders.position_ownership import (
    ContractKey,
    derive_contract_key_from_position,
    render_contract,
)
from app.brokers.base import (
    OrderRequest,
    OrderSide,
    OrderType,
    ProductType,
    TimeInForce,
    OrderPurpose,
    Position,
)
from app.hub.account_runner import AccountRunner
from app.hub.sweep_state import SweepStateManager

logger = logging.getLogger(__name__)

_EOD_CANCEL_IMMEDIATE_RETRY_MAX_ATTEMPTS = 3
_EOD_CANCEL_IMMEDIATE_RETRY_BACKOFF_SECONDS = 0.25


def _as_int(value: object) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _position_value(pos: object, key: str, default: object = None) -> object:
    if isinstance(pos, dict):
        return pos.get(key, default)
    return getattr(pos, key, default)


def _position_symbol_token(pos: Position) -> Optional[str]:
    token = (
        _position_value(pos, "symbol_token")
        or _position_value(pos, "token")
        or _position_value(pos, "instrument_token")
        or _position_value(pos, "symboltoken")
    )
    if token is None:
        return None
    text = str(token).strip()
    return text or None


def _is_probably_derivative_position(pos: Position, *, symbol: str) -> bool:
    exchange = str(
        _position_value(pos, "exchange") or _position_value(pos, "exch_seg") or ""
    ).strip().upper()
    if exchange.startswith(("NFO", "BFO", "MCX", "CDS", "NSEFO")):
        return True
    sym = symbol.upper()
    has_digit = any(ch.isdigit() for ch in sym)
    if has_digit and sym.endswith(("CE", "PE")):
        return True
    if "FUT" in sym or "OPT" in sym:
        return True
    return False


def _resolve_position_lot_size(pos: Position) -> Optional[int]:
    for candidate in (_position_value(pos, "lot_size"), _position_value(pos, "lotsize")):
        parsed = _as_int(candidate)
        if parsed is not None and parsed > 0:
            return parsed

    symbol = str(_position_value(pos, "symbol", "") or "").strip()
    meta = dashboard_bus.resolve_instrument_meta(
        symbol=symbol,
        token=_position_symbol_token(pos),
    )
    if isinstance(meta, dict):
        for key in ("lot_size", "lotSize", "lotsize"):
            parsed = _as_int(meta.get(key))
            if parsed is not None and parsed > 0:
                return parsed

    fallback = _as_int(lot_size_for_symbol_optional(symbol))
    if fallback is not None and fallback > 0:
        return fallback
    if not _is_probably_derivative_position(pos, symbol=symbol):
        return 1
    return None


def _resolve_exit_lots(
    pos: Position,
) -> tuple[Optional[int], int, Optional[int], str]:
    signed_units = _as_int(
        _position_value(pos, "quantity")
        if _position_value(pos, "quantity") is not None
        else _position_value(pos, "netqty")
    )
    if signed_units is None:
        return None, 0, None, "invalid_position_units"

    abs_units = abs(signed_units)
    if abs_units <= 0:
        return None, abs_units, None, "zero_position_units"

    lot_size = _resolve_position_lot_size(pos)
    if lot_size is None or lot_size <= 0:
        return None, abs_units, None, "lot_size_missing"

    if abs_units % lot_size != 0:
        return None, abs_units, lot_size, "units_not_multiple_of_lot_size"

    lots = abs_units // lot_size
    if lots <= 0:
        return None, abs_units, lot_size, "non_positive_lots"
    return lots, abs_units, lot_size, "ok"


def _coerce_product_type(
    value: object,
    *,
    default: Optional[ProductType] = None,
) -> Optional[ProductType]:
    candidate = getattr(value, "value", value)
    text = str(candidate or "").strip().upper()
    if text:
        try:
            return ProductType(text)
        except ValueError:
            pass
    return default


@dataclass(frozen=True)
class PositionExitPlan:
    position_symbol: str
    position_quantity: int
    exit_side: Optional[OrderSide]
    lots: Optional[int]
    broker_units: int
    lot_size: Optional[int]
    exchange: Optional[str]
    symbol_token: Optional[str]
    product_type: Optional[ProductType]
    contract_key: Optional[ContractKey]
    contract_text: str
    order_req: Optional[OrderRequest]
    reason: str

    @property
    def ok(self) -> bool:
        return self.reason == "ok" and self.order_req is not None


def build_position_exit_plan(
    pos: Position,
    *,
    tag: str,
    idempotency_key: Optional[str] = None,
    position_ownership_bypass: bool = True,
    exit_reason: Optional[str] = None,
    default_product_type: Optional[ProductType] = ProductType.INTRADAY,
    require_exchange: bool = False,
    require_symbol_token: bool = False,
    require_contract_key: bool = False,
    position_id: Optional[str] = None,
    contract_key_ref: Optional[str] = None,
    strategy_id: Optional[str] = None,
    account_id: Optional[str] = None,
) -> PositionExitPlan:
    symbol = str(_position_value(pos, "symbol", "") or "").strip()
    signed_units = _as_int(
        _position_value(pos, "quantity")
        if _position_value(pos, "quantity") is not None
        else _position_value(pos, "netqty")
    )
    if signed_units is None:
        return PositionExitPlan(
            position_symbol=symbol,
            position_quantity=0,
            exit_side=None,
            lots=None,
            broker_units=0,
            lot_size=None,
            exchange=None,
            symbol_token=None,
            product_type=None,
            contract_key=None,
            contract_text=render_contract(None),
            order_req=None,
            reason="invalid_position_units",
        )

    if signed_units > 0:
        exit_side = OrderSide.SELL
    elif signed_units < 0:
        exit_side = OrderSide.BUY
    else:
        return PositionExitPlan(
            position_symbol=symbol,
            position_quantity=0,
            exit_side=None,
            lots=None,
            broker_units=0,
            lot_size=None,
            exchange=None,
            symbol_token=None,
            product_type=None,
            contract_key=None,
            contract_text=render_contract(None),
            order_req=None,
            reason="zero_position_units",
        )

    lots, broker_units, lot_size, qty_reason = _resolve_exit_lots(pos)
    token = _position_symbol_token(pos)
    contract_key, contract_reason = derive_contract_key_from_position(pos)
    contract_text = render_contract(contract_key)
    exchange = (
        str(_position_value(pos, "exchange") or _position_value(pos, "exch_seg") or "").strip()
        or str(getattr(contract_key, "exchange", "") or "").strip()
        or None
    )
    symbol_token = (
        str(token).strip()
        if token not in (None, "")
        else str(getattr(contract_key, "broker_token", "") or "").strip() or None
    )
    product_type = _coerce_product_type(
        _position_value(pos, "product_type"),
        default=None,
    )
    if product_type is None and contract_key is not None:
        product_type = _coerce_product_type(contract_key.product_type, default=None)
    if product_type is None:
        product_type = default_product_type

    if require_contract_key and contract_key is None:
        return PositionExitPlan(
            position_symbol=symbol,
            position_quantity=signed_units,
            exit_side=exit_side,
            lots=lots,
            broker_units=broker_units,
            lot_size=lot_size,
            exchange=exchange,
            symbol_token=symbol_token,
            product_type=product_type,
            contract_key=None,
            contract_text=render_contract(None),
            order_req=None,
            reason=f"contract_key_missing:{contract_reason or 'unknown'}",
        )

    if lots is None:
        return PositionExitPlan(
            position_symbol=symbol,
            position_quantity=signed_units,
            exit_side=exit_side,
            lots=None,
            broker_units=broker_units,
            lot_size=lot_size,
            exchange=exchange,
            symbol_token=symbol_token,
            product_type=product_type,
            contract_key=contract_key,
            contract_text=contract_text,
            order_req=None,
            reason=qty_reason,
        )

    if not symbol:
        return PositionExitPlan(
            position_symbol="",
            position_quantity=signed_units,
            exit_side=exit_side,
            lots=lots,
            broker_units=broker_units,
            lot_size=lot_size,
            exchange=exchange,
            symbol_token=symbol_token,
            product_type=product_type,
            contract_key=contract_key,
            contract_text=contract_text,
            order_req=None,
            reason="symbol_missing",
        )

    if require_exchange and not exchange:
        return PositionExitPlan(
            position_symbol=symbol,
            position_quantity=signed_units,
            exit_side=exit_side,
            lots=lots,
            broker_units=broker_units,
            lot_size=lot_size,
            exchange=None,
            symbol_token=symbol_token,
            product_type=product_type,
            contract_key=contract_key,
            contract_text=contract_text,
            order_req=None,
            reason="exchange_missing",
        )

    if require_symbol_token and not symbol_token:
        return PositionExitPlan(
            position_symbol=symbol,
            position_quantity=signed_units,
            exit_side=exit_side,
            lots=lots,
            broker_units=broker_units,
            lot_size=lot_size,
            exchange=exchange,
            symbol_token=None,
            product_type=product_type,
            contract_key=contract_key,
            contract_text=contract_text,
            order_req=None,
            reason="symbol_token_missing",
        )

    if product_type is None:
        return PositionExitPlan(
            position_symbol=symbol,
            position_quantity=signed_units,
            exit_side=exit_side,
            lots=lots,
            broker_units=broker_units,
            lot_size=lot_size,
            exchange=exchange,
            symbol_token=symbol_token,
            product_type=None,
            contract_key=contract_key,
            contract_text=contract_text,
            order_req=None,
            reason="product_type_missing",
        )

    order_req = OrderRequest(
        symbol=symbol,
        quantity=lots,
        side=exit_side,
        order_type=OrderType.MARKET,
        product_type=product_type,
        time_in_force=TimeInForce.DAY,
        limit_price=None,
        stop_price=None,
        tag=tag,
        purpose=OrderPurpose.EXIT,
        exchange=exchange,
        symbol_token=symbol_token,
        idempotency_key=idempotency_key,
        position_ownership_bypass=position_ownership_bypass,
        exit_reason=exit_reason,
        position_id=position_id,
        contract_key_ref=contract_key_ref or contract_text,
        strategy_id=strategy_id,
        account_id=account_id,
    )
    return PositionExitPlan(
        position_symbol=symbol,
        position_quantity=signed_units,
        exit_side=exit_side,
        lots=lots,
        broker_units=broker_units,
        lot_size=lot_size,
        exchange=exchange,
        symbol_token=symbol_token,
        product_type=product_type,
        contract_key=contract_key,
        contract_text=contract_text,
        order_req=order_req,
        reason="ok",
    )


# Orchestrate profit sweep exits across accounts using persistent state.
@dataclass
class ProfitSweepEngine:
    """
    Hub-level profit sweep orchestrator with persistent state management.

    Uses SweepProfitEngine (PnL-based daily profit target) to decide when to
    sweep all open positions for a given (tenant, broker_account).
    
    Supports two strategies:
    - SIMPLE: Single daily sweep at PROFIT_DAILY_TARGET
    - MULTIPLE: Multiple sweeps per day with cooldown and limits
    
    State is persisted in Firestore for multi-instance coordination.
    """

    settings: Settings
    pnl_engine: PnLEngine
    sweep_engine: SweepProfitEngine
    state_store: StateStore
    order_router: OrderRouter
    sweep_state_manager: SweepStateManager
    profit_lock_manager: ProfitLockManager
    clock: Optional[IClock] = None
    _clock: IClock = field(init=False)
    
    # Legacy fields (kept for old-style profit sweep compatibility)
    _swept_today: Dict[Tuple[TenantId, BrokerAccountId], date] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        self._clock = self.clock or SystemClock()

    # Return today's date in the configured hub time zone.
    def _today(self) -> date:
        tz = ZoneInfo(self.settings.default_time_zone)
        return self._clock.now_local(tz).date()

    # Check legacy profit sweep configuration flags.
    def _config_enabled(self) -> bool:
        """Check if old-style profit sweep is enabled."""
        if not self.settings.enable_profit_checks:
            return False
        if not self.settings.profit_enable_daily_target:
            return False
        if (
            self.settings.profit_daily_target is None
            and self.settings.profit_daily_target_paper is None
        ):
            return False
        return True

    # Check if dual profit sweep (simple/multiple) is enabled.
    def _dual_sweep_enabled(self) -> bool:
        """Check if new dual profit sweep feature is enabled."""
        return (
            getattr(self.settings, "profit_sweep_enabled", False)
            and getattr(self.settings, "profit_sweep_strategy", "SIMPLE") in ["SIMPLE", "MULTIPLE"]
        )

    async def _try_execute_sweep(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        max_sweeps_per_day: int,
        cooldown_minutes: float = 30,
    ) -> Tuple[bool, Optional[str]]:
        # Sweep state store may perform sync Firestore/Postgres I/O.
        return await asyncio.to_thread(
            self.sweep_state_manager.try_execute_sweep,
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            max_sweeps_per_day=max_sweeps_per_day,
            cooldown_minutes=cooldown_minutes,
        )

    async def _record_sweep_execution(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
    ) -> bool:
        # Sweep state store may perform sync Firestore/Postgres I/O.
        return bool(
            await asyncio.to_thread(
                self.sweep_state_manager.record_sweep_execution,
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
            )
        )

    async def _get_sweep_status(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
    ) -> Dict:
        # Sweep state store may perform sync Firestore/Postgres I/O.
        return await asyncio.to_thread(
            self.sweep_state_manager.get_sweep_status,
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
        )

    # Run legacy profit sweep logic for all active runners.
    async def maybe_sweep_for_runners(
        self,
        runners: Iterable[AccountRunner],
    ) -> None:
        """
        For each active AccountRunner, check whether daily realized PnL meets
        the configured daily target via SweepProfitEngine. If it does and the
        account has not been swept today, submit EXIT orders for all open
        positions in that (tenant, broker_account) and mark as swept.
        """
        if not self._config_enabled():
            log_event(
                logger,
                event_type="PROFIT_SWEEP_SKIPPED_CONFIG",
                message="profit sweep disabled via settings",
                level=logging.DEBUG,
            )
            return

        today = self._today()
        excluded_positions = 0
        for runner in runners:
            if runner is None or not getattr(runner, "is_running", False):
                continue

            tenant_id = runner.tenant_id
            broker_account_id = runner.broker_account_id
            key = (tenant_id, broker_account_id)

            if self._swept_today.get(key) == today:
                continue

            is_paper = str(runner.runtime_mode).upper() == "PAPER"
            strategy_id = StrategyId("profit_sweep")

            decision = self.sweep_engine.check_should_sweep(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                strategy_id=strategy_id,
                is_paper=is_paper,
            )

            if not decision.should_sweep:
                continue

            log_event(
                logger,
                event_type="PROFIT_TARGET_REACHED",
                message=(
                    f"daily profit target reached: total={decision.current_total_pnl} "
                    f"target={decision.target}"
                ),
                level=logging.INFO,
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                strategy_id=strategy_id,
                total_pnl=decision.current_total_pnl,
                target=decision.target,
                reason=decision.reason,
            )

            positions: List[Position] = (
                self.state_store.get_positions(broker_account_id) or []
            )
            if not positions:
                log_event(
                    logger,
                    event_type="PROFIT_SWEEP_NO_POSITIONS",
                    message="profit sweep triggered but no open positions to close",
                    level=logging.INFO,
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    strategy_id=strategy_id,
                )
                self._swept_today[key] = today
                continue

            for pos in positions:
                signed_units = _as_int(getattr(pos, "quantity", None))
                if signed_units is None:
                    continue
                if signed_units > 0:
                    exit_side = OrderSide.SELL
                elif signed_units < 0:
                    exit_side = OrderSide.BUY
                else:
                    continue

                exchange = getattr(pos, "exchange", None) or getattr(pos, "exch_seg", None)
                if self._is_excluded_exchange(exchange):
                    excluded_positions += 1
                    continue

                lots, broker_units, lot_size, qty_reason = _resolve_exit_lots(pos)
                if lots is None:
                    log_event(
                        logger,
                        event_type="PROFIT_SWEEP_EXIT_SKIPPED_QTY",
                        message="skipping exit: unsafe units-to-lots conversion",
                        level=logging.WARNING,
                        tenant_id=tenant_id,
                        broker_account_id=broker_account_id,
                        strategy_id=strategy_id,
                        symbol=pos.symbol,
                        side=exit_side.name,
                        broker_units=broker_units,
                        lot_size=lot_size,
                        reason=qty_reason,
                    )
                    continue

                token = _position_symbol_token(pos)
                order_req = OrderRequest(
                    symbol=pos.symbol,
                    quantity=lots,
                    side=exit_side,
                    order_type=OrderType.MARKET,
                    product_type=pos.product_type or ProductType.INTRADAY,
                    time_in_force=TimeInForce.DAY,
                    limit_price=None,
                    stop_price=None,
                    tag="DAILY_PROFIT_SWEEP",
                    purpose=OrderPurpose.EXIT,
                    exchange=exchange,
                    symbol_token=str(token) if token is not None else None,
                    position_ownership_bypass=True,
                )

                try:
                    hub_order_id, resp = await self.order_router.submit_order(
                        tenant_id=tenant_id,
                        broker_account_id=broker_account_id,
                        strategy_id=strategy_id,
                        order_req=order_req,
                    )
                except Exception as exc:
                    log_event(
                        logger,
                        event_type="PROFIT_SWEEP_EXIT_FAILED",
                        message="profit sweep exit order failed",
                        level=logging.ERROR,
                        tenant_id=tenant_id,
                        broker_account_id=broker_account_id,
                        strategy_id=strategy_id,
                        symbol=pos.symbol,
                        qty=lots,
                        side=exit_side.name,
                        broker_units=broker_units,
                        lot_size=lot_size,
                        error=repr(exc),
                    )
                    continue

                log_event(
                    logger,
                    event_type="PROFIT_SWEEP_ORDER",
                    message="submitted profit-sweep exit order",
                    level=logging.INFO,
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    strategy_id=strategy_id,
                    symbol=pos.symbol,
                    qty=lots,
                    side=exit_side.name,
                    broker_units=broker_units,
                    lot_size=lot_size,
                    hub_order_id=hub_order_id,
                    status=getattr(resp, "status", None),
                    reason=getattr(resp, "message", None),
                )

            self._swept_today[key] = today
            log_event(
                logger,
                event_type="PROFIT_SWEEP_TRIGGERED",
                message="submitted profit-sweep exit orders for account",
                level=logging.INFO,
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                strategy_id=strategy_id,
                total_pnl=decision.current_total_pnl,
                target=decision.target,
                position_count=len(positions),
            )

    # Run SIMPLE daily profit sweep if target is reached.
    async def maybe_sweep_simple(
        self,
        runners: Iterable[AccountRunner],
    ) -> None:
        """
        SIMPLE dual profit sweep strategy: single daily sweep at configured target.
        
        Once daily realized PnL reaches PROFIT_DAILY_TARGET, exit ALL positions
        and do not sweep again until next trading day.
        Uses persistent Firestore state for multi-instance coordination.
        """
        if not self._dual_sweep_enabled():
            return
        
        strategy = getattr(self.settings, "profit_sweep_strategy", "SIMPLE")
        if strategy != "SIMPLE":
            return
        
        if not getattr(self.settings, "profit_simple_enabled", True):
            log_event(
                logger,
                event_type="PROFIT_SWEEP_SIMPLE_DISABLED",
                message="SIMPLE sweep strategy disabled",
                level=logging.DEBUG,
            )
            return
        
        daily_target = getattr(self.settings, "profit_daily_target", None)
        if daily_target is None or daily_target <= 0:
            log_event(
                logger,
                event_type="PROFIT_SWEEP_SIMPLE_INVALID",
                message="SIMPLE strategy requires valid profit_daily_target",
                level=logging.WARNING,
            )
            return
        
        for runner in runners:
            if runner is None or not getattr(runner, "is_running", False):
                continue
            
            tenant_id = runner.tenant_id
            broker_account_id = runner.broker_account_id
            is_paper = str(runner.runtime_mode).upper() == "PAPER"
            strategy_id = StrategyId("profit_sweep_simple")
            
            if is_paper and getattr(self.settings, "profit_daily_target_paper", None):
                daily_target = getattr(self.settings, "profit_daily_target_paper")

            total_pnl = self.pnl_engine.get_current_total_pnl(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
            )
            if total_pnl is None:
                continue

            signal_valid = None
            try:
                signal_valid = self.state_store.get_strategy_signal_valid(
                    broker_account_id
                )
            except Exception:
                signal_valid = None

            exit_reason = None
            lock_floor = None
            peak_total_pnl = None

            if not getattr(self.settings, "profit_lock_enabled", True):
                if total_pnl < daily_target:
                    continue
            else:
                lock_decision = self.profit_lock_manager.evaluate(
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    total_pnl=total_pnl,
                    target=daily_target,
                    giveback_pct=float(getattr(self.settings, "profit_lock_giveback_pct", 0.1)),
                    signal_valid=signal_valid,
                    require_signal=bool(getattr(self.settings, "profit_lock_require_signal", True)),
                    exit_cooldown_seconds=float(
                        getattr(self.settings, "profit_lock_exit_cooldown_seconds", 30.0)
                    ),
                )

                if not lock_decision.lock_active:
                    continue

                if not lock_decision.exit_required:
                    log_event(
                        logger,
                        event_type="PROFIT_LOCK_ACTIVE",
                        message="SIMPLE: profit lock active; holding positions",
                        level=logging.DEBUG,
                        tenant_id=tenant_id,
                        broker_account_id=broker_account_id,
                        total_pnl=total_pnl,
                        lock_floor=lock_decision.lock_floor,
                        peak_total_pnl=lock_decision.peak_total_pnl,
                        signal_valid=lock_decision.signal_valid,
                        target=daily_target,
                    )
                    continue

                exit_reason = lock_decision.exit_reason
                lock_floor = lock_decision.lock_floor
                peak_total_pnl = lock_decision.peak_total_pnl

            # Check if sweep allowed using persistent state
            # SIMPLE strategy: max 1 sweep per day
            can_sweep, reason = await self._try_execute_sweep(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                max_sweeps_per_day=1,  # SIMPLE allows only 1 per day
            )

            if not can_sweep:
                log_event(
                    logger,
                    event_type="PROFIT_SWEEP_SIMPLE_BLOCKED",
                    message=f"SIMPLE: sweep blocked by state: {reason}",
                    level=logging.DEBUG,
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    reason=reason,
                )
                continue

            log_event(
                logger,
                event_type="PROFIT_SWEEP_SIMPLE_TARGET_REACHED",
                message=f"SIMPLE: profit exit triggered: total={total_pnl}, target={daily_target}",
                level=logging.INFO,
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                total_pnl=total_pnl,
                target=daily_target,
                lock_floor=lock_floor,
                peak_total_pnl=peak_total_pnl,
                exit_reason=exit_reason,
            )

            exit_tag = "SIMPLE_PROFIT_SWEEP"
            if exit_reason:
                exit_tag = "PROFIT_LOCK_EXIT"
            
            positions: List[Position] = (
                self.state_store.get_positions(broker_account_id) or []
            )
            if not positions:
                log_event(
                    logger,
                    event_type="PROFIT_SWEEP_SIMPLE_NO_POSITIONS",
                    message="SIMPLE: triggered but no open positions",
                    level=logging.INFO,
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                )
                # Record sweep even with no positions (to enforce daily limit)
                await self._record_sweep_execution(
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                )
                continue

            # Exit all positions
            await self._exit_all_positions(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                strategy_id=strategy_id,
                positions=positions,
                reason=exit_tag,
            )

            # Record sweep in persistent state
            await self._record_sweep_execution(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
            )

            log_event(
                logger,
                event_type="PROFIT_SWEEP_SIMPLE_EXECUTED",
                message="SIMPLE: profit sweep completed",
                level=logging.INFO,
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                total_pnl=total_pnl,
                target=daily_target,
                position_count=len(positions),
            )

    # Run MULTIPLE profit sweep with cooldown and daily limits.
    async def maybe_sweep_multiple(
        self,
        runners: Iterable[AccountRunner],
    ) -> None:
        """
        MULTIPLE dual profit sweep strategy: multiple sweeps per day with cooldown.
        
        Each time daily realized PnL reaches PROFIT_MULTIPLE_TARGET, exit ALL positions
        and wait PROFIT_SWEEP_COOLDOWN_MINUTES before next sweep. Maximum PROFIT_MAX_SWEEPS_PER_DAY.
        Uses persistent Firestore state for multi-instance coordination.
        """
        if not self._dual_sweep_enabled():
            return
        
        strategy = getattr(self.settings, "profit_sweep_strategy", "SIMPLE")
        if strategy != "MULTIPLE":
            return
        
        if not getattr(self.settings, "profit_multiple_enabled", False):
            log_event(
                logger,
                event_type="PROFIT_SWEEP_MULTIPLE_DISABLED",
                message="MULTIPLE sweep strategy disabled",
                level=logging.DEBUG,
            )
            return
        
        multiple_target = getattr(self.settings, "profit_multiple_target", None)
        cooldown_minutes = getattr(self.settings, "profit_sweep_cooldown_minutes", 30)
        max_sweeps = getattr(self.settings, "profit_max_sweeps_per_day", 5)
        
        if multiple_target is None or multiple_target <= 0:
            log_event(
                logger,
                event_type="PROFIT_SWEEP_MULTIPLE_INVALID",
                message="MULTIPLE strategy requires valid profit_multiple_target",
                level=logging.WARNING,
            )
            return
        
        for runner in runners:
            if runner is None or not getattr(runner, "is_running", False):
                continue
            
            tenant_id = runner.tenant_id
            broker_account_id = runner.broker_account_id
            strategy_id = StrategyId("profit_sweep_multiple")
            is_paper = str(runner.runtime_mode).upper() == "PAPER"
            
            # Check if sweep allowed using persistent state
            # MULTIPLE strategy: max N sweeps per day with cooldown
            can_sweep, reason = await self._try_execute_sweep(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                max_sweeps_per_day=max_sweeps,
                cooldown_minutes=cooldown_minutes,
            )
            
            if not can_sweep:
                log_event(
                    logger,
                    event_type="PROFIT_SWEEP_MULTIPLE_BLOCKED",
                    message=f"MULTIPLE: sweep blocked by state: {reason}",
                    level=logging.DEBUG,
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    reason=reason,
                )
                continue
            
            # Get current realized PnL
            total_pnl = self.pnl_engine.get_current_total_pnl(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
            )
            
            if total_pnl is None or total_pnl < multiple_target:
                continue
            
            # Get sweep status for logging
            status = await self._get_sweep_status(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
            )
            sweep_count = status["sweep_count_today"]
            
            log_event(
                logger,
                event_type="PROFIT_SWEEP_MULTIPLE_TARGET_REACHED",
                message=f"MULTIPLE: profit target reached: total={total_pnl}, target={multiple_target}",
                level=logging.INFO,
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                total_pnl=total_pnl,
                target=multiple_target,
                sweep_count=sweep_count,
                max_sweeps=max_sweeps,
            )
            
            positions: List[Position] = (
                self.state_store.get_positions(broker_account_id) or []
            )
            if not positions:
                log_event(
                    logger,
                    event_type="PROFIT_SWEEP_MULTIPLE_NO_POSITIONS",
                    message="MULTIPLE: triggered but no open positions",
                    level=logging.INFO,
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    sweep_count=sweep_count,
                )
                # Record sweep even with no positions (to enforce limits)
                await self._record_sweep_execution(
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                )
                continue
            
            # Exit all positions
            exited_count = await self._exit_all_positions(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                strategy_id=strategy_id,
                positions=positions,
                reason="MULTIPLE_PROFIT_SWEEP",
            )
            
            # Record sweep in persistent state
            await self._record_sweep_execution(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
            )
            
            # Get updated status
            updated_status = await self._get_sweep_status(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
            )
            new_count = updated_status["sweep_count_today"]
            
            log_event(
                logger,
                event_type="PROFIT_SWEEP_MULTIPLE_EXECUTED",
                message="MULTIPLE: profit sweep completed",
                level=logging.INFO,
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                total_pnl=total_pnl,
                target=multiple_target,
                position_count=len(positions),
                exited_count=exited_count,
                sweep_count=new_count,
                max_sweeps=max_sweeps,
                cooldown_minutes=cooldown_minutes,
            )

    # Exit all open positions and return the count of exit orders sent.
    async def _exit_all_positions(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        positions: List[Position],
        reason: str,
    ) -> int:
        """
        Helper to exit all positions. Returns count of successful exits.

        M3: Checks DegradedScopeManager exit restriction before each exit.
        Only operator-reviewed or break-glass actions may proceed when restricted.
        """
        from app.core.degraded_scope_manager import degraded_scope_manager

        exited_count = 0
        is_break_glass = "break_glass" in str(reason).lower()

        for pos in positions:
            # M3: Skip exit if scope is exit-restricted (§13.2)
            symbol = str(_position_value(pos, "symbol", "") or "")
            scope_key = f"{broker_account_id}:{symbol}"
            if not is_break_glass and degraded_scope_manager.is_exit_restricted(scope_key):
                logger.warning(
                    "exit_engines: skipping exit for %s — exit restricted by "
                    "DegradedScopeManager (§13.2). Only break-glass may proceed.",
                    scope_key,
                )
                continue
            exchange = _position_value(pos, "exchange") or _position_value(pos, "exch_seg")
            if self._is_excluded_exchange(exchange):
                continue

            exit_plan = build_position_exit_plan(
                pos,
                tag=reason,
                position_ownership_bypass=True,
                exit_reason=reason,
                account_id=str(broker_account_id),
                strategy_id=str(strategy_id),
            )
            if not exit_plan.ok or exit_plan.order_req is None:
                log_event(
                    logger,
                    event_type="PROFIT_SWEEP_EXIT_SKIPPED_QTY",
                    message=f"skipping exit ({reason}): unsafe units-to-lots conversion",
                    level=logging.WARNING,
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    strategy_id=strategy_id,
                    symbol=exit_plan.position_symbol,
                    side=(exit_plan.exit_side.name if exit_plan.exit_side is not None else None),
                    broker_units=exit_plan.broker_units,
                    lot_size=exit_plan.lot_size,
                    reason=exit_plan.reason,
                )
                continue

            try:
                hub_order_id, resp = await self.order_router.submit_order(
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    strategy_id=strategy_id,
                    order_req=exit_plan.order_req,
                )
                exited_count += 1
                
                log_event(
                    logger,
                    event_type="PROFIT_SWEEP_EXIT_ORDER",
                    message=f"submitted exit order ({reason})",
                    level=logging.INFO,
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    strategy_id=strategy_id,
                    symbol=exit_plan.position_symbol,
                    qty=exit_plan.lots,
                    side=(exit_plan.exit_side.name if exit_plan.exit_side is not None else None),
                    broker_units=exit_plan.broker_units,
                    lot_size=exit_plan.lot_size,
                    hub_order_id=hub_order_id,
                    status=getattr(resp, "status", None),
                    reason=reason,
                )
            except Exception as exc:
                log_event(
                    logger,
                    event_type="PROFIT_SWEEP_EXIT_ERROR",
                    message=f"exit order failed ({reason})",
                    level=logging.ERROR,
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    strategy_id=strategy_id,
                    symbol=exit_plan.position_symbol,
                    qty=exit_plan.lots,
                    side=(exit_plan.exit_side.name if exit_plan.exit_side is not None else None),
                    broker_units=exit_plan.broker_units,
                    lot_size=exit_plan.lot_size,
                    error=repr(exc),
                    reason=reason,
                )
                continue
        
        return exited_count


from app.hub.eod_state import EODStateManager

# Orchestrate end-of-day exits across all active accounts.
@dataclass
class EODExitEngine:
    """
    Hub-level end-of-day exit orchestrator.

    When enabled, after the configured EOD time in the hub's default_time_zone,
    it sends EXIT market orders for all open positions across all active
    accounts, once per trading day.
    """

    settings: Settings
    state_store: StateStore
    order_router: OrderRouter
    eod_state_manager: Optional[EODStateManager] = None
    clock: Optional[IClock] = None
    _clock: IClock = field(init=False)
    _exited_today: Optional[date] = None
    _eod_time: Optional[time] = None
    _retry_cutoff_time: Optional[time] = None
    _exclude_exchanges: set[str] = field(default_factory=set)
    _tz: ZoneInfo = field(init=False)
    _retry_on_no_eligible: bool = False
    _require_fresh_position_sync: bool = False
    _positions_max_age_seconds: float = 60.0
    _position_telemetry: bool = True
    _cancel_open_orders_enabled: bool = True
    _cancel_retry_loop_enabled: bool = False
    _submitted_exit_keys: set[str] = field(default_factory=set)
    _submitted_exit_date: Optional[date] = None

    # Initialize time zone and excluded exchange settings.
    def __post_init__(self) -> None:
        self._clock = self.clock or SystemClock()
        self._tz = ZoneInfo(self.settings.default_time_zone)
        self._eod_time = self._parse_eod_time(
            getattr(self.settings, "eod_exit_time", ""),
            field_name="eod_exit_time",
        )
        self._retry_cutoff_time = self._parse_eod_time(
            getattr(self.settings, "eod_exit_retry_cutoff_time", "15:30"),
            field_name="eod_exit_retry_cutoff_time",
        )
        raw_excl = getattr(self.settings, "eod_exit_excluded_exchanges", "") or ""
        self._exclude_exchanges = {
            s.strip().upper()
            for s in raw_excl.split(",")
            if s and s.strip()
        }
        self._retry_on_no_eligible = bool(
            getattr(self.settings, "eod_exit_retry_on_no_eligible", False)
        )
        self._require_fresh_position_sync = bool(
            getattr(self.settings, "eod_exit_require_fresh_position_sync", False)
        )
        self._position_telemetry = bool(
            getattr(self.settings, "eod_exit_position_telemetry", True)
        )
        self._cancel_open_orders_enabled = bool(
            getattr(self.settings, "eod_cancel_open_orders_enabled", True)
        )
        self._cancel_retry_loop_enabled = bool(
            getattr(self.settings, "eod_cancel_retry_loop_enabled", False)
        )
        try:
            max_age = float(
                getattr(self.settings, "eod_exit_positions_max_age_seconds", 60)
            )
        except (TypeError, ValueError):
            max_age = 60.0
        self._positions_max_age_seconds = max(1.0, max_age)

    async def _cancel_open_orders_with_immediate_retries(
        self,
        *,
        cancel_fn: Any,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
    ) -> dict[str, int]:
        attempted_total = 0
        cancelled_total = 0
        unsupported_total = 0
        final_failed = 0
        attempts_run = 0
        exhausted = False

        for attempt in range(1, _EOD_CANCEL_IMMEDIATE_RETRY_MAX_ATTEMPTS + 1):
            attempts_run = attempt
            try:
                cancel_summary = await cancel_fn(
                    exclude_exchanges=self._exclude_exchanges,
                    reason_tag="EOD_CANCEL_OPEN_ORDERS",
                )
                pass_attempted = int(cancel_summary.get("attempted", 0) or 0)
                pass_cancelled = int(cancel_summary.get("cancelled", 0) or 0)
                pass_failed = int(cancel_summary.get("failed", 0) or 0)
                pass_unsupported = int(cancel_summary.get("unsupported", 0) or 0)
                attempted_total += pass_attempted
                cancelled_total += pass_cancelled
                unsupported_total += pass_unsupported
                final_failed = pass_failed
                should_retry = (
                    pass_failed > 0
                    and attempt < _EOD_CANCEL_IMMEDIATE_RETRY_MAX_ATTEMPTS
                )
                log_event(
                    logger,
                    event_type="EOD_CANCEL_OPEN_ORDERS_RETRY_ATTEMPT",
                    message="EOD open-order cancel retry attempt completed",
                    level=logging.INFO if pass_failed == 0 else logging.WARNING,
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    attempt=attempt,
                    max_attempts=_EOD_CANCEL_IMMEDIATE_RETRY_MAX_ATTEMPTS,
                    attempted=pass_attempted,
                    cancelled=pass_cancelled,
                    failed=pass_failed,
                    unsupported=pass_unsupported,
                    will_retry=should_retry,
                )
                if not should_retry:
                    exhausted = (
                        pass_failed > 0
                        and attempt == _EOD_CANCEL_IMMEDIATE_RETRY_MAX_ATTEMPTS
                    )
                    break
            except Exception as exc:
                final_failed = 1
                should_retry = attempt < _EOD_CANCEL_IMMEDIATE_RETRY_MAX_ATTEMPTS
                log_event(
                    logger,
                    event_type="EOD_CANCEL_OPEN_ORDERS_RETRY_ATTEMPT",
                    message="EOD open-order cancel retry attempt raised transient error",
                    level=logging.WARNING,
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    attempt=attempt,
                    max_attempts=_EOD_CANCEL_IMMEDIATE_RETRY_MAX_ATTEMPTS,
                    error=repr(exc),
                    will_retry=should_retry,
                )
                if not should_retry:
                    exhausted = attempt == _EOD_CANCEL_IMMEDIATE_RETRY_MAX_ATTEMPTS
                    log_event(
                        logger,
                        event_type="EOD_CANCEL_OPEN_ORDERS_FAILED",
                        message="EOD open-order cancel pass failed",
                        level=logging.WARNING,
                        tenant_id=tenant_id,
                        broker_account_id=broker_account_id,
                        error=repr(exc),
                        retry_attempt=attempt,
                        retry_max_attempts=_EOD_CANCEL_IMMEDIATE_RETRY_MAX_ATTEMPTS,
                    )
                    break

            exhausted = attempt == _EOD_CANCEL_IMMEDIATE_RETRY_MAX_ATTEMPTS
            await asyncio.sleep(_EOD_CANCEL_IMMEDIATE_RETRY_BACKOFF_SECONDS)

        if exhausted and final_failed > 0:
            log_event(
                logger,
                event_type="EOD_CANCEL_OPEN_ORDERS_RETRY_EXHAUSTED",
                message="EOD open-order cancel retry loop exhausted with failures remaining",
                level=logging.WARNING,
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                attempts_run=attempts_run,
                max_attempts=_EOD_CANCEL_IMMEDIATE_RETRY_MAX_ATTEMPTS,
                attempted=attempted_total,
                cancelled=cancelled_total,
                failed=final_failed,
                unsupported=unsupported_total,
            )

        log_event(
            logger,
            event_type="EOD_CANCEL_OPEN_ORDERS_RETRY_RESULT",
            message="EOD open-order cancel retry loop finished",
            level=logging.INFO if final_failed == 0 else logging.WARNING,
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            attempts_run=attempts_run,
            max_attempts=_EOD_CANCEL_IMMEDIATE_RETRY_MAX_ATTEMPTS,
            attempted=attempted_total,
            cancelled=cancelled_total,
            failed=final_failed,
            unsupported=unsupported_total,
        )
        return {
            "attempted": attempted_total,
            "cancelled": cancelled_total,
            "failed": final_failed,
            "unsupported": unsupported_total,
            "retry_attempts": max(0, attempts_run - 1),
        }

    # Normalize exchange values into a safe uppercase string.
    @staticmethod
    def _norm_exchange(raw: object) -> str:
        if raw is None:
            return ""
        try:
            return str(raw).strip().upper()
        except Exception:
            return ""

    # Check whether an exchange is excluded from EOD exits.
    def _is_excluded_exchange(self, exchange: object) -> bool:
        exch = self._norm_exchange(exchange)
        if not exch or not self._exclude_exchanges:
            return False
        # Exact match or prefix match (e.g., 'MCX' matches 'MCX', 'MCXFO')
        return any(exch == ex or exch.startswith(ex) for ex in self._exclude_exchanges)

    # Parse HH:MM time strings from settings.
    def _parse_eod_time(
        self,
        raw: str,
        *,
        field_name: str,
    ) -> Optional[time]:
        """
        Parse "HH:MM" into a time object.
        If parsing fails, log a warning and return None.
        """
        if raw in (None, ""):
            return None
        try:
            hh, mm = str(raw).split(":")
            return time(hour=int(hh), minute=int(mm))
        except Exception as exc:
            log_event(
                logger,
                event_type="EOD_EXIT_TIME_PARSE_ERROR",
                message="failed to parse EOD time value",
                level=logging.WARNING,
                field=field_name,
                raw_value=raw,
                error=repr(exc),
            )
            return None

    @staticmethod
    def _parse_iso_ts(raw: object) -> Optional[datetime]:
        if raw in (None, ""):
            return None
        try:
            text = str(raw).strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return None

    def _positions_sync_snapshot(
        self,
        broker_account_id: BrokerAccountId,
    ) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "status": None,
            "last_ok_ts": None,
            "last_ok_dt": None,
            "last_ok_age_seconds": None,
            "error_reason": None,
            "retry_after_seconds": None,
        }
        getter = getattr(self.state_store, "get_positions_status", None)
        if not callable(getter):
            return out
        try:
            raw = getter(broker_account_id)
        except Exception:
            return out
        if not isinstance(raw, dict):
            return out
        status_raw = raw.get("status")
        status = str(status_raw).strip().upper() if status_raw not in (None, "") else None
        last_ok_ts = raw.get("last_ok_ts")
        last_ok_dt = self._parse_iso_ts(last_ok_ts)
        age_s: Optional[float] = None
        if last_ok_dt is not None:
            age_s = max(
                0.0,
                (
                    self._clock.now_utc() - last_ok_dt.astimezone(timezone.utc)
                ).total_seconds(),
            )
        out.update(
            {
                "status": status,
                "last_ok_ts": last_ok_ts,
                "last_ok_dt": last_ok_dt,
                "last_ok_age_seconds": age_s,
                "error_reason": raw.get("error_reason"),
                "retry_after_seconds": raw.get("retry_after_seconds"),
            }
        )
        return out

    def _is_positions_snapshot_fresh(
        self,
        *,
        sync_snapshot: Dict[str, Any],
    ) -> tuple[bool, str]:
        if not self._require_fresh_position_sync:
            return True, "freshness_gate_disabled"
        status = sync_snapshot.get("status")
        if status != "OK":
            return False, "status_not_ok"
        age_s = sync_snapshot.get("last_ok_age_seconds")
        if age_s is None:
            return False, "last_ok_missing"
        if float(age_s) > float(self._positions_max_age_seconds):
            return False, "last_ok_stale"
        return True, "ok"

    def _is_retry_window_open(self, now_local: datetime) -> bool:
        if self._retry_cutoff_time is None:
            return True
        return now_local.time() < self._retry_cutoff_time

    def _reset_daily_submission_keys(self, today: date) -> None:
        if self._submitted_exit_date == today:
            return
        self._submitted_exit_date = today
        self._submitted_exit_keys.clear()

    @staticmethod
    def _exit_submission_key(
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        symbol: str,
        side: OrderSide,
        broker_units: int,
        token: Optional[str],
    ) -> str:
        token_text = str(token or "").strip() or "-"
        return "|".join(
            [
                str(tenant_id),
                str(broker_account_id),
                str(symbol),
                side.name,
                str(int(abs(broker_units))),
                token_text,
            ]
        )

    # Execute EOD exits across runners if the time threshold is reached.
    async def maybe_force_exit_all(
        self,
        runners: Iterable[AccountRunner],
    ) -> None:
        """
        If EOD exit is enabled and local time is past EOD_EXIT_TIME and we
        haven't exited today yet, send EXIT orders for all open positions
        across all accounts and mark the day as exited.
        """
        if not self.settings.enable_eod_exit:
            return
        if self._eod_time is None:
            return

        now = self._clock.now_local(self._tz)
        today = now.date()

        if self._exited_today == today:
            return
        if now.time() < self._eod_time:
            return

        self._reset_daily_submission_keys(today)
        retry_window_open = self._is_retry_window_open(now)

        total_orders = 0
        failed_orders = 0
        excluded_positions = 0
        invalid_qty_positions = 0
        fetched_positions = 0
        normalized_non_zero_positions = 0
        eligible_positions = 0
        cancel_attempted = 0
        cancel_success = 0
        cancel_failed = 0
        cancel_unsupported = 0
        freshness_blocked_accounts = 0
        freshness_blocked_positions = 0
        duplicate_suppressed = 0
        strategy_id = StrategyId("eod_exit")

        for runner in runners:
            if runner is None or not getattr(runner, "is_running", False):
                continue

            tenant_id = runner.tenant_id
            broker_account_id = runner.broker_account_id

            if self.eod_state_manager and self.eod_state_manager.has_exited_today(tenant_id, broker_account_id):
                continue
                
            acc_failed_start = failed_orders
            acc_cfailed_start = cancel_failed
            acc_fblocked_start = freshness_blocked_accounts

            positions: List[Position] = (
                self.state_store.get_positions(broker_account_id) or []
            )
            fetched_positions += len(positions)

            sync_snapshot = self._positions_sync_snapshot(broker_account_id)
            fresh_ok, fresh_reason = self._is_positions_snapshot_fresh(
                sync_snapshot=sync_snapshot
            )

            non_zero_positions: List[tuple[Position, int]] = []
            for pos in positions:
                signed_units = _as_int(getattr(pos, "quantity", None))
                if signed_units is None:
                    invalid_qty_positions += 1
                    continue
                if signed_units == 0:
                    continue
                non_zero_positions.append((pos, signed_units))
            normalized_non_zero_positions += len(non_zero_positions)

            if self._position_telemetry:
                log_event(
                    logger,
                    event_type="EOD_EXIT_POSITION_TELEMETRY",
                    message="EOD position snapshot",
                    level=logging.INFO,
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    fetched_count=len(positions),
                    normalized_non_zero_count=len(non_zero_positions),
                    positions_status=sync_snapshot.get("status"),
                    last_ok_ts=sync_snapshot.get("last_ok_ts"),
                    last_ok_age_seconds=sync_snapshot.get("last_ok_age_seconds"),
                    freshness_ok=fresh_ok,
                    freshness_reason=fresh_reason,
                )

            if self._require_fresh_position_sync and not fresh_ok:
                freshness_blocked_accounts += 1
                freshness_blocked_positions += len(non_zero_positions)
                log_event(
                    logger,
                    event_type="EOD_EXIT_SKIPPED_STALE_POSITIONS",
                    message="EOD exit skipped due to stale position sync state",
                    level=logging.WARNING,
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    positions_status=sync_snapshot.get("status"),
                    last_ok_ts=sync_snapshot.get("last_ok_ts"),
                    last_ok_age_seconds=sync_snapshot.get("last_ok_age_seconds"),
                    freshness_reason=fresh_reason,
                    error_reason=sync_snapshot.get("error_reason"),
                    retry_after_seconds=sync_snapshot.get("retry_after_seconds"),
                )
                continue

            for pos, signed_units in non_zero_positions:
                if signed_units > 0:
                    exit_side = OrderSide.SELL
                else:
                    exit_side = OrderSide.BUY

                exchange = getattr(pos, "exchange", None) or getattr(pos, "exch_seg", None)
                if self._is_excluded_exchange(exchange):
                    excluded_positions += 1
                    continue

                # M3: Check DegradedScopeManager exit restriction (§13.2)
                from app.core.degraded_scope_manager import degraded_scope_manager
                eod_symbol = str(getattr(pos, "symbol", "") or "")
                eod_scope_key = f"{broker_account_id}:{eod_symbol}"
                if degraded_scope_manager.is_exit_restricted(eod_scope_key):
                    logger.warning(
                        "EOD exit skipped for %s — exit restricted by DegradedScopeManager (§13.2)",
                        eod_scope_key,
                    )
                    continue

                eligible_positions += 1

                lots, broker_units, lot_size, qty_reason = _resolve_exit_lots(pos)
                if lots is None:
                    log_event(
                        logger,
                        event_type="EOD_EXIT_SKIPPED_QTY",
                        message="skipping EOD exit: unsafe units-to-lots conversion",
                        level=logging.WARNING,
                        tenant_id=tenant_id,
                        broker_account_id=broker_account_id,
                        strategy_id=strategy_id,
                        symbol=pos.symbol,
                        side=exit_side.name,
                        broker_units=broker_units,
                        lot_size=lot_size,
                        reason=qty_reason,
                    )
                    continue

                token = _position_symbol_token(pos)
                submission_key = self._exit_submission_key(
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    symbol=pos.symbol,
                    side=exit_side,
                    broker_units=broker_units,
                    token=token,
                )
                if (
                    self._retry_on_no_eligible
                    and submission_key in self._submitted_exit_keys
                ):
                    duplicate_suppressed += 1
                    continue
                order_req = OrderRequest(
                    symbol=pos.symbol,
                    quantity=lots,
                    side=exit_side,
                    order_type=OrderType.MARKET,
                    product_type=pos.product_type or ProductType.INTRADAY,
                    time_in_force=TimeInForce.DAY,
                    limit_price=None,
                    stop_price=None,
                    tag="EOD_EXIT",
                    purpose=OrderPurpose.EXIT,
                    exchange=exchange,
                    symbol_token=str(token) if token is not None else None,
                    idempotency_key=f"eod_{today.isoformat()}_{submission_key}",
                    position_ownership_bypass=True,
                )

                try:
                    hub_order_id, resp = await self.order_router.submit_order(
                        tenant_id=tenant_id,
                        broker_account_id=broker_account_id,
                        strategy_id=strategy_id,
                        order_req=order_req,
                    )
                    status_upper = str(getattr(resp, "status", "") or "").upper()
                    if status_upper not in {"REJECTED", "FAILED", "ERROR"}:
                        total_orders += 1
                        if self._retry_on_no_eligible:
                            self._submitted_exit_keys.add(submission_key)
                    else:
                        failed_orders += 1
                except Exception as exc:
                    failed_orders += 1
                    log_event(
                        logger,
                        event_type="EOD_EXIT_FAILED",
                        message="EOD exit order failed",
                        level=logging.ERROR,
                        tenant_id=tenant_id,
                        broker_account_id=broker_account_id,
                        strategy_id=strategy_id,
                        symbol=pos.symbol,
                        qty=lots,
                        side=exit_side.name,
                        broker_units=broker_units,
                        lot_size=lot_size,
                        error=repr(exc),
                    )
                    continue

                log_event(
                    logger,
                    event_type="EOD_EXIT_ORDER",
                    message="submitted EOD exit order",
                    level=logging.INFO,
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    strategy_id=strategy_id,
                    symbol=pos.symbol,
                    qty=lots,
                    side=exit_side.name,
                    broker_units=broker_units,
                    lot_size=lot_size,
                    hub_order_id=hub_order_id,
                    status=getattr(resp, "status", None),
                    reason=getattr(resp, "message", None),
                )

            if self._cancel_open_orders_enabled:
                cancel_fn = getattr(runner, "cancel_open_intraday_orders", None)
                if callable(cancel_fn):
                    if self._cancel_retry_loop_enabled:
                        cancel_summary = await self._cancel_open_orders_with_immediate_retries(
                            cancel_fn=cancel_fn,
                            tenant_id=tenant_id,
                            broker_account_id=broker_account_id,
                        )
                        cancel_attempted += int(cancel_summary.get("attempted", 0) or 0)
                        cancel_success += int(cancel_summary.get("cancelled", 0) or 0)
                        cancel_failed += int(cancel_summary.get("failed", 0) or 0)
                        cancel_unsupported += int(cancel_summary.get("unsupported", 0) or 0)
                    else:
                        try:
                            cancel_summary = await cancel_fn(
                                exclude_exchanges=self._exclude_exchanges,
                                reason_tag="EOD_CANCEL_OPEN_ORDERS",
                            )
                            cancel_attempted += int(cancel_summary.get("attempted", 0) or 0)
                            cancel_success += int(cancel_summary.get("cancelled", 0) or 0)
                            cancel_failed += int(cancel_summary.get("failed", 0) or 0)
                            cancel_unsupported += int(cancel_summary.get("unsupported", 0) or 0)
                        except Exception as exc:
                            cancel_failed += 1
                            log_event(
                                logger,
                                event_type="EOD_CANCEL_OPEN_ORDERS_FAILED",
                                message="EOD open-order cancel pass failed",
                                level=logging.WARNING,
                                tenant_id=tenant_id,
                                broker_account_id=broker_account_id,
                                error=repr(exc),
                            )
                else:
                    cancel_unsupported += 1
                    log_event(
                        logger,
                        event_type="EOD_CANCEL_OPEN_ORDERS_UNSUPPORTED",
                        message="Runner does not expose open-order cancel capability",
                        level=logging.WARNING,
                        tenant_id=tenant_id,
                        broker_account_id=broker_account_id,
                    )

            acc_failed = failed_orders - acc_failed_start
            acc_cfailed = cancel_failed - acc_cfailed_start
            acc_fblocked = freshness_blocked_accounts - acc_fblocked_start
            if acc_failed == 0 and acc_cfailed == 0 and acc_fblocked == 0:
                if self.eod_state_manager:
                    self.eod_state_manager.record_eod_exit(tenant_id, broker_account_id)

        if self._position_telemetry:
            log_event(
                logger,
                event_type="EOD_EXIT_POSITION_TELEMETRY_SUMMARY",
                message="EOD position telemetry summary",
                level=logging.INFO,
                exit_date=str(today),
                fetched_positions=fetched_positions,
                normalized_non_zero_positions=normalized_non_zero_positions,
                eligible_positions=eligible_positions,
                excluded_positions=excluded_positions,
                invalid_qty_positions=invalid_qty_positions,
                freshness_blocked_accounts=freshness_blocked_accounts,
                freshness_blocked_positions=freshness_blocked_positions,
                duplicate_suppressed=duplicate_suppressed,
                cancel_attempted=cancel_attempted,
                cancel_success=cancel_success,
                cancel_failed=cancel_failed,
                cancel_unsupported=cancel_unsupported,
                retry_on_no_eligible=self._retry_on_no_eligible,
                require_fresh_position_sync=self._require_fresh_position_sync,
                retry_window_open=retry_window_open,
                retry_cutoff=self._retry_cutoff_time.strftime("%H:%M")
                if self._retry_cutoff_time
                else None,
            )

        mark_exited = True
        retry_pending_reason: Optional[str] = None
        if retry_window_open and freshness_blocked_accounts > 0:
            mark_exited = False
            retry_pending_reason = "stale_position_sync"
        elif retry_window_open and self._retry_on_no_eligible and failed_orders > 0:
            mark_exited = False
            retry_pending_reason = "order_failures"
        elif retry_window_open and self._retry_on_no_eligible and cancel_failed > 0:
            mark_exited = False
            retry_pending_reason = "order_cancel_failures"
        elif total_orders > 0:
            mark_exited = True
        elif cancel_success > 0:
            mark_exited = True
        elif excluded_positions > 0:
            mark_exited = True
        elif self._retry_on_no_eligible and retry_window_open:
            mark_exited = False
            retry_pending_reason = "no_eligible_positions"

        if self._cancel_open_orders_enabled and (cancel_attempted > 0 or cancel_failed > 0):
            log_event(
                logger,
                event_type="EOD_CANCEL_OPEN_ORDERS_SUMMARY",
                message="EOD open-order cancellation summary",
                level=logging.INFO if cancel_failed == 0 else logging.WARNING,
                exit_date=str(today),
                attempted=cancel_attempted,
                cancelled=cancel_success,
                failed=cancel_failed,
                unsupported=cancel_unsupported,
                excluded_exchanges=sorted(self._exclude_exchanges) if self._exclude_exchanges else None,
            )

        if mark_exited:
            self._exited_today = today

        if total_orders > 0:
            log_event(
                logger,
                event_type="EOD_EXIT_TRIGGERED",
                message="EOD exit executed for eligible positions",
                level=logging.INFO,
                exit_date=str(today),
                total_orders=total_orders,
                excluded_positions=excluded_positions,
                excluded_exchanges=sorted(self._exclude_exchanges) if self._exclude_exchanges else None,
            )
        else:
            if retry_pending_reason is not None:
                log_event(
                    logger,
                    event_type="EOD_EXIT_RETRY_PENDING",
                    message="EOD exit pending retry window",
                    level=logging.INFO,
                    exit_date=str(today),
                    reason=retry_pending_reason,
                    retry_cutoff=self._retry_cutoff_time.strftime("%H:%M")
                    if self._retry_cutoff_time
                    else None,
                    freshness_blocked_accounts=freshness_blocked_accounts,
                    excluded_positions=excluded_positions,
                    failed_orders=failed_orders,
                    duplicate_suppressed=duplicate_suppressed,
                )
            elif excluded_positions > 0:
                log_event(
                    logger,
                    event_type="EOD_EXIT_SKIPPED_EXCLUDED_EXCHANGES",
                    message="EOD exit skipped: all open positions are on excluded exchanges",
                    level=logging.WARNING,
                    exit_date=str(today),
                    total_orders=total_orders,
                    excluded_positions=excluded_positions,
                    failed_orders=failed_orders,
                    freshness_blocked_accounts=freshness_blocked_accounts,
                    duplicate_suppressed=duplicate_suppressed,
                    excluded_exchanges=sorted(self._exclude_exchanges) if self._exclude_exchanges else None,
                )
            else:
                log_event(
                    logger,
                    event_type="EOD_EXIT_NO_ELIGIBLE_POSITIONS",
                    message="EOD exit: no open positions eligible for exit",
                    level=logging.INFO,
                    exit_date=str(today),
                    total_orders=total_orders,
                    excluded_positions=excluded_positions,
                    excluded_exchanges=sorted(self._exclude_exchanges) if self._exclude_exchanges else None,
                )

__all__ = [
    "PositionExitPlan",
    "build_position_exit_plan",
    "ProfitSweepEngine",
    "EODExitEngine",
]
