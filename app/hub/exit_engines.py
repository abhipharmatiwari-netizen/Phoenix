"""
Hub-level profit sweep and end-of-day exit orchestrators.
Coordinates exits across runners based on PnL targets and schedules.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
import logging
import threading
from zoneinfo import ZoneInfo
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

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
from app.pnl.position_trailing_lock import (
    PositionTrailingLockDecision,
    PositionTrailingLockInflightBackend,
    PositionTrailingLockInflightMarker,
    PositionTrailingLockManager,
    _NoopPositionTrailingLockInflightBackend,
)
from app.orders.router import OrderRouter
from app.orders.position_ownership import (
    ContractKey,
    UNKNOWN_OWNER,
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
    _exclude_exchanges: set[str] = field(default_factory=set)
    
    # Legacy fields (kept for old-style profit sweep compatibility)
    _swept_today: Dict[Tuple[TenantId, BrokerAccountId], date] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        self._clock = self.clock or SystemClock()
        raw_excl = getattr(self.settings, "eod_exit_excluded_exchanges", "") or ""
        self._exclude_exchanges = {
            s.strip().upper()
            for s in str(raw_excl or "").split(",")
            if s and s.strip()
        }

    @staticmethod
    def _norm_exchange(raw: object) -> str:
        if raw is None:
            return ""
        try:
            return str(raw).strip().upper()
        except Exception:
            return ""

    def _is_excluded_exchange(self, exchange: object) -> bool:
        exch = self._norm_exchange(exchange)
        if not exch or not self._exclude_exchanges:
            return False
        return any(exch == ex or exch.startswith(ex) for ex in self._exclude_exchanges)

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
        
        _base_daily_target = getattr(self.settings, "profit_daily_target", None)
        _target_mode = str(getattr(self.settings, "profit_simple_target_mode", "absolute") or "absolute").lower()

        if _target_mode == "absolute":
            if _base_daily_target is None or _base_daily_target <= 0:
                log_event(
                    logger,
                    event_type="PROFIT_SWEEP_SIMPLE_INVALID",
                    message="SIMPLE strategy requires valid profit_daily_target when mode=absolute",
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

            # Compute effective daily_target for this runner
            if _target_mode == "premium_pct":
                _premium_pct = float(getattr(self.settings, "profit_simple_target_premium_pct", 0.5))
                _open_premium = 0.0
                try:
                    _account_snaps = self.pnl_engine._state_store.list_account_snapshots(
                        tenant_id=tenant_id, broker_account_id=broker_account_id
                    ) if hasattr(self.pnl_engine, "_state_store") else []
                    _open_premium = float(sum(
                        getattr(s, "control_open_premium", 0.0) or 0.0
                        for s in _account_snaps
                    ))
                except Exception:
                    _open_premium = 0.0
                if _open_premium <= 0.0:
                    log_event(
                        logger,
                        event_type="PROFIT_SWEEP_SIMPLE_PREMIUM_SKIP",
                        message="SIMPLE premium_pct: no open premium data; skipping tick",
                        level=logging.DEBUG,
                        tenant_id=tenant_id,
                        broker_account_id=broker_account_id,
                    )
                    continue
                daily_target = _open_premium * _premium_pct
            else:
                daily_target = _base_daily_target
                if is_paper and getattr(self.settings, "profit_daily_target_paper", None):
                    daily_target = getattr(self.settings, "profit_daily_target_paper")

            _control_pnl = self.pnl_engine.get_control_total_pnl(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
            )
            total_pnl = (
                _control_pnl
                if _control_pnl is not None
                else self.pnl_engine.get_current_total_pnl(
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                )
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
            str(runner.runtime_mode).upper() == "PAPER"
            
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
            
            # Get current PnL — prefer control PnL (excludes entry premium distortion)
            _control_pnl = self.pnl_engine.get_control_total_pnl(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
            )
            total_pnl = (
                _control_pnl
                if _control_pnl is not None
                else self.pnl_engine.get_current_total_pnl(
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                )
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


from app.hub.eod_state import EODStateManager  # noqa: E402 — late import to break circular dependency

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

# --- Per-position trailing profit lock engine ---------------------------
# Independent of HubProfitSweepEngine. Iterates each runner's open positions,
# tracks per-(account, symbol) peak unrealized P&L via PositionTrailingLockManager,
# and emits a single-position exit when current unrealized P&L falls below
# peak * (1 - giveback_pct).
@dataclass
class PositionTrailingLockEngine:
    settings: Settings
    state_store: StateStore
    order_router: OrderRouter
    manager: PositionTrailingLockManager
    clock: IClock = field(default_factory=SystemClock)
    # Issue #219: provider for the durable hub kill-switch manager. A callable
    # is required (rather than a direct reference) because ``AppRuntime``
    # REPLACES ``HubRuntime.kill_switch_manager`` at startup after loading
    # state from Postgres (see ``app/runtime/app_runtime.py`` step 3 — kill-
    # switch durable state restore). A direct reference captured at engine
    # construction time would point at the original, empty pre-load instance
    # while the bridge / interceptor / admin routes all consult the
    # post-load replacement — this is the bug Codex flagged on PR #231.
    # The provider closure resolves the ATTRIBUTE on the runtime each time,
    # so swap-in is observed.
    #
    # When the provider is None (test path), the engine skips the kill-switch
    # gate entirely and behaves exactly as before #219. Production wiring
    # passes ``lambda: self.kill_switch_manager`` from ``HubRuntime`` so the
    # post-load instance is always observed.
    kill_switch_manager_provider: Optional[Callable[[], Any]] = None
    # Per-(tenant, account) timestamp of the last suppression-skip log so we
    # do not flood logs while a kill switch is tripped for a long window.
    _suppression_log_state: Dict[Tuple[str, str], datetime] = field(
        default_factory=dict, init=False, repr=False
    )
    # Issue #251: durable backend for inflight markers. When wired (LIVE),
    # markers persist to Postgres so a restart does NOT drop the duplicate-
    # fill guard between submit_order and broker terminal confirmation.
    # Default Noop = pre-#251 in-memory-only behaviour (acceptable for
    # PAPER/SHADOW because no real broker order is at stake).
    inflight_backend: PositionTrailingLockInflightBackend = field(
        default_factory=_NoopPositionTrailingLockInflightBackend,
    )
    # Issue #225: per-(tenant, account, symbol) inflight markers for the
    # most recently submitted trailing-lock exit. Set after submit;
    # checked at the start of the next evaluation cycle to block
    # duplicate trailing-lock submissions while the broker has not yet
    # confirmed the prior order's terminal state. The 2026-05-08 incident
    # produced duplicate 3-lot fills (broker_order_ids 842740 + 842946
    # ~60s apart) because the time-based ``exit_cooldown_seconds`` and
    # the position-ownership ``exit_already_in_flight`` lock both
    # released between fires. This per-position marker is the
    # idempotency safety net.
    #
    # Value tuple: (broker_order_id_or_None, monotonic_submitted_at_float,
    # wallclock_submitted_at_datetime). The wall-clock instant is what is
    # persisted to Postgres (issue #251); the monotonic timestamp is what
    # the age comparison uses inside the watchdog (monotonic is immune to
    # wall-clock jumps).
    _inflight_markers: Dict[
        Tuple[str, str, str],
        Tuple[Optional[str], float, datetime],
    ] = field(default_factory=dict, init=False, repr=False)
    # Issue #251: True once the engine has SUCCESSFULLY hydrated the in-memory
    # marker dict from the durable backend. Lazy because the engine is
    # constructed at module import time but the backend may not be reachable
    # until the runtime has wired up Postgres.
    #
    # PR #261 round-1 review P1: previously this flag was set BEFORE the
    # ``load_all()`` call, so a transient Postgres outage on first eval would
    # permanently disable the duplicate-fill guard for the lifetime of the
    # process. We now flip it to True ONLY on confirmed success and retry on
    # the next eval cycle if the first attempt raised.
    _inflight_hydrated: bool = field(
        default=False, init=False, repr=False,
    )
    # PR #261 round-1 review P1: count of failed hydrate attempts since
    # startup, surfaced in the structured WARNING on each retry so an
    # operator can see how many cycles the duplicate-fill guard has been
    # degraded.
    _inflight_hydrate_failed_attempts: int = field(
        default=0, init=False, repr=False,
    )
    # PR #261 round-2 review P1 (runtime.py:539): when the LIVE startup
    # path detects that the durable Postgres backend could not be
    # constructed, ``HubRuntime`` sets this flag so the engine refuses to
    # submit any trailing-lock exit until a process restart re-attempts
    # the backend init. Without this flag, the engine would happily write
    # ``fail_closed=True`` markers to the in-memory noop backend (which
    # cannot raise) and the durable duplicate-fill guard would be missing
    # in production.
    inflight_disabled: bool = field(default=False, repr=False)
    # PR #261 round-2 review P2 (exit_engines.py:3047): when the marker
    # is armed PRE-submit we do not know the broker_order_id yet. To
    # make the unknown-id terminal-evidence fallback robust we capture
    # the expected exit ``side`` and ``quantity`` (lots) at arming time
    # so the disappeared-symbol fallback can match the right ledger row
    # rather than any unrelated entry order for the same symbol.
    _inflight_exit_specs: Dict[
        Tuple[str, str, str],
        Tuple[Optional[str], Optional[int]],
    ] = field(default_factory=dict, init=False, repr=False)
    _submission_guard_lock: threading.RLock = field(
        default_factory=threading.RLock, init=False, repr=False
    )

    @staticmethod
    def _is_live_mode() -> bool:
        """Return True iff the process is running in LIVE TRADE_MODE.

        Centralised so every fail-closed gate sees the same answer in the
        same evaluation cycle, and so tests can monkeypatch the env var.
        """
        import os
        return (
            str(os.getenv("TRADE_MODE", "PAPER") or "PAPER").strip().upper()
            == "LIVE"
        )

    def _resolve_position_owner_strategy_id(
        self,
        *,
        tenant_id: Any,
        broker_account_id: Any,
        pos: Any,
    ) -> Optional[str]:
        for attr_name in (
            "owner_strategy_id",
            "ownership_strategy_id",
            "position_owner_strategy_id",
            "strategy_id",
        ):
            raw = _position_value(pos, attr_name)
            token = str(raw or "").strip()
            if token and token != UNKNOWN_OWNER and not token.startswith("system::"):
                return token

        store = getattr(self.order_router, "_position_ownership_store", None)
        if store is None:
            return None
        try:
            contract_key, _reason = derive_contract_key_from_position(pos)
            if contract_key is None:
                return None
            owner: Optional[str] = None
            get_owner = getattr(store, "get_owner", None)
            if callable(get_owner):
                owner = get_owner(
                    tenant_id=TenantId(str(tenant_id)),
                    broker_account_id=BrokerAccountId(str(broker_account_id)),
                    contract_key=contract_key,
                )
            if owner in (None, UNKNOWN_OWNER):
                get_record = getattr(store, "get_ownership_record", None)
                if callable(get_record):
                    record = get_record(
                        tenant_id=TenantId(str(tenant_id)),
                        broker_account_id=BrokerAccountId(str(broker_account_id)),
                        contract_key=contract_key,
                    )
                    owner = getattr(record, "owner_strategy_id", None)
            owner_text = str(owner or "").strip()
            if owner_text and owner_text != UNKNOWN_OWNER and not owner_text.startswith("system::"):
                return owner_text
        except Exception as exc:
            if self._is_live_mode():
                log_event(
                    logger,
                    event_type="POSITION_TRAILING_LOCK_OWNER_LOOKUP_FAILED",
                    message=(
                        "Trailing-lock owner lookup failed in LIVE; exit will "
                        "still route as system actor but cannot pre-bind owner."
                    ),
                    level=logging.WARNING,
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    symbol=str(_position_value(pos, "symbol", "") or ""),
                    error=repr(exc),
                )
        return None

    def _enabled(self) -> bool:
        return bool(getattr(self.settings, "position_trailing_lock_enabled", False))

    def _tick_enabled(self) -> bool:
        return bool(
            getattr(self.settings, "position_trailing_lock_tick_enabled", False)
        )

    def _resolve_kill_switch_manager(self) -> Any:
        """Resolve the current durable kill-switch manager via the provider.

        Returns None when no provider is wired (tests that do not exercise
        the gate) or when the provider raises / returns None. Callers must
        treat None as "kill switch state unknown — fall through" (fail OPEN).
        """
        if self.kill_switch_manager_provider is None:
            return None
        try:
            return self.kill_switch_manager_provider()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "PositionTrailingLockEngine: kill_switch_manager_provider "
                "raised (non-fatal): %s",
                exc,
            )
            return None

    def _is_kill_switch_tripped_for_scope(
        self, *, tenant_id: Any, broker_account_id: Any
    ) -> bool:
        """Return True if the durable kill switch is tripped for this scope.

        Checks GLOBAL → TENANT → ACCOUNT hierarchy via
        ``KillSwitchManager.is_tripped_for_scope``.

        Failure mode (Codex P2 round 2 review):

        - **LIVE**: a lookup failure means we cannot prove the kill switch
          is INACTIVE; we MUST fail CLOSED (return True, skip exits). The
          ``GlobalKillSwitchInterceptor`` is NOT a backstop here because
          it explicitly bypasses exit orders by design. If we returned
          False on lookup failure, trailing-lock could submit exits
          against the very stale state this gate is supposed to suppress.
        - **non-LIVE** (PAPER/SHADOW/dev): keep the historical fail-OPEN
          behaviour so unrelated infrastructure issues do not block dev
          loops. The risk is bounded — no real broker order is placed.
        - **No manager wired** (provider=None): same fail-OPEN. This is
          the test-fixture case where the engine is exercised without
          hub-runtime wiring.
        """
        ksm = self._resolve_kill_switch_manager()
        if ksm is None:
            return False
        try:
            return bool(
                ksm.is_tripped_for_scope(
                    tenant_id=str(tenant_id) if tenant_id else None,
                    account_id=str(broker_account_id) if broker_account_id else None,
                )
            )
        except Exception as exc:  # pragma: no cover - defensive, logged below
            import os
            trade_mode = str(
                os.getenv("TRADE_MODE", "PAPER") or "PAPER"
            ).strip().upper()
            if trade_mode == "LIVE":
                logger.error(
                    "PositionTrailingLockEngine: kill_switch lookup failed "
                    "in LIVE — failing CLOSED (skipping exit submission to "
                    "avoid running against stale state): %s",
                    exc,
                )
                return True
            logger.warning(
                "PositionTrailingLockEngine: kill_switch lookup failed "
                "(non-fatal in non-LIVE): %s",
                exc,
            )
            return False

    def _maybe_log_suppression_skip(
        self, *, tenant_id: Any, broker_account_id: Any
    ) -> None:
        """Emit a rate-limited (1 per 60s per scope) suppression-skip event."""
        key = (str(tenant_id), str(broker_account_id))
        now = self.clock.now_utc()
        last = self._suppression_log_state.get(key)
        if last is not None and (now - last).total_seconds() < 60.0:
            return
        self._suppression_log_state[key] = now
        log_event(
            logger,
            event_type="POSITION_TRAILING_LOCK_SKIPPED_KILL_SWITCH",
            message=(
                "Trailing-lock evaluation skipped: durable kill switch tripped "
                "for scope (issue #219)."
            ),
            level=logging.WARNING,
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
        )

    def _inflight_max_seconds(self) -> float:
        """Issue #225: max age of an inflight trailing-lock marker before
        it is auto-cleared with an ERROR event. Default 60s — matches the
        order_ownership exit-lock watchdog floor minus a safety margin so
        the trailing-lock marker is the FIRST gate to see an in-flight
        exit (preventing duplicate submissions) and the ownership lock
        watchdog is the second-line backstop."""
        # Configurable via Settings; fall back to 60s if absent.
        return float(
            getattr(
                self.settings,
                "position_trailing_lock_inflight_max_seconds",
                60.0,
            )
        )

    def _is_inflight_blocked(
        self, *, tenant_id: Any, broker_account_id: Any, symbol: str,
    ) -> bool:
        """Return True if a recent trailing-lock submission for this
        (tenant, account, symbol) is still considered in-flight (younger
        than ``_inflight_max_seconds``).

        Auto-clears stale markers and emits a structured ERROR event so a
        marker that never resolves (e.g. the broker order vanished from
        polling) does not permanently block trailing-lock for the symbol.
        """
        import time as _t
        key = (str(tenant_id), str(broker_account_id), str(symbol))
        marker = self._inflight_markers.get(key)
        if marker is None:
            return False
        broker_order_id, submitted_at_mono, _wallclock = marker
        age = _t.monotonic() - float(submitted_at_mono)
        max_age = self._inflight_max_seconds()
        if age < max_age:
            return True
        # Stale marker — auto-clear and surface as ERROR. Operator can
        # investigate via broker_order_id; trailing-lock will resume
        # normal evaluation on the next cycle. Issue #251: also drop the
        # persisted row so a subsequent restart does NOT rehydrate the
        # stale marker.
        #
        # PR #261 round-3 review P2 (exit_engines.py:2227): in LIVE,
        # request ``raise_on_failure=True`` so a Postgres delete/connect
        # failure is propagated (and surfaced as an operator-visible
        # ERROR event) rather than swallowed here. Without this, the
        # in-memory marker is cleared but the durable row persists and a
        # subsequent restart rehydrates the stale marker — blocking the
        # very position the timeout was meant to unblock until ANOTHER
        # timeout fires. Mirror the operator-visible ERROR pattern from
        # ``_clear_inflight_marker`` (round-2 P2). In non-LIVE, keep the
        # historical log-only behaviour because no real broker order is
        # at stake.
        self._inflight_markers.pop(key, None)
        self._inflight_exit_specs.pop(key, None)
        live = self._is_live_mode()
        try:
            self.inflight_backend.delete_marker(
                str(tenant_id),
                str(broker_account_id),
                str(symbol),
                raise_on_failure=live,
            )
        except Exception as _exc:
            log_event(
                logger,
                event_type=(
                    "POSITION_TRAILING_LOCK_INFLIGHT_DELETE_FAILED"
                ),
                message=(
                    "Durable inflight marker delete FAILED while "
                    "ageing out a stale marker — the in-memory marker "
                    "is already cleared but the durable row remains "
                    "and a process restart will rehydrate the stale "
                    "marker. Operator action: investigate Postgres "
                    "health and DELETE the row manually if needed "
                    "(PR #261 round-3 review P2 — exit_engines.py:2227)."
                ),
                level=logging.ERROR,
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                symbol=symbol,
                broker_order_id=broker_order_id,
                error=repr(_exc),
            )
        log_event(
            logger,
            event_type="POSITION_TRAILING_LOCK_INFLIGHT_TIMEOUT",
            message=(
                "Trailing-lock inflight marker exceeded max age — auto-"
                "clearing. The broker order may not have reached a "
                "terminal state via the snapshot polling path. "
                "Investigate broker_order_id and reconcile manually if "
                "duplicate fills are observed."
            ),
            level=logging.ERROR,
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            symbol=symbol,
            broker_order_id=broker_order_id,
            age_seconds=round(age, 2),
            max_age_seconds=max_age,
        )
        return False

    def _maybe_log_inflight_skip(
        self, *, tenant_id: Any, broker_account_id: Any, symbol: str,
    ) -> None:
        """Rate-limited (1 per 60s per scope) inflight-skip event.

        PR #236 round-3 review P3: previous version logged every skip,
        producing one event per evaluate cycle while the marker held —
        flooding logs while the marker held its full max-age window.
        Mirror the pattern used by ``_maybe_log_suppression_skip``.
        """
        key = (str(tenant_id), str(broker_account_id), str(symbol))
        if not hasattr(self, "_inflight_skip_log_state"):
            self._inflight_skip_log_state: Dict[
                Tuple[str, str, str], datetime
            ] = {}
        now = self.clock.now_utc()
        last = self._inflight_skip_log_state.get(key)
        if last is not None and (now - last).total_seconds() < 60.0:
            return
        self._inflight_skip_log_state[key] = now
        marker = self._inflight_markers.get(key)
        broker_order_id = marker[0] if marker else None
        log_event(
            logger,
            event_type="POSITION_TRAILING_LOCK_SKIPPED_INFLIGHT",
            message=(
                "Trailing-lock submission skipped: a recent exit for this "
                "position is still in-flight (issue #225)."
            ),
            level=logging.WARNING,
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            symbol=symbol,
            broker_order_id=broker_order_id,
        )

    def _set_inflight_marker(
        self,
        *,
        tenant_id: Any,
        broker_account_id: Any,
        symbol: str,
        broker_order_id: Optional[str],
        persist: bool = True,
        fail_closed: bool = False,
    ) -> None:
        """Arm the inflight marker for (tenant, account, symbol).

        Issue #251: also writes to the durable backend so the marker
        survives a process restart between submit_order and broker
        terminal confirmation. ``persist=False`` is used by the startup
        hydration path to seed the in-memory dict from already-persisted
        rows without re-writing them.

        PR #261 round-1 review P1: ``fail_closed=True`` is used by the
        PRE-submit arming in ``_emit_exit`` in LIVE so a persistence
        failure aborts the broker submission. Without fail-closed, a
        persist failure followed by a restart between broker-submit and
        the (never-completed) post-submit confirmation would lose the
        durable duplicate-fill guard. Default ``fail_closed=False`` is
        used by the post-submit refresh paths (synchronous fill,
        post-submit raise) where the broker order is already placed and
        we want best-effort persistence with a log-only fallback.
        """
        import time as _t
        key = (str(tenant_id), str(broker_account_id), str(symbol))
        now_mono = _t.monotonic()
        now_utc = datetime.now(timezone.utc)
        self._inflight_markers[key] = (broker_order_id, now_mono, now_utc)
        # PR #261 round-3 review P2 (exit_engines.py:2509): pull the
        # exit spec captured by ``_emit_exit`` immediately before this
        # call so we can round-trip it to the durable backend. Without
        # this, a restart hydrates the marker but ``_inflight_exit_specs``
        # is empty and the unknown-broker-order-id terminal-evidence
        # fallback can never recognise a matching FILLED row.
        _spec = self._inflight_exit_specs.get(key)
        _spec_side, _spec_units = (
            (_spec[0], _spec[1]) if _spec else (None, None)
        )
        if persist:
            try:
                # PR #261 round-2 review P1 (root cause): when the caller
                # has opted into fail-closed (LIVE pre-submit), request
                # ``raise_on_failure=True`` so a Postgres outage actually
                # bubbles up here. The historical default
                # (``raise_on_failure=False``) keeps the legacy log-only
                # behaviour for post-submit refresh paths where the
                # broker order has ALREADY been placed and the in-memory
                # marker remains the authoritative duplicate-fill guard.
                self.inflight_backend.save_marker(
                    PositionTrailingLockInflightMarker(
                        tenant_id=str(tenant_id),
                        broker_account_id=str(broker_account_id),
                        symbol=str(symbol),
                        broker_order_id=broker_order_id,
                        submitted_at=now_utc,
                        exit_side=_spec_side,
                        exit_broker_units=_spec_units,
                    ),
                    raise_on_failure=bool(fail_closed),
                )
            except Exception as exc:
                if fail_closed:
                    # PR #261 round-1 review P1: surface a clear ERROR
                    # event and re-raise so the caller (``_emit_exit``)
                    # ABORTS the broker submission. Placing the broker
                    # order without a durable guard reopens the
                    # restart-window duplicate-fill gap that issue #251
                    # set out to close.
                    log_event(
                        logger,
                        event_type=(
                            "POSITION_TRAILING_LOCK_INFLIGHT_PERSIST_FAILED"
                        ),
                        message=(
                            "Pre-submit inflight marker persistence FAILED "
                            "in LIVE — ABORTING broker submission to avoid "
                            "running without the durable duplicate-fill "
                            "guard (PR #261 round-1 review P1, issue "
                            "#251)."
                        ),
                        level=logging.ERROR,
                        tenant_id=tenant_id,
                        broker_account_id=broker_account_id,
                        symbol=symbol,
                        broker_order_id=broker_order_id,
                        error=repr(exc),
                    )
                    # Roll back the in-memory marker too so a subsequent
                    # eval cycle (after the operator resolves Postgres)
                    # can re-arm cleanly without a phantom in-memory
                    # guard surviving the abort.
                    self._inflight_markers.pop(key, None)
                    raise
                logger.warning(
                    "trailing-lock inflight marker persist failed (non-fatal; "
                    "in-memory guard remains): %s",
                    exc,
                )

    def _reserve_inflight_marker(
        self,
        *,
        tenant_id: Any,
        broker_account_id: Any,
        symbol: str,
        broker_order_id: Optional[str],
        fail_closed: bool = False,
    ) -> bool:
        with self._submission_guard_lock:
            if self._is_inflight_blocked(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                symbol=symbol,
            ):
                self._maybe_log_inflight_skip(
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    symbol=symbol,
                )
                return False
            self._set_inflight_marker(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                symbol=symbol,
                broker_order_id=broker_order_id,
                fail_closed=fail_closed,
            )
            return True

    def _clear_inflight_marker(
        self, *, tenant_id: Any, broker_account_id: Any, symbol: str,
    ) -> None:
        key = (str(tenant_id), str(broker_account_id), str(symbol))
        self._inflight_markers.pop(key, None)
        self._inflight_exit_specs.pop(key, None)
        # PR #261 round-2 review P2 (position_trailing_lock.py:568): in
        # LIVE, request ``raise_on_failure=True`` so a swallowed delete
        # cannot leave a stale durable row that a subsequent restart
        # would rehydrate. We surface the failure as a structured ERROR
        # event so the operator can clean up manually. In non-LIVE the
        # historical log-only path is preserved (no broker order at
        # stake).
        live = self._is_live_mode()
        try:
            self.inflight_backend.delete_marker(
                str(tenant_id),
                str(broker_account_id),
                str(symbol),
                raise_on_failure=live,
            )
        except Exception as exc:
            log_event(
                logger,
                event_type=(
                    "POSITION_TRAILING_LOCK_INFLIGHT_DELETE_FAILED"
                ),
                message=(
                    "Durable inflight marker delete FAILED in LIVE — "
                    "the in-memory guard is already cleared but the "
                    "durable row remains and a process restart will "
                    "rehydrate the stale marker. Operator action: "
                    "investigate Postgres health and DELETE the row "
                    "manually if needed (PR #261 round-2 review P2)."
                ),
                level=logging.ERROR,
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                symbol=symbol,
                error=repr(exc),
            )

    def _hydrate_inflight_markers_from_backend(self) -> None:
        """Issue #251: lazy hydration of the in-memory marker dict from
        the durable backend on first ``evaluate_runners`` invocation.

        Hydration is lazy (not in ``__post_init__``) because the engine
        dataclass may be constructed before the Postgres pool is reachable
        in some startup paths. The result is a one-shot import on SUCCESS:
        once hydrated, subsequent submit/clear operations are the
        authoritative source.

        PR #261 round-1 review P1: distinguish "load succeeded → 0 rows"
        from "load failed → unknown". The previous version set
        ``_inflight_hydrated = True`` BEFORE calling ``load_all()`` and
        early-returned on any exception, so a transient Postgres outage
        on the first eval after restart permanently dropped the
        duplicate-fill guard for the lifetime of the process. We now:
          * Set ``_inflight_hydrated = True`` ONLY after a confirmed
            successful load (success may legitimately return zero rows
            — that is distinct from a load failure).
          * Increment ``_inflight_hydrate_failed_attempts`` and emit a
            structured WARNING naming the retry count on each failure
            so an operator can see how long the guard has been degraded.
          * Retry on the NEXT eval cycle until success.

        The reload of ``submitted_at`` becomes both the in-memory monotonic
        timestamp AND the wall-clock instant. We compute the monotonic
        equivalent by subtracting (now_utc - persisted_submitted_at) from
        the current monotonic clock — so the age comparison
        (``_is_inflight_blocked``) sees the SAME effective marker age as
        the durable record. This is the critical correctness property:
        after restart, a 50-second-old marker must STILL be 50 seconds old
        and not reset to zero.
        """
        if self._inflight_hydrated:
            return
        # PR #261 round-2 review P1 (root cause): force the backend to
        # PROPAGATE Postgres failures rather than silently returning an
        # empty list. Without ``raise_on_failure=True``, the backend's
        # exception handler converts a connect/query failure into ``[]``,
        # the ``except Exception`` arm below never fires, and we
        # incorrectly flip ``_inflight_hydrated = True`` — permanently
        # disabling retries for the lifetime of the process. With this
        # flag the failure now bubbles up here so the retry/degraded
        # bookkeeping below can actually run.
        try:
            persisted = list(
                self.inflight_backend.load_all(raise_on_failure=True)
            )
        except Exception as exc:
            self._inflight_hydrate_failed_attempts += 1
            log_event(
                logger,
                event_type="POSITION_TRAILING_LOCK_INFLIGHT_HYDRATE_FAILED",
                message=(
                    "trailing-lock inflight marker hydrate attempt FAILED — "
                    "duplicate-fill guard is degraded to in-memory only "
                    "for this eval cycle; will retry on next cycle "
                    "(PR #261 round-1/round-2 review P1, issue #251)."
                ),
                level=logging.WARNING,
                failed_attempts=self._inflight_hydrate_failed_attempts,
                error=repr(exc),
            )
            return
        # Successful load (even if zero rows). Mark hydrated so we do not
        # re-query the backend on every eval cycle.
        self._inflight_hydrated = True
        if not persisted:
            return
        import time as _t
        now_mono = _t.monotonic()
        now_utc = datetime.now(timezone.utc)
        hydrated_count = 0
        for marker in persisted:
            try:
                submitted_at = marker.submitted_at
                if submitted_at.tzinfo is None:
                    submitted_at = submitted_at.replace(tzinfo=timezone.utc)
                age_seconds = max(0.0, (now_utc - submitted_at).total_seconds())
                effective_mono = now_mono - age_seconds
                key = (
                    str(marker.tenant_id),
                    str(marker.broker_account_id),
                    str(marker.symbol),
                )
                self._inflight_markers[key] = (
                    marker.broker_order_id,
                    effective_mono,
                    submitted_at,
                )
                # PR #261 round-3 review P2 (exit_engines.py:2509):
                # repopulate ``_inflight_exit_specs`` from the persisted
                # exit spec so the unknown-broker-order-id fallback can
                # match the right ledger row after a restart. Legacy
                # rows have NULL spec columns; skip those so the engine
                # falls back to the broker_order_id strict-match path.
                _persisted_side = (
                    str(marker.exit_side).strip().upper()
                    if marker.exit_side
                    else None
                )
                _persisted_units: Optional[int]
                try:
                    _persisted_units = (
                        int(marker.exit_broker_units)
                        if marker.exit_broker_units is not None
                        else None
                    )
                except (TypeError, ValueError):
                    _persisted_units = None
                if _persisted_units is not None and _persisted_units <= 0:
                    _persisted_units = None
                if _persisted_side or _persisted_units is not None:
                    self._inflight_exit_specs[key] = (
                        _persisted_side,
                        _persisted_units,
                    )
                hydrated_count += 1
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "trailing-lock inflight marker hydrate row failed "
                    "(non-fatal, skipping row): %s",
                    exc,
                )
        if hydrated_count:
            log_event(
                logger,
                event_type="POSITION_TRAILING_LOCK_INFLIGHT_HYDRATED",
                message=(
                    f"Hydrated {hydrated_count} persisted inflight marker(s) "
                    "from the durable backend at startup — restart duplicate-"
                    "fill guard intact (issue #251)."
                ),
                level=logging.INFO,
                hydrated_count=hydrated_count,
            )

    @staticmethod
    def _compute_unrealized_pnl(pos: Any, symbol: str) -> Optional[float]:
        """Compute live unrealized PnL using the LTP cache + position avg_price.

        Returns None if LTP is unavailable (skip evaluation rather than treat as 0).
        """
        try:
            qty = int(_position_value(pos, "quantity") or 0)
        except (TypeError, ValueError):
            return None
        if qty == 0:
            return None
        avg_price = _position_value(pos, "avg_price")
        if avg_price is None:
            avg_price = _position_value(pos, "average_price")
        if avg_price is None:
            return None
        try:
            avg_f = float(avg_price)
        except (TypeError, ValueError):
            return None
        token = _position_symbol_token(pos)
        ltp = dashboard_bus.get_last_price_for_instrument(symbol=symbol, token=token)
        if ltp is None and symbol:
            ltp = dashboard_bus.get_last_price(symbol)
        if ltp is None:
            return None
        return float((float(ltp) - avg_f) * qty)

    @staticmethod
    def _compute_unrealized_pnl_from_price(
        pos: Any, price: float,
    ) -> Optional[float]:
        try:
            qty = int(_position_value(pos, "quantity") or 0)
        except (TypeError, ValueError):
            return None
        if qty == 0:
            return None
        avg_price = _position_value(pos, "avg_price")
        if avg_price is None:
            avg_price = _position_value(pos, "average_price")
        if avg_price is None:
            return None
        try:
            return float((float(price) - float(avg_price)) * qty)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _position_matches_tick(
        pos: Any,
        *,
        tick_label: Optional[str],
        tick_symbol: Optional[str],
        tick_token: Optional[str],
    ) -> bool:
        pos_symbol = str(_position_value(pos, "symbol", "") or "").strip()
        pos_symbol_norm = pos_symbol.upper()
        pos_token = str(_position_symbol_token(pos) or "").strip()
        label_norm = str(tick_label or "").strip().upper()
        symbol_norm = str(tick_symbol or "").strip().upper()
        token_norm = str(tick_token or "").strip()

        if token_norm and pos_token and token_norm == pos_token:
            return True
        if symbol_norm and pos_symbol_norm and symbol_norm == pos_symbol_norm:
            return True
        if label_norm and pos_symbol_norm and label_norm == pos_symbol_norm:
            return True

        resolved_label = dashboard_bus.resolve_label(
            symbol=pos_symbol,
            token=pos_token or None,
        )
        if resolved_label and label_norm:
            return str(resolved_label).strip().upper() == label_norm
        return False

    def _maybe_log_ownership_exit_lock_skip(
        self,
        *,
        tenant_id: Any,
        broker_account_id: Any,
        symbol: str,
        owner: Optional[str],
        released_at: Optional[datetime],
    ) -> None:
        key = (str(tenant_id), str(broker_account_id), str(symbol))
        if not hasattr(self, "_ownership_exit_lock_skip_log_state"):
            self._ownership_exit_lock_skip_log_state: Dict[
                Tuple[str, str, str], datetime
            ] = {}
        now = self.clock.now_utc()
        last = self._ownership_exit_lock_skip_log_state.get(key)
        if last is not None and (now - last).total_seconds() < 60.0:
            return
        self._ownership_exit_lock_skip_log_state[key] = now
        log_event(
            logger,
            event_type="POSITION_TRAILING_LOCK_SKIPPED_OWNERSHIP_EXIT_INFLIGHT",
            message=(
                "Trailing-lock submission skipped: position ownership "
                "already has an active exit lock for this contract."
            ),
            level=logging.WARNING,
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            symbol=symbol,
            owner=owner,
            released_at=released_at.isoformat() if released_at else None,
        )

    def _position_ownership_exit_lock_active(
        self,
        *,
        tenant_id: Any,
        broker_account_id: Any,
        pos: Any,
        symbol: str,
    ) -> bool:
        store = getattr(self.order_router, "_position_ownership_store", None)
        if store is None:
            return False
        try:
            contract_key, _reason = derive_contract_key_from_position(pos)
            get_record = getattr(store, "get_ownership_record", None)
            if not callable(get_record):
                return False
            record = get_record(
                tenant_id=TenantId(str(tenant_id)),
                broker_account_id=BrokerAccountId(str(broker_account_id)),
                contract_key=contract_key,
            )
            released_at = getattr(record, "released_at", None)
            if released_at is None:
                return False
            if released_at.tzinfo is None:
                released_at = released_at.replace(tzinfo=timezone.utc)
            watchdog = float(getattr(store, "_exit_lock_max_seconds", 0.0) or 0.0)
            if watchdog <= 0.0:
                return False
            age = (self.clock.now_utc() - released_at).total_seconds()
            if age >= watchdog:
                return False
            self._maybe_log_ownership_exit_lock_skip(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                symbol=symbol,
                owner=getattr(record, "owner_strategy_id", None),
                released_at=released_at,
            )
            return True
        except Exception as exc:
            if self._is_live_mode():
                log_event(
                    logger,
                    event_type="POSITION_TRAILING_LOCK_OWNERSHIP_LOOKUP_FAILED",
                    message=(
                        "Trailing-lock ownership exit-lock lookup failed in "
                        "LIVE; failing closed for this position."
                    ),
                    level=logging.ERROR,
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    symbol=symbol,
                    error=repr(exc),
                )
                return True
            logger.warning(
                "PositionTrailingLockEngine: ownership exit-lock lookup "
                "failed for %s (non-fatal in non-LIVE): %s",
                symbol,
                exc,
            )
            return False

    async def evaluate_tick(
        self,
        runners: Iterable[AccountRunner],
        *,
        tick_label: Optional[str],
        price: float,
        tick_symbol: Optional[str] = None,
        tick_token: Optional[str] = None,
    ) -> None:
        """Evaluate trailing profit protection for positions matching one tick.

        The watchdog path remains the authoritative cleanup sweep. This path is
        deliberately narrow: it only updates/submits for positions whose broker
        symbol/token maps to the incoming tick.
        """
        if not self._enabled() or not self._tick_enabled():
            return
        try:
            price_f = float(price)
        except (TypeError, ValueError):
            return
        if price_f <= 0.0:
            return

        if self.inflight_disabled:
            log_event(
                logger,
                event_type="POSITION_TRAILING_LOCK_TICK_DISABLED_NO_BACKEND",
                message=(
                    "Tick-driven trailing-lock submission skipped: durable "
                    "inflight backend was not constructed at startup (LIVE)."
                ),
                level=logging.ERROR,
            )
            return

        if not self._inflight_hydrated:
            self._hydrate_inflight_markers_from_backend()
        if self._is_live_mode() and not self._inflight_hydrated:
            log_event(
                logger,
                event_type="POSITION_TRAILING_LOCK_TICK_SKIPPED_HYDRATE_PENDING",
                message=(
                    "Tick-driven trailing-lock submission skipped: durable "
                    "inflight marker hydrate has not succeeded yet in LIVE."
                ),
                level=logging.ERROR,
                failed_attempts=self._inflight_hydrate_failed_attempts,
            )
            return

        giveback_pct = float(
            getattr(self.settings, "position_trailing_lock_giveback_pct", 0.10)
        )
        floor_inr = float(
            getattr(self.settings, "position_trailing_lock_floor_inr", 500.0)
        )
        cooldown = float(
            getattr(self.settings, "position_trailing_lock_exit_cooldown_seconds", 30.0)
        )
        for runner in runners:
            try:
                await self._evaluate_runner_tick(
                    runner,
                    tick_label=tick_label,
                    tick_symbol=tick_symbol,
                    tick_token=tick_token,
                    price=price_f,
                    giveback_pct=giveback_pct,
                    floor_inr=floor_inr,
                    cooldown=cooldown,
                )
            except Exception as exc:
                log_event(
                    logger,
                    event_type="POSITION_TRAILING_LOCK_TICK_RUNNER_ERROR",
                    message="PositionTrailingLockEngine tick evaluation failed",
                    level=logging.ERROR,
                    tenant_id=getattr(runner, "tenant_id", None),
                    broker_account_id=getattr(runner, "broker_account_id", None),
                    tick_label=tick_label,
                    tick_symbol=tick_symbol,
                    tick_token=tick_token,
                    error=repr(exc),
                )

    async def _evaluate_runner_tick(
        self,
        runner: AccountRunner,
        *,
        tick_label: Optional[str],
        tick_symbol: Optional[str],
        tick_token: Optional[str],
        price: float,
        giveback_pct: float,
        floor_inr: float,
        cooldown: float,
    ) -> None:
        tenant_id = getattr(runner, "tenant_id", None)
        broker_account_id = getattr(runner, "broker_account_id", None)
        if tenant_id is None or broker_account_id is None:
            return

        kill_switch_tripped = self._is_kill_switch_tripped_for_scope(
            tenant_id=tenant_id, broker_account_id=broker_account_id,
        )
        if kill_switch_tripped:
            self._maybe_log_suppression_skip(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
            )
            return

        positions = self.state_store.get_positions(broker_account_id) or []
        for pos in positions:
            symbol = str(_position_value(pos, "symbol", "") or "").strip()
            if not symbol:
                continue
            if not self._position_matches_tick(
                pos,
                tick_label=tick_label,
                tick_symbol=tick_symbol,
                tick_token=tick_token,
            ):
                continue
            try:
                qty = int(_position_value(pos, "quantity") or 0)
            except (TypeError, ValueError):
                qty = 0
            if qty == 0:
                continue
            if self._is_inflight_blocked(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                symbol=symbol,
            ):
                self._maybe_log_inflight_skip(
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    symbol=symbol,
                )
                continue
            if self._position_ownership_exit_lock_active(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                pos=pos,
                symbol=symbol,
            ):
                continue
            unrealized = self._compute_unrealized_pnl_from_price(pos, price)
            if unrealized is None:
                continue
            decision = self.manager.evaluate(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                symbol=symbol,
                current_unrealized_pnl=unrealized,
                floor_inr=floor_inr,
                giveback_pct=giveback_pct,
                exit_cooldown_seconds=cooldown,
            )
            if decision.exit_required:
                await self._emit_exit(runner, pos, decision)

    async def evaluate_runners(self, runners: Iterable[AccountRunner]) -> None:
        if not self._enabled():
            return
        # PR #261 round-2 review P1 (runtime.py:539): if HubRuntime
        # detected at startup that the durable Postgres backend could
        # not be initialised in LIVE, the engine is permanently disabled
        # for the lifetime of the process — the in-memory noop backend
        # is NOT a safe substitute because ``fail_closed=True`` writes
        # would silently succeed against it and the engine would happily
        # submit broker orders with no durable restart guard. A process
        # restart is required after the operator has resolved the
        # Postgres outage.
        #
        # PR #261 round-3 review P2 (exit_engines.py:2584): previously
        # this branch ``return``ed early, which ALSO suppressed the
        # qty==0 closed-position cleanup paths inside ``_evaluate_runner``
        # (manager state reset + inflight marker clear). If a position
        # closes manually or via broker fill while the backend is
        # unavailable, the persisted trailing-lock peak/armed state
        # survives until restart and is then applied to a same-symbol
        # reopen — emitting an immediate stale trailing-lock exit on
        # the new position's first evaluate cycle. The fix: emit the
        # warning once-per-cycle and continue running ``_evaluate_runner``
        # so the cleanup paths still fire. ``_evaluate_runner`` consults
        # ``self.inflight_disabled`` to refuse the broker-submission
        # branch (preserve fail-closed) while still resetting state.
        if self.inflight_disabled:
            log_event(
                logger,
                event_type="POSITION_TRAILING_LOCK_DISABLED_NO_BACKEND",
                message=(
                    "Trailing-lock SUBMISSIONS disabled this cycle: "
                    "durable inflight backend was not constructed at "
                    "startup (LIVE). Closed-position cleanup paths "
                    "(manager state reset + marker clear) still run so "
                    "stale peak/armed state is not preserved into a "
                    "same-symbol reopen. Restart the process after "
                    "Postgres is reachable to re-enable trailing-lock "
                    "exits (PR #261 round-2 review P1 — runtime.py:539; "
                    "round-3 review P2 — exit_engines.py:2584)."
                ),
                level=logging.ERROR,
            )
            # Fall through to the runner loop. ``_evaluate_runner`` reads
            # ``self.inflight_disabled`` and skips submission while still
            # honouring qty==0 cleanup. Skip hydration / live-mode gates
            # below because the backend is known-bad — there is nothing
            # to hydrate.
            for runner in runners:
                try:
                    await self._evaluate_runner(
                        runner,
                        giveback_pct=float(
                            getattr(
                                self.settings,
                                "position_trailing_lock_giveback_pct",
                                0.10,
                            )
                        ),
                        floor_inr=float(
                            getattr(
                                self.settings,
                                "position_trailing_lock_floor_inr",
                                500.0,
                            )
                        ),
                        cooldown=float(
                            getattr(
                                self.settings,
                                "position_trailing_lock_exit_cooldown_seconds",
                                30.0,
                            )
                        ),
                    )
                except Exception as exc:
                    log_event(
                        logger,
                        event_type=(
                            "POSITION_TRAILING_LOCK_RUNNER_ERROR"
                        ),
                        message=(
                            "PositionTrailingLockEngine runner evaluation "
                            "failed while inflight_disabled (cleanup-only "
                            "mode)"
                        ),
                        level=logging.ERROR,
                        tenant_id=getattr(runner, "tenant_id", None),
                        broker_account_id=getattr(
                            runner, "broker_account_id", None,
                        ),
                        error=repr(exc),
                    )
            return
        # Issue #251: hydrate persisted inflight markers from the durable
        # backend on the first evaluate call. Restart-survival depends on
        # this — otherwise a process restart drops the duplicate-fill
        # guard immediately. Lazy because the backend may not be reachable
        # at engine-construction time in some startup orderings.
        if not self._inflight_hydrated:
            self._hydrate_inflight_markers_from_backend()
        # PR #261 round-2 review P1 (exit_engines.py:2405): in LIVE, if
        # we still do not have a successful hydrate (Postgres still
        # unreachable), refuse to submit trailing-lock exits this cycle.
        # The empty in-memory marker set is NOT proof "no exits are
        # in-flight" — it is "we could not read the durable record" —
        # and proceeding with normal evaluation would re-submit an exit
        # for a position whose persisted marker we simply could not
        # load. Fail closed.
        #
        # PR #261 round-5 review P2 (exit_engines.py:2760): previously
        # this branch ``return``ed, which ALSO suppressed the qty==0
        # closed-position cleanup paths in ``_evaluate_runner``. If
        # Postgres is unreachable for a few watchdog cycles while a
        # symbol closes and is then reopened before hydration succeeds,
        # the old peak/armed state is never reset and the reopened
        # position can be evaluated against stale trailing-lock state.
        # Mirror the ``inflight_disabled`` cleanup-only mode (round-3 P2,
        # exit_engines.py:2584): emit the warning, then fall through to
        # the runner loop with ``submissions_blocked=True`` so the
        # qty==0 cleanup runs unconditionally while the new-exit
        # submission path remains fail-closed.
        hydrate_pending_block = (
            self._is_live_mode() and not self._inflight_hydrated
        )
        if hydrate_pending_block:
            log_event(
                logger,
                event_type=(
                    "POSITION_TRAILING_LOCK_SKIPPED_HYDRATE_PENDING"
                ),
                message=(
                    "Trailing-lock SUBMISSIONS skipped this cycle: durable "
                    "inflight marker hydrate has not succeeded yet in LIVE. "
                    "Submitting exits with an unknown durable-marker state "
                    "could place a duplicate exit for a position whose "
                    "persisted marker simply could not be loaded. "
                    "Closed-position cleanup (manager state reset + marker "
                    "clear) still runs so a position closed during the "
                    "hydrate-pending window cannot preserve stale peak/"
                    "armed state into a same-symbol reopen "
                    "(PR #261 round-2 review P1 — exit_engines.py:2405; "
                    "round-5 review P2 — exit_engines.py:2760)."
                ),
                level=logging.ERROR,
                failed_attempts=self._inflight_hydrate_failed_attempts,
            )
        giveback_pct = float(getattr(self.settings, "position_trailing_lock_giveback_pct", 0.10))
        floor_inr = float(getattr(self.settings, "position_trailing_lock_floor_inr", 500.0))
        cooldown = float(
            getattr(self.settings, "position_trailing_lock_exit_cooldown_seconds", 30.0)
        )
        for runner in runners:
            try:
                await self._evaluate_runner(
                    runner,
                    giveback_pct=giveback_pct,
                    floor_inr=floor_inr,
                    cooldown=cooldown,
                    submissions_blocked=hydrate_pending_block,
                )
            except Exception as exc:
                log_event(
                    logger,
                    event_type="POSITION_TRAILING_LOCK_RUNNER_ERROR",
                    message="PositionTrailingLockEngine runner evaluation failed",
                    level=logging.ERROR,
                    tenant_id=getattr(runner, "tenant_id", None),
                    broker_account_id=getattr(runner, "broker_account_id", None),
                    error=repr(exc),
                )

    async def _evaluate_runner(
        self,
        runner: AccountRunner,
        *,
        giveback_pct: float,
        floor_inr: float,
        cooldown: float,
        submissions_blocked: bool = False,
    ) -> None:
        # PR #261 round-5 review P2 (exit_engines.py:2760): when the
        # caller has determined that NEW trailing-lock submissions must
        # be fail-closed for this cycle (durable inflight hydrate has
        # not succeeded yet in LIVE), we still want to run the qty==0
        # cleanup paths below so a closed position cannot preserve
        # stale peak/armed state into a same-symbol reopen.
        # ``submissions_blocked=True`` short-circuits the non-zero
        # submission branch the same way ``self.inflight_disabled``
        # does.
        tenant_id = getattr(runner, "tenant_id", None)
        broker_account_id = getattr(runner, "broker_account_id", None)
        if tenant_id is None or broker_account_id is None:
            return
        # Issue #219: do not SUBMIT exits while the durable kill switch is
        # tripped for this scope. The legacy RiskManager auto-trip propagates
        # to the durable manager (issue #218); when that path is active,
        # internal position state is being held stale by BROKER_SYNC
        # suppression and trailing-lock evaluation against it has historically
        # produced runaway exit submissions and duplicate broker fills (the
        # 2026-05-08 NATURALGAS22MAY26265CE incident). Skipping the exit
        # submission is the correct safe behaviour; operator-initiated manual
        # flatten is the documented path while the kill switch is tripped.
        kill_switch_tripped = self._is_kill_switch_tripped_for_scope(
            tenant_id=tenant_id, broker_account_id=broker_account_id,
        )
        if kill_switch_tripped:
            self._maybe_log_suppression_skip(
                tenant_id=tenant_id, broker_account_id=broker_account_id,
            )
        positions = self.state_store.get_positions(broker_account_id) or []
        live_symbols: set[str] = set()
        # Issue #225 (PR #236 review P2): track every symbol present in
        # the snapshot — including qty==0 — so that markers for symbols
        # that disappeared entirely from the snapshot can be swept at
        # the end of the loop. Without this sweep, a fill that removes
        # the position from the next snapshot leaves the marker armed
        # until the timeout window expires, blocking re-opens of the
        # same symbol unnecessarily.
        seen_symbols: set[str] = set()
        for pos in positions:
            symbol = str(_position_value(pos, "symbol", "") or "").strip()
            if not symbol:
                continue
            seen_symbols.add(symbol)
            try:
                qty = int(_position_value(pos, "quantity") or 0)
            except (TypeError, ValueError):
                qty = 0
            if qty == 0:
                # Position closed since last cycle — clear any persisted state.
                # NB: this cleanup MUST run even while the kill switch is
                # tripped (issue #220 review feedback). If a position closed
                # during the kill-switch window, the persisted peak/armed
                # state for that symbol must be reset; otherwise a re-opened
                # position after rearm reuses the stale peak and immediately
                # emits a trailing-lock exit even though its own peak never
                # armed.
                self.manager.reset_position(tenant_id, broker_account_id, symbol)
                # Issue #225: a closed position means our prior in-flight
                # exit (if any) reached terminal state — clear the marker
                # so future opens on the same symbol are not blocked.
                self._clear_inflight_marker(
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    symbol=symbol,
                )
                continue
            # Skip the exit-submission path entirely when the kill switch
            # is tripped, but keep iterating positions so the qty==0
            # cleanup branch above continues to fire for every closed
            # position in this cycle.
            if kill_switch_tripped:
                continue
            # PR #261 round-3 review P2 (exit_engines.py:2584): when the
            # durable inflight backend was not constructed at startup,
            # refuse to submit any trailing-lock exit (preserve the
            # fail-closed contract from runtime.py:539). The qty==0
            # cleanup path above STILL fires so that a position closed
            # during the backend-outage window does not preserve stale
            # peak/armed state into a same-symbol reopen.
            if self.inflight_disabled:
                continue
            # PR #261 round-5 review P2 (exit_engines.py:2760): same
            # contract for the hydrate-pending window — caller passed
            # ``submissions_blocked=True`` because the durable inflight
            # hydrate has not succeeded yet in LIVE. Skip the submission
            # branch but keep the qty==0 cleanup above firing.
            if submissions_blocked:
                continue
            # Issue #225: per-position inflight-marker check. If a recent
            # trailing-lock submission for this (tenant, account, symbol)
            # is still considered in-flight, do NOT submit another. This
            # is the primary defence against the 2026-05-08 duplicate-fill
            # scenario (broker_order_ids 842740 + 842946 ~60s apart).
            if self._is_inflight_blocked(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                symbol=symbol,
            ):
                self._maybe_log_inflight_skip(
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    symbol=symbol,
                )
                continue
            if self._position_ownership_exit_lock_active(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                pos=pos,
                symbol=symbol,
            ):
                continue
            live_symbols.add(symbol)
            unrealized = self._compute_unrealized_pnl(pos, symbol)
            if unrealized is None:
                # No LTP yet — skip; will reattempt next cycle.
                continue
            decision = self.manager.evaluate(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                symbol=symbol,
                current_unrealized_pnl=unrealized,
                floor_inr=floor_inr,
                giveback_pct=giveback_pct,
                exit_cooldown_seconds=cooldown,
            )
            if decision.exit_required:
                await self._emit_exit(runner, pos, decision)

        # Issue #225 (PR #236 review P2): sweep markers for this
        # (tenant, account) whose symbol disappeared entirely from the
        # snapshot. State stores can omit qty==0 records (the
        # AccountRunner._sync_positions path stores broker positions
        # as-is; ``OrderLifecycleService`` only appends projected
        # positions when qty != 0). When a fill closes a position, the
        # symbol may simply vanish from the next snapshot — without
        # this sweep the marker would persist until the timeout window
        # and incorrectly block a quick re-open. The grace period
        # (5s) prevents clearing a marker that was just armed in this
        # same cycle.
        # PR #236 round-4/round-5 review P2: pass the broker positions
        # sync FINGERPRINT (last_ok_ts when status==OK and recent) so
        # the disappeared-symbol sweep only counts misses against a
        # snapshot from a DISTINCT successful sync. None means the
        # snapshot is stale or sync is not currently OK — sweep is
        # suppressed entirely. A non-None value that matches the
        # previous fingerprint also suppresses (same snapshot already
        # observed in a prior cycle).
        positions_sync_fingerprint = self._get_positions_sync_fingerprint(
            broker_account_id=broker_account_id,
        )
        self._sweep_disappeared_symbol_markers(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            seen_symbols=seen_symbols,
            grace_seconds=5.0,
            positions_sync_fingerprint=positions_sync_fingerprint,
        )

    def _get_positions_sync_fingerprint(
        self, *, broker_account_id: Any,
    ) -> Optional[str]:
        """Return the current ``last_ok_ts`` for the broker positions
        snapshot, or None if positions sync has never succeeded or
        the current ``status`` is not OK.

        PR #236 round-5 review P2: the disappeared-symbol sweep must
        require BOTH a current OK status AND a fresh ``last_ok_ts``.
        ``StateStore.update_positions_status`` keeps ``last_ok_ts``
        when a later sync reports ERROR/BLOCKED, so timestamp-only
        freshness can be misleading after a failed sync. The returned
        timestamp is also used as a per-evaluation fingerprint — the
        miss-counter only increments when the fingerprint CHANGES
        from the previous evaluation (distinct successful sync), so
        two watchdog ticks observing the SAME OK-but-empty snapshot
        cannot reach the two-miss threshold without a fresh broker
        sync in between.
        """
        try:
            get_status = getattr(
                self.state_store, "get_positions_status", None,
            )
            if not callable(get_status):
                return None
            status = get_status(broker_account_id) or {}
            current_status = str(status.get("status") or "").strip().upper()
            if current_status != "OK":
                return None
            last_ok_iso = status.get("last_ok_ts")
            if not last_ok_iso:
                return None
            from datetime import datetime, timezone
            try:
                last_ok = datetime.fromisoformat(
                    str(last_ok_iso).replace("Z", "+00:00")
                )
            except ValueError:
                return None
            now = self.clock.now_utc()
            if last_ok.tzinfo is None:
                last_ok = last_ok.replace(tzinfo=timezone.utc)
            age = (now - last_ok).total_seconds()
            poll_interval = float(
                getattr(
                    self.settings,
                    "hub_subscription_poll_interval",
                    60.0,
                ) or 60.0
            )
            max_fresh_age = max(poll_interval * 2.0, 30.0)
            if age > max_fresh_age:
                return None
            return str(last_ok_iso)
        except Exception:  # pragma: no cover - defensive
            return None

    def _sweep_disappeared_symbol_markers(
        self,
        *,
        tenant_id: Any,
        broker_account_id: Any,
        seen_symbols: set,
        grace_seconds: float,
        positions_sync_fingerprint: Optional[str] = None,
    ) -> None:
        """Issue #225 (PR #236 review P2): clear inflight markers for
        symbols that disappeared from the snapshot entirely. Skips
        markers younger than ``grace_seconds`` to avoid racing the
        marker just armed earlier in this evaluate cycle.

        PR #236 round-4 review P1: require at least
        ``_min_consecutive_missing_for_sweep`` consecutive missing
        observations before clearing. A single OK-but-empty/incomplete
        broker poll (transient broker glitch) can otherwise be
        misinterpreted as proof the prior exit reached terminal state.

        PR #236 round-5 review P2: ``positions_sync_fingerprint`` is
        the ``last_ok_ts`` of the broker positions snapshot when the
        current status is OK (None otherwise). The miss-counter only
        increments when the fingerprint is non-None AND DIFFERENT from
        the previously-observed fingerprint for the same key — a
        repeated evaluation of the SAME snapshot does not count, so
        the two-miss threshold can only be reached after two DISTINCT
        successful syncs have observed the symbol absent.
        """
        import time as _t
        now_mono = _t.monotonic()
        tenant_key = str(tenant_id)
        account_key = str(broker_account_id)
        if not hasattr(self, "_disappeared_symbol_miss_counts"):
            self._disappeared_symbol_miss_counts: Dict[
                Tuple[str, str, str], int
            ] = {}
        if not hasattr(self, "_disappeared_symbol_last_fingerprint"):
            self._disappeared_symbol_last_fingerprint: Dict[
                Tuple[str, str, str], str
            ] = {}
        min_consecutive_misses = 2
        # Snapshot keys to avoid mutating the dict while iterating.
        keys_to_check = [
            k for k in self._inflight_markers
            if k[0] == tenant_key and k[1] == account_key
        ]
        # Reset the miss-counter (and fingerprint memory) for any
        # symbol present in this snapshot — once we observe it, we
        # restart the counter so a later transient miss does not
        # accumulate against an old streak.
        for symbol in seen_symbols:
            sym_key = (tenant_key, account_key, str(symbol))
            self._disappeared_symbol_miss_counts.pop(sym_key, None)
            self._disappeared_symbol_last_fingerprint.pop(sym_key, None)
        # PR #236 round-5 review P2: if the underlying positions
        # snapshot is stale (no fingerprint) OR the current status is
        # not OK, do NOT count this evaluation against the miss
        # threshold.
        if positions_sync_fingerprint is None:
            return
        for key in keys_to_check:
            symbol = key[2]
            if symbol in seen_symbols:
                continue
            marker = self._inflight_markers.get(key)
            if marker is None:
                continue
            _, submitted_at_mono, _wallclock = marker
            if (now_mono - float(submitted_at_mono)) < grace_seconds:
                # Just-armed in this cycle (or sub-grace) — do not race.
                continue
            # PR #236 round-5 review P2: only count this evaluation as
            # a miss if the positions-sync fingerprint is DISTINCT
            # from the previously-observed fingerprint for this key.
            # Repeated evaluations of the SAME snapshot do not count.
            previous_fingerprint = self._disappeared_symbol_last_fingerprint.get(
                key
            )
            if previous_fingerprint == positions_sync_fingerprint:
                continue
            self._disappeared_symbol_last_fingerprint[key] = (
                positions_sync_fingerprint
            )
            # Round-4 P1: count consecutive missing snapshots for this
            # symbol; only clear once we have crossed the threshold.
            current_misses = self._disappeared_symbol_miss_counts.get(
                key, 0
            ) + 1
            self._disappeared_symbol_miss_counts[key] = current_misses
            if current_misses < min_consecutive_misses:
                # Single missing snapshot is ambiguous (could be a
                # transient broker glitch). Wait for the next cycle.
                continue
            # Issue #252: disappearance from the snapshot is NOT proof of
            # terminal state. An OK-but-empty/truncated broker poll can
            # hit this path while the broker order is still live. Require
            # EXPLICIT broker-order terminal evidence — a snapshot of the
            # broker order ledger that shows the marker's broker_order_id
            # in a canonical terminal state — before clearing. If we do
            # not have such evidence, hold the marker until the normal
            # ``_is_inflight_blocked`` timeout sweeps it. The timeout
            # default (120s) is intentionally larger than the watchdog
            # cadence so the bound is bounded.
            broker_order_id = marker[0]
            _, marker_submitted_at_mono, marker_submitted_at_wall = marker
            expected_side, expected_quantity = self._inflight_exit_specs.get(
                key, (None, None),
            )
            terminal_state = self._marker_has_terminal_broker_evidence(
                broker_account_id=broker_account_id,
                symbol=symbol,
                broker_order_id=broker_order_id,
                marker_submitted_at=marker_submitted_at_wall,
                expected_side=expected_side,
                expected_quantity=expected_quantity,
            )
            if not terminal_state:
                # PR #261 round-1 review P2: the comment above promises
                # "hold until the normal timeout sweeps it", but
                # ``_is_inflight_blocked`` is ONLY called while iterating
                # currently-present non-zero positions. For a marker
                # whose symbol filled-then-disappeared (so the
                # corresponding position will never appear in the
                # snapshot again) and whose terminal ledger row is
                # absent, no other code path will ever sweep the
                # marker — it would emit
                # ``POSITION_TRAILING_LOCK_INFLIGHT_HELD_NO_TERMINAL``
                # every watchdog cycle forever. Honour the comment: if
                # the marker is older than the configured
                # ``inflight_max_seconds``, age it out here.
                age_seconds = float(now_mono) - float(submitted_at_mono)
                max_age = self._inflight_max_seconds()
                if age_seconds >= max_age:
                    self._inflight_markers.pop(key, None)
                    self._inflight_exit_specs.pop(key, None)
                    live = self._is_live_mode()
                    try:
                        self.inflight_backend.delete_marker(
                            tenant_key,
                            account_key,
                            str(symbol),
                            raise_on_failure=live,
                        )
                    except Exception as _exc:
                        log_event(
                            logger,
                            event_type=(
                                "POSITION_TRAILING_LOCK_INFLIGHT_DELETE_FAILED"
                            ),
                            message=(
                                "Durable inflight marker delete FAILED while "
                                "ageing out a disappeared-symbol marker — "
                                "the in-memory marker is cleared but the "
                                "durable row remains. Operator should "
                                "DELETE the row manually if a restart "
                                "rehydrates a stale marker (PR #261 "
                                "round-2 review P2)."
                            ),
                            level=logging.ERROR,
                            tenant_id=tenant_id,
                            broker_account_id=broker_account_id,
                            symbol=symbol,
                            error=repr(_exc),
                        )
                    self._disappeared_symbol_miss_counts.pop(key, None)
                    self._disappeared_symbol_last_fingerprint.pop(key, None)
                    # PR #261 round-2 review P2 (exit_engines.py:2834):
                    # also RESET ``PositionTrailingLockManager`` state.
                    # The terminal-evidence branch below already does
                    # this — the timeout branch was the missing mirror.
                    # Without this reset, a same-symbol re-open after
                    # the timeout would reuse the stale peak/armed
                    # state and could emit another trailing-lock exit
                    # immediately on the new position's first evaluate
                    # cycle (issue #250).
                    try:
                        self.manager.reset_position(
                            tenant_id, broker_account_id, symbol,
                        )
                    except Exception as exc:  # pragma: no cover - defensive
                        logger.warning(
                            "trailing-lock state reset failed after "
                            "disappeared-symbol timeout for %s "
                            "(non-fatal): %s",
                            symbol, exc,
                        )
                    log_event(
                        logger,
                        event_type=(
                            "POSITION_TRAILING_LOCK_INFLIGHT_TIMEOUT"
                        ),
                        message=(
                            "Disappeared-symbol inflight marker exceeded "
                            "max age without terminal broker-order "
                            "evidence — auto-clearing AND resetting "
                            "persisted trailing-lock manager state to "
                            "prevent a same-symbol re-open from reusing "
                            "a stale peak. Investigate broker_order_id "
                            "and reconcile manually if duplicate fills "
                            "are observed (PR #261 round-1 review P2 "
                            "+ round-2 review P2 — exit_engines.py:2834)."
                        ),
                        level=logging.ERROR,
                        tenant_id=tenant_id,
                        broker_account_id=broker_account_id,
                        symbol=symbol,
                        broker_order_id=broker_order_id,
                        age_seconds=round(age_seconds, 2),
                        max_age_seconds=max_age,
                        consecutive_missing_snapshots=current_misses,
                    )
                    continue
                # Still within the timeout window — log and hold.
                log_event(
                    logger,
                    event_type=(
                        "POSITION_TRAILING_LOCK_INFLIGHT_HELD_NO_TERMINAL"
                    ),
                    message=(
                        "Symbol disappeared from snapshot but no terminal "
                        "broker-order evidence yet — holding inflight "
                        "marker until normal timeout (issue #252)."
                    ),
                    level=logging.INFO,
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    symbol=symbol,
                    broker_order_id=broker_order_id,
                    consecutive_missing_snapshots=current_misses,
                    age_seconds=round(age_seconds, 2),
                    max_age_seconds=max_age,
                )
                continue
            # PR #261 round-3 review P2 (exit_engines.py:3072): when the
            # terminal-evidence gate has cleared, propagate Postgres
            # delete failures in LIVE so a swallowed delete cannot leave
            # a durable row behind. The in-memory marker has already been
            # popped above, so a swallowed durable delete would survive
            # a restart and rehydrate the stale marker — blocking the
            # legitimate same-symbol reopen we just verified is safe to
            # allow. Mirror the operator-visible ERROR pattern used by
            # ``_clear_inflight_marker`` and the round-3 stale-marker
            # timeout path. In non-LIVE, keep the historical log-only
            # behaviour because no real broker order is at stake.
            self._inflight_markers.pop(key, None)
            self._inflight_exit_specs.pop(key, None)
            live_terminal = self._is_live_mode()
            try:
                self.inflight_backend.delete_marker(
                    tenant_key,
                    account_key,
                    str(symbol),
                    raise_on_failure=live_terminal,
                )
            except Exception as _exc:
                log_event(
                    logger,
                    event_type=(
                        "POSITION_TRAILING_LOCK_INFLIGHT_DELETE_FAILED"
                    ),
                    message=(
                        "Durable inflight marker delete FAILED in the "
                        "terminal-evidence clear path — the in-memory "
                        "marker is already cleared but the durable row "
                        "remains and a process restart will rehydrate "
                        "the stale marker. Operator action: investigate "
                        "Postgres health and DELETE the row manually if "
                        "needed (PR #261 round-3 review P2 — "
                        "exit_engines.py:3072)."
                    ),
                    level=logging.ERROR,
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    symbol=symbol,
                    error=repr(_exc),
                )
            self._disappeared_symbol_miss_counts.pop(key, None)
            self._disappeared_symbol_last_fingerprint.pop(key, None)
            # Issue #225 (PR #236 round-2 review P1) + issue #250: also
            # reset the persisted ``PositionTrailingLockManager`` state
            # for this symbol. The qty==0 path resets it; the disappeared-
            # symbol path was previously omitting this. Without the
            # reset, a quick same-symbol re-open is evaluated against the
            # stale peak/armed state and can immediately emit another
            # trailing-lock exit even though the new position never armed.
            #
            # PR #261 round-5 review P2 (exit_engines.py:3274): only
            # reset peaks on FILLED. CANCELLED / REJECTED / EXPIRED /
            # FAILED are terminal evidence FOR THE EXIT ORDER but they
            # do NOT prove the underlying position is closed — the
            # rejected exit means the position is still open and the
            # existing peak/armed state remains the correct basis for
            # the next exit attempt. Resetting peaks here would drop
            # the historic peak that a subsequent trailing-lock retry
            # needs.
            try:
                from app.orders.order_state import TERMINAL_FILL_STATES

                terminal_is_fill = terminal_state in TERMINAL_FILL_STATES
            except Exception:  # pragma: no cover - defensive
                # Failing to import = fail closed (do not reset peaks).
                terminal_is_fill = False
            if terminal_is_fill:
                try:
                    self.manager.reset_position(
                        tenant_id, broker_account_id, symbol,
                    )
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning(
                        "trailing-lock state reset failed for disappeared "
                        "symbol %s (non-fatal): %s",
                        symbol, exc,
                    )
            else:
                log_event(
                    logger,
                    event_type=(
                        "POSITION_TRAILING_LOCK_PEAK_PRESERVED_NON_FILL"
                    ),
                    message=(
                        "Disappeared-symbol marker cleared with NON-FILL "
                        "terminal evidence (CANCELLED / REJECTED / "
                        "EXPIRED / FAILED) — preserving "
                        "PositionTrailingLockManager peak/armed state so "
                        "the next exit attempt for the still-open "
                        "position can rely on the historic peak "
                        "(PR #261 round-5 review P2 — "
                        "exit_engines.py:3274)."
                    ),
                    level=logging.INFO,
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    symbol=symbol,
                    terminal_state=str(terminal_state),
                )
            log_event(
                logger,
                event_type="POSITION_TRAILING_LOCK_INFLIGHT_CLEARED_BY_SNAPSHOT",
                message=(
                    "Inflight marker cleared because the symbol has been "
                    "absent from the broker position snapshot for "
                    f"{min_consecutive_misses} consecutive evaluations AND "
                    "the broker order ledger shows the prior exit in a "
                    "canonical terminal state. Persisted trailing-lock "
                    "manager state was reset ONLY if the terminal status "
                    "is FILLED — non-fill terminal evidence (CANCELLED / "
                    "REJECTED / EXPIRED / FAILED) preserves the peak "
                    "because the underlying position is still open "
                    "(issues #250 + #252; PR #261 round-5 review P2)."
                ),
                level=logging.INFO,
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                symbol=symbol,
                consecutive_missing_snapshots=min_consecutive_misses,
                broker_order_id=broker_order_id,
                terminal_state=str(terminal_state),
            )

    def _marker_has_terminal_broker_evidence(
        self,
        *,
        broker_account_id: Any,
        symbol: str,
        broker_order_id: Optional[str],
        marker_submitted_at: Optional[datetime] = None,
        expected_side: Optional[str] = None,
        expected_quantity: Optional[int] = None,
    ) -> Optional[Any]:
        """Issue #252: return the resolved canonical terminal lifecycle
        state for the marker iff the broker-order ledger shows explicit
        terminal evidence; return None otherwise (and on ANY uncertainty
        — fail closed).

        PR #261 round-5 review P2 (exit_engines.py:3274): callers need
        to distinguish FILLED (the underlying position is now flat) from
        CANCELLED / REJECTED / EXPIRED / FAILED (the position is likely
        STILL OPEN — the exit did not happen). The disappeared-symbol
        sweep was previously resetting the ``PositionTrailingLockManager``
        peak/armed state on every terminal status; that loses the
        historic peak the next exit attempt needs when the terminal
        evidence is a non-fill cancel/reject. Returning the lifecycle
        state lets the caller branch on FILLED-only.

        Truthy/falsy semantics are preserved: ``None`` (not terminal /
        unknown) is falsy; a lifecycle-state enum value (terminal) is
        truthy — so existing call sites using ``if not terminal_evidence``
        continue to behave the same way.

        Two acceptance paths:

        * **Strict** (preferred): the broker_order_id captured at
          submission time is present in the orders snapshot AND its
          status maps to a canonical terminal lifecycle state
          (FILLED / COMPLETE / EXECUTED / CANCELLED / REJECTED /
          FAILED / EXPIRED).
        * **Symbol fallback** (broker_order_id unknown): the most
          recent ledger entry for this symbol that ALSO matches the
          marker's captured ``expected_side`` / ``expected_quantity``
          and is dated at or after ``marker_submitted_at`` (minus a
          small tolerance) is in a canonical terminal state. Without
          these extra criteria an older filled entry order for the
          same symbol (which is common in the broker ledger) would
          satisfy the gate and the engine would clear the marker
          while the actual exit order is still live — reopening the
          very duplicate-submit window this guard is meant to close
          (PR #261 round-2 review P2 — exit_engines.py:3047).

        Returns False on ANY uncertainty — fail closed (hold the
        marker, accept a longer block, avoid duplicate fills).

        PR #261 round-1 review P2: read the FULL broker order snapshot
        via ``state_store.get_order_snapshot()`` rather than
        ``state_store.get_orders()``. AccountRunner's sync writes only
        ``_derive_active_orders(...)`` to ``set_orders``, which DROPS
        terminal statuses (FILLED/CANCELLED/REJECTED/EXPIRED/FAILED)
        before publishing — so the terminal evidence we want is
        invisible there. The full broker ledger (including terminal
        rows) lives in ``get_order_snapshot``. We fall back to
        ``get_orders()`` if the snapshot is empty in case a deployment
        is mid-rollout and only the legacy projection is populated.
        """
        try:
            orders = self.state_store.get_order_snapshot(broker_account_id) or []
        except Exception:  # pragma: no cover - defensive
            orders = []
        if not orders:
            # Fallback for callers / deployments that only populate the
            # legacy ``get_orders`` projection.
            try:
                orders = self.state_store.get_orders(broker_account_id) or []
            except Exception:  # pragma: no cover - defensive
                return None
        if not orders:
            return None
        try:
            from app.orders.order_state import (
                TERMINAL_ORDER_STATES,
                classify_broker_status,
            )
        except Exception:  # pragma: no cover - defensive
            return None

        def _terminal_state(status_str: Any) -> Optional[Any]:
            """Return the canonical terminal lifecycle state for the
            status string, or None if the status is not terminal /
            cannot be classified.

            PR #261 round-1 review P2: ``classify_broker_status`` has
            a fuzzy fallback that maps ANY unrecognized status
            containing ``CANCEL`` (e.g. ``CANCEL_PENDING`` with an
            underscore, which is NOT in the canonical map's
            ``"CANCEL PENDING"`` entry) to ``CANCELLED`` (a terminal
            state). The submit-response path elsewhere in this engine
            explicitly treats ``CANCEL_PENDING`` / ``PENDING_CANCEL``
            / ``CANCEL_REQUESTED`` as non-terminal — if the ledger
            reports a pending cancel while the position snapshot is
            missing, the sweep would otherwise clear the marker and
            reset trailing state before the exit order is actually
            terminal. Explicitly veto pending-cancel variants BEFORE
            delegating.

            PR #261 round-5 review P2 (exit_engines.py:3274): callers
            need to differentiate FILLED from CANCELLED/REJECTED/
            EXPIRED/FAILED — only FILLED proves the position is now
            flat. Return the classified state so the caller can branch.
            """
            try:
                normalised = str(status_str or "").strip().upper().replace("-", "_")
            except Exception:
                normalised = ""
            _PENDING_CANCEL_SUBSTRINGS = (
                "CANCEL_PENDING",
                "PENDING_CANCEL",
                "CANCEL_REQUESTED",
                "CANCEL_REQUEST",
                "CANCEL PENDING",
                "PENDING CANCEL",
                "MODIFY_PENDING",
                "MODIFY PENDING",
            )
            for _sub in _PENDING_CANCEL_SUBSTRINGS:
                if _sub in normalised:
                    return None
            try:
                state = classify_broker_status(status_str)
            except Exception:
                return None
            if state in TERMINAL_ORDER_STATES:
                return state
            return None

        symbol_str = str(symbol)
        if broker_order_id:
            for order in orders:
                if str(getattr(order, "order_id", "") or "") == str(broker_order_id):
                    return _terminal_state(getattr(order, "status", ""))
            # broker_order_id captured but not found in current ledger
            # snapshot — could be a stale snapshot or a missed sync.
            # Fail closed; the inflight timeout will eventually sweep.
            return None
        # broker_order_id unknown — fall back to the most recent
        # ledger entry for this symbol, but only treat it as terminal
        # evidence if it is unambiguously terminal AND it matches the
        # captured marker spec (side opposite to the position, exit
        # quantity in BROKER UNITS — not lots — submitted at or after
        # the marker armed). The broker snapshot reports
        # ``OrderStatus.quantity`` in broker units after the router
        # resolves the request quantity to ``broker_qty``; the spec
        # capture in ``_emit_exit`` therefore records ``broker_units``
        # for an apples-to-apples comparison (PR #261 round-3 review
        # P2 — exit_engines.py:3283). Without these criteria an older
        # filled ENTRY order for the same symbol would satisfy the
        # gate and clear the marker while the actual exit order is
        # still live (PR #261 round-2 review P2 — exit_engines.py:3047).
        #
        # If the caller could not supply ``expected_side`` /
        # ``expected_quantity`` (e.g. legacy code paths that have not
        # been upgraded yet), we fail CLOSED rather than fall back to
        # the historical permissive symbol-only match. The cost of
        # being too strict is a longer wait for the inflight timeout
        # to fire; the cost of being too lax is the duplicate-fill
        # scenario this PR exists to prevent.
        if (
            marker_submitted_at is None
            or expected_side is None
            or expected_quantity is None
        ):
            return None
        # Normalise the marker's wall-clock submission instant to UTC
        # and compute the cutoff (marker armed minus a small tolerance
        # for any clock skew between the engine and the broker).
        try:
            cutoff = marker_submitted_at
            if cutoff.tzinfo is None:
                cutoff = cutoff.replace(tzinfo=timezone.utc)
        except Exception:  # pragma: no cover - defensive
            return None
        from datetime import timedelta as _td
        cutoff = cutoff - _td(seconds=5.0)
        expected_side_str = str(expected_side or "").strip().upper()
        try:
            expected_qty_int = int(expected_quantity or 0)
        except (TypeError, ValueError):
            return None
        if not expected_side_str or expected_qty_int <= 0:
            return None
        candidate = None
        for order in orders:
            if str(getattr(order, "symbol", "") or "") != symbol_str:
                continue
            # Side must match exit direction (opposite of position).
            order_side = str(
                getattr(order, "side", "") or ""
            ).strip().upper()
            if order_side != expected_side_str:
                continue
            # Quantity (lots) must match the marker's exit size.
            try:
                order_qty = int(getattr(order, "quantity", 0) or 0)
            except (TypeError, ValueError):
                continue
            if order_qty != expected_qty_int:
                continue
            # Timestamp must be at or after the marker armed (with
            # tolerance). An entry order filled BEFORE this marker
            # was armed cannot be the exit order.
            updated_at_raw = getattr(order, "updated_at", None)
            if not updated_at_raw:
                continue
            try:
                updated_at = datetime.fromisoformat(
                    str(updated_at_raw).replace("Z", "+00:00")
                )
            except Exception:
                continue
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            if updated_at < cutoff:
                continue
            candidate = order  # rely on caller's list ordering (recency)
        if candidate is None:
            return None
        return _terminal_state(getattr(candidate, "status", ""))

    async def _emit_exit(
        self,
        runner: AccountRunner,
        pos: Any,
        decision: PositionTrailingLockDecision,
    ) -> None:
        tenant_id = runner.tenant_id
        broker_account_id = runner.broker_account_id
        reason = decision.exit_reason or "position_giveback_breach"
        # M3: degraded scope manager check, mirroring _exit_all_positions.
        from app.core.degraded_scope_manager import degraded_scope_manager

        scope_key = f"{broker_account_id}:{decision.symbol}"
        if degraded_scope_manager.is_exit_restricted(scope_key):
            log_event(
                logger,
                event_type="POSITION_TRAILING_LOCK_EXIT_RESTRICTED",
                message="position trailing exit blocked by DegradedScopeManager",
                level=logging.WARNING,
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                symbol=decision.symbol,
                scope_key=scope_key,
            )
            return

        owner_strategy_id = self._resolve_position_owner_strategy_id(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            pos=pos,
        )
        exit_plan = build_position_exit_plan(
            pos,
            tag=reason,
            position_ownership_bypass=True,
            exit_reason=reason,
            account_id=str(broker_account_id),
            strategy_id=owner_strategy_id or "system::position_trailing_lock",
        )
        if not exit_plan.ok or exit_plan.order_req is None:
            log_event(
                logger,
                event_type="POSITION_TRAILING_LOCK_EXIT_SKIPPED",
                message="position trailing exit skipped: invalid exit plan",
                level=logging.WARNING,
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                symbol=decision.symbol,
                peak=decision.peak_unrealized_pnl,
                current=decision.current_unrealized_pnl,
                lock_floor=decision.lock_floor,
                reason=exit_plan.reason,
            )
            return
        if owner_strategy_id:
            exit_plan.order_req.strategy_context = {
                **(exit_plan.order_req.strategy_context or {}),
                "position_owner_strategy_id": owner_strategy_id,
                "exit_actor_strategy_id": "system::position_trailing_lock",
            }
        # Issue #225 (PR #236 review):
        #   P1 — arm marker BEFORE submit_order so that if the router
        #   raises AFTER it placed the broker order (e.g. lifecycle
        #   persistence failure), the marker is still in place to block
        #   duplicate submissions. Without arming pre-submit, a
        #   post-broker raise leaves no record of the in-flight order.
        #   P2 — only KEEP the marker for accepted submissions. If the
        #   router returns a REJECTED/FAILED response (no active runner,
        #   policy interceptor rejection), there is no broker order to
        #   wait for; clear the marker so the next eligible cycle can
        #   try again immediately.
        #
        # PR #261 round-1 review P1: in LIVE, the pre-submit marker
        # persistence MUST be fail-closed. If the UPSERT raises, abort
        # the broker submission rather than silently placing the order
        # without the durable guard. PAPER/SHADOW preserves the
        # historical log-only behaviour since no real broker order is at
        # stake.
        _fail_closed_persist = self._is_live_mode()
        # PR #261 round-2 review P2 (exit_engines.py:3047): capture the
        # expected exit side / quantity BEFORE the pre-submit marker
        # arming so the disappeared-symbol terminal-evidence fallback
        # (when the router never surfaces a broker_order_id) can match
        # the right ledger row rather than any unrelated entry order
        # for the same symbol. Indexed by the same (tenant, account,
        # symbol) key the marker uses.
        #
        # PR #261 round-3 review P2 (exit_engines.py:3283): record
        # ``broker_units`` (not lots). The broker order snapshot reports
        # ``OrderStatus.quantity`` in BROKER UNITS after the router
        # replaces the request quantity with ``broker_qty``. For a
        # derivative position such as 1 lot of NIFTY (50 units) or
        # NATURALGAS (1250 units), a real matching FILLED exit row will
        # have ``quantity=50`` / ``quantity=1250`` while ``expected_qty``
        # captured as ``exit_plan.lots`` would have been ``1`` — the
        # equality check in ``_marker_has_terminal_broker_evidence``
        # would therefore NEVER match and the marker could only clear
        # by timeout. ``PositionExitPlan.broker_units`` is already
        # computed by ``build_position_exit_plan`` and is exactly what
        # the router writes onto the ``OrderRequest.quantity``, so the
        # ledger row's ``quantity`` field will match.
        _key = (
            str(tenant_id), str(broker_account_id), str(decision.symbol),
        )
        try:
            _exit_side = exit_plan.order_req.side
            _exit_side_str = (
                _exit_side.value
                if hasattr(_exit_side, "value")
                else str(_exit_side or "")
            ).strip().upper()
            # Prefer broker_units (units the broker actually sees).
            # Fall back to order_req.quantity (also in broker units
            # after the router resolves lot_size) then to lots if the
            # plan was constructed before broker_units was populated.
            _exit_units_candidates = (
                exit_plan.broker_units,
                getattr(exit_plan.order_req, "quantity", None),
                exit_plan.lots,
            )
            _exit_units: Optional[int] = None
            for _candidate in _exit_units_candidates:
                try:
                    _parsed = int(_candidate)
                except (TypeError, ValueError):
                    continue
                if _parsed > 0:
                    _exit_units = _parsed
                    break
            self._inflight_exit_specs[_key] = (
                _exit_side_str or None,
                _exit_units,
            )
        except Exception:  # pragma: no cover - defensive
            self._inflight_exit_specs[_key] = (None, None)
        try:
            reserved = self._reserve_inflight_marker(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                symbol=decision.symbol,
                broker_order_id=None,  # populated post-submit on success
                fail_closed=_fail_closed_persist,
            )
            if not reserved:
                return
        except Exception as exc:
            # Pre-submit marker persistence failed in LIVE. ``_set_inflight_marker``
            # has already logged the ERROR event and rolled back the in-memory
            # marker. Emit the higher-level skip event so operators see the
            # missed eval cycle in the trailing-lock log stream, then abort
            # without placing the broker order.
            log_event(
                logger,
                event_type="POSITION_TRAILING_LOCK_EXIT_SKIPPED",
                message=(
                    "position trailing exit ABORTED: pre-submit inflight "
                    "marker persistence failed in LIVE — failing closed "
                    "to preserve the duplicate-fill guard "
                    "(PR #261 round-1 review P1)."
                ),
                level=logging.ERROR,
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                symbol=decision.symbol,
                reason="inflight_persist_failed_pre_submit",
                error=repr(exc),
            )
            return
        try:
            submit_result = await self.order_router.submit_order(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                strategy_id=StrategyId("system::position_trailing_lock"),
                order_req=exit_plan.order_req,
            )
            # Inspect response shape to decide whether to keep the
            # marker (broker accepted the submission) or clear it
            # (router rejection — no in-flight order exists).
            broker_order_id: Optional[str] = None
            response_status: str = ""
            try:
                if isinstance(submit_result, tuple) and len(submit_result) >= 2:
                    response = submit_result[1]
                    boi = getattr(response, "broker_order_id", None)
                    if boi:
                        broker_order_id = str(boi)
                    # PR #236 round-5 review P2: STRIP whitespace
                    # before upper-casing so padded broker statuses
                    # like " FILLED" / " CANCELLED" / " REJECTED"
                    # match the canonical classifier set. Mirror
                    # ``classify_broker_status`` semantics exactly.
                    response_status = str(
                        getattr(response, "status", "") or ""
                    ).strip().upper()
            except Exception:  # pragma: no cover - defensive
                broker_order_id = None
                response_status = ""

            # Issue #225 (PR #236 round-2 review P2 + round-3 review P2):
            # treat ALL terminal non-fill statuses as "no broker order to
            # wait for" — and ALSO treat synchronous terminal FILLs as
            # "no need to track inflight" since the order is already
            # done. The router/lifecycle classify CANCELLED/CANCELED/
            # EXPIRED as terminal non-fill outcomes (releasing position
            # ownership). FILLED/COMPLETE are terminal fills.
            #
            # Round-3 P2: also tolerate decorated/normalized status
            # variants (e.g. "REJECTED:reason", "CANCELLED_AT_BROKER",
            # "complete") via prefix matching after upper-case
            # normalization. The clearance set is conservative — only
            # statuses that are definitely terminal qualify.
            # PR #236 round-4 review P2: align with the canonical
            # ``classify_broker_status`` set. Cover BOTH terminal
            # non-fill (``REJECTED``/``REJECT``/``FAILED``/``FAILURE``/
            # ``ERROR``/``CANCELLED``/``CANCELED``/``EXPIRED``) and
            # terminal-fill (``FILLED``/``FULL``/``COMPLETE``/
            # ``EXECUTED``) prefixes. PR #236 round-4 review P3:
            # distinguish the two so a synchronous broker fill is NOT
            # mislabelled as a router rejection in logs/alerts.
            # PR #236 round-6 review P2: ``CANCEL`` (singular, EXACT
            # match) is a canonical terminal cancellation; ``CANCEL
            # REQUESTED``/``CANCEL_PENDING`` are non-terminal pending
            # states and must NOT clear the marker. Use prefix
            # matching only for statuses where decoration is safe
            # (e.g. ``REJECTED:reason``).
            terminal_nonfill_prefixes = (
                "REJECTED",
                "REJECT",
                "FAILED",
                "FAILURE",
                "ERROR",
                "CANCELLED",
                "CANCELED",
                "EXPIRED",
            )
            terminal_nonfill_exact = (
                "CANCEL",
            )
            terminal_fill_prefixes = (
                "FILLED",
                "FULL",
                "COMPLETE",
                "EXECUTED",
            )
            is_terminal_nonfill = (
                response_status in terminal_nonfill_exact
                or any(
                    response_status.startswith(p)
                    for p in terminal_nonfill_prefixes
                )
            )
            is_terminal_fill = any(
                response_status.startswith(p)
                for p in terminal_fill_prefixes
            )
            if is_terminal_nonfill:
                # Router-rejected / non-fill terminal — no broker order
                # to wait for. Clear marker so a subsequent eligible
                # cycle is not blocked by a non-existent in-flight
                # order.
                self._clear_inflight_marker(
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    symbol=decision.symbol,
                )
                log_event(
                    logger,
                    event_type="POSITION_TRAILING_LOCK_EXIT_REJECTED_BY_ROUTER",
                    message=(
                        "Trailing-lock exit was rejected by the router "
                        "(no broker order placed). Inflight marker cleared "
                        "so the next eligible cycle can retry."
                    ),
                    level=logging.WARNING,
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    symbol=decision.symbol,
                    response_status=response_status,
                )
                return
            if is_terminal_fill:
                # Synchronous terminal fill — broker reports the exit
                # is already filled. Clear marker (no inflight order
                # remains) and emit the SUBMITTED event with a
                # ``synchronous_fill`` flag so log/alert review does
                # not mistake a real execution for a router rejection
                # (PR #236 round-4 review P3).
                self._clear_inflight_marker(
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    symbol=decision.symbol,
                )
                # PR #236 round-5 review P2: ALSO reset the persisted
                # trailing-lock manager state for this symbol —
                # OrderLifecycleService projects the fill into the
                # state store immediately for synchronous responses,
                # which means the disappeared-symbol sweep will not
                # see this position again to do the cleanup. Without
                # this reset, a same-symbol re-open is evaluated
                # against the stale peak/armed state.
                try:
                    self.manager.reset_position(
                        tenant_id, broker_account_id, decision.symbol,
                    )
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning(
                        "trailing-lock state reset failed after "
                        "synchronous fill for %s (non-fatal): %s",
                        decision.symbol, exc,
                    )
                log_event(
                    logger,
                    event_type="POSITION_TRAILING_LOCK_EXIT_SUBMITTED",
                    message=(
                        "position trailing exit submitted (synchronous "
                        "terminal fill — broker confirmed completion in "
                        "the submit response)"
                    ),
                    level=logging.WARNING,
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    symbol=decision.symbol,
                    peak_unrealized_pnl=round(decision.peak_unrealized_pnl, 2),
                    current_unrealized_pnl=round(decision.current_unrealized_pnl, 2),
                    lock_floor=round(decision.lock_floor, 2) if decision.lock_floor is not None else None,
                    exit_reason=reason,
                    broker_order_id=broker_order_id,
                    response_status=response_status,
                    synchronous_fill=True,
                )
                return
            # Accepted — refresh marker with broker_order_id (if any) so
            # diagnostics carry the id; the timestamp is implicitly
            # refreshed too which extends the in-flight window from
            # broker-submission time. NB: broker_order_id remains None
            # if the router did not surface it; the marker still blocks.
            self._set_inflight_marker(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                symbol=decision.symbol,
                broker_order_id=broker_order_id,
            )
            log_event(
                logger,
                event_type="POSITION_TRAILING_LOCK_EXIT_SUBMITTED",
                message="position trailing exit submitted",
                level=logging.WARNING,
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                symbol=decision.symbol,
                peak_unrealized_pnl=round(decision.peak_unrealized_pnl, 2),
                current_unrealized_pnl=round(decision.current_unrealized_pnl, 2),
                lock_floor=round(decision.lock_floor, 2) if decision.lock_floor is not None else None,
                exit_reason=reason,
                broker_order_id=broker_order_id,
            )
        except Exception as exc:
            # Critical: the marker remains armed so a post-broker-submit
            # exception (e.g. lifecycle persistence failed AFTER broker
            # accepted the order) does not leave the engine free to
            # submit another exit. The marker will time out via
            # _is_inflight_blocked if no terminal observation comes
            # through, surfacing as POSITION_TRAILING_LOCK_INFLIGHT_TIMEOUT
            # for operator action.
            #
            # PR #236 round-4 review P2: REFRESH the marker timestamp
            # here. The marker was timestamped pre-submit; a slow
            # broker call that raises AFTER possibly placing the order
            # can consume most of ``inflight_max_seconds`` before the
            # exception fires, leaving the next watchdog cycle to
            # immediately treat the marker as stale and submit another
            # exit even though the broker order may have just become
            # untracked. Refreshing the timestamp gives the marker a
            # full timeout window starting from the failure observation,
            # so operator action / order reconciliation has time to
            # land before the marker auto-clears.
            self._set_inflight_marker(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                symbol=decision.symbol,
                broker_order_id=None,
            )
            log_event(
                logger,
                event_type="POSITION_TRAILING_LOCK_EXIT_ERROR",
                message=(
                    "position trailing exit submission failed; inflight "
                    "marker REMAINS armed (and timestamp refreshed) "
                    "because the router raised AFTER potentially placing "
                    "the broker order — preserves idempotency until the "
                    "marker times out (issue #225 PR #236 review P1, "
                    "round-4 review P2)."
                ),
                level=logging.ERROR,
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                symbol=decision.symbol,
                error=repr(exc),
            )


__all__ = [
    "PositionExitPlan",
    "build_position_exit_plan",
    "ProfitSweepEngine",
    "EODExitEngine",
    "PositionTrailingLockEngine",
]
