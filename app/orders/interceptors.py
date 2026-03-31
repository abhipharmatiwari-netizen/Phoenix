"""
Order routing policy interceptors.

Interceptors implement a common interface and can be composed into a pipeline
without changing OrderRouter core logic.
"""

from __future__ import annotations

import inspect
import logging
import os
from dataclasses import dataclass
from dataclasses import replace
from typing import Any, Optional, Protocol

from app.brokers.base import OrderPurpose, OrderRequest, OrderResponse
from app.core.identifiers import BrokerAccountId, StrategyId, TenantId
from app.core.logging_utils import log_event
from app.orders.position_ownership import (
    ContractKey,
    PositionOwnershipStore,
    UNKNOWN_MODE_BLOCK_ENTRIES,
    UNKNOWN_OWNER,
    derive_contract_key,
    render_contract,
)
from app.risk.capital_engine import CapitalEngine
from app.risk.circuit_breaker import TradingCircuitBreaker
from app.risk.exposure_limiter import (
    ExposureLimits,
    check_exposure_limits,
    compute_exposure_snapshot,
)
from app.risk.profit_engine import ProfitEngine
from app.risk.risk_engine import RiskEngine

logger = logging.getLogger(__name__)


def _is_true_env(name: str) -> bool:
    return str(os.getenv(name, "0")).strip().lower() in {"1", "true", "yes", "on"}


def _has_explicit_attr(obj: object, name: str) -> bool:
    try:
        return inspect.getattr_static(obj, name) is not None
    except AttributeError:
        return False


@dataclass
class OrderInterceptionContext:
    tenant_id: TenantId
    broker_account_id: BrokerAccountId
    strategy_id: StrategyId
    correlation_id: Optional[str]
    request_id: Optional[str]
    order_req: OrderRequest
    settings: Any
    is_exit_order: bool
    lot_size: Optional[int]
    instrument_meta: Optional[dict[str, Any]]
    reference_price: Optional[float]
    balance: Any
    positions: Any
    capital_engine: Optional[CapitalEngine]
    risk_engine: Optional[RiskEngine]
    profit_engine: Optional[ProfitEngine]
    position_ownership_store: Optional[PositionOwnershipStore] = None
    position_ownership_contract_key: Optional[ContractKey] = None
    position_ownership_contract_text: Optional[str] = None
    position_ownership_lock_acquired: bool = False
    position_ownership_fill_owner_strategy_id: Optional[StrategyId] = None
    position_ownership_unknown_reason: Optional[str] = None
    exposure_limits: Optional[ExposureLimits] = None
    circuit_breaker: Optional[TradingCircuitBreaker] = None


class OrderInterceptor(Protocol):
    def evaluate(self, ctx: OrderInterceptionContext) -> Optional[OrderResponse]: ...


def _rejected_response(reason: str) -> OrderResponse:
    return OrderResponse(
        broker_order_id="",
        status="REJECTED",
        message=reason,
        filled_quantity=0,
        average_price=None,
    )


class CapitalResizeInterceptor:
    def evaluate(self, ctx: OrderInterceptionContext) -> Optional[OrderResponse]:
        if (
            ctx.is_exit_order
            or ctx.capital_engine is None
            or not bool(getattr(ctx.settings, "enable_capital_checks", False))
            or not bool(getattr(ctx.settings, "capital_auto_resize_enabled", False))
        ):
            return None
        resized_qty, sizing_reason = ctx.capital_engine.suggest_order_quantity(
            tenant_id=ctx.tenant_id,
            broker_account_id=ctx.broker_account_id,
            order=ctx.order_req,
            lot_size=ctx.lot_size,
            reference_price=ctx.reference_price,
        )
        if resized_qty <= 0:
            log_event(
                logger,
                event_type="ORDER_REJECTED_BY_RISK",
                message=sizing_reason,
                tenant_id=ctx.tenant_id,
                broker_account_id=ctx.broker_account_id,
                strategy_id=ctx.strategy_id,
                correlation_id=ctx.correlation_id,
                request_id=ctx.request_id,
                instrument=ctx.order_req.symbol,
                level=logging.WARNING,
                origin="capital_sizing",
            )
            return _rejected_response(sizing_reason)

        if resized_qty != int(ctx.order_req.quantity):
            original_qty = int(ctx.order_req.quantity)
            ctx.order_req = replace(ctx.order_req, quantity=int(resized_qty))
            log_event(
                logger,
                event_type="ORDER_QTY_ADJUSTED_FOR_CAPITAL",
                message=sizing_reason,
                tenant_id=ctx.tenant_id,
                broker_account_id=ctx.broker_account_id,
                strategy_id=ctx.strategy_id,
                correlation_id=ctx.correlation_id,
                request_id=ctx.request_id,
                instrument=ctx.order_req.symbol,
                level=logging.INFO,
                origin="capital_sizing",
                original_qty=original_qty,
                adjusted_qty=int(resized_qty),
                lot_size=ctx.lot_size,
                reference_price=ctx.reference_price,
            )
        return None


class CapitalGuardInterceptor:
    def evaluate(self, ctx: OrderInterceptionContext) -> Optional[OrderResponse]:
        ok_capital = True
        capital_reason = "capital_checks_disabled"
        decision = None
        if (
            not ctx.is_exit_order
            and ctx.capital_engine is not None
            and bool(getattr(ctx.settings, "enable_capital_checks", False))
        ):
            if _has_explicit_attr(ctx.capital_engine, "evaluate_order"):
                decision = ctx.capital_engine.evaluate_order(
                    tenant_id=ctx.tenant_id,
                    broker_account_id=ctx.broker_account_id,
                    strategy_id=ctx.strategy_id,
                    balance=ctx.balance,
                    positions=ctx.positions,
                    order=ctx.order_req,
                    reference_price=ctx.reference_price,
                    instrument_meta=ctx.instrument_meta,
                    lot_size=ctx.lot_size,
                )
                ok_capital = bool(decision.allowed)
                capital_reason = str(decision.reason)
            else:
                ok_capital, capital_reason = ctx.capital_engine.can_afford_order(
                    tenant_id=ctx.tenant_id,
                    broker_account_id=ctx.broker_account_id,
                    strategy_id=ctx.strategy_id,
                    balance=ctx.balance,
                    positions=ctx.positions,
                    order=ctx.order_req,
                    reference_price=ctx.reference_price,
                )
        if ok_capital:
            return None

        log_event(
            logger,
            event_type="ORDER_REJECTED_BY_RISK",
            message=capital_reason,
            tenant_id=ctx.tenant_id,
            broker_account_id=ctx.broker_account_id,
            strategy_id=ctx.strategy_id,
            correlation_id=ctx.correlation_id,
            request_id=ctx.request_id,
            instrument=ctx.order_req.symbol,
            level=logging.WARNING,
            origin="capital",
            capital_reason_code=getattr(decision, "reason_code", None),
            capital_margin_mode=getattr(decision, "mode", None),
            required_capital=getattr(decision, "required_capital", None),
            available_capital=getattr(decision, "available_capital", None),
        )
        return _rejected_response(capital_reason)


class RiskGuardInterceptor:
    def evaluate(self, ctx: OrderInterceptionContext) -> Optional[OrderResponse]:
        ok_risk = True
        risk_reason = "risk_checks_disabled"
        if (
            not ctx.is_exit_order
            and ctx.risk_engine is not None
            and bool(getattr(ctx.settings, "enable_risk_checks", False))
        ):
            ok_risk, risk_reason = ctx.risk_engine.check_order_allowed(
                tenant_id=ctx.tenant_id,
                broker_account_id=ctx.broker_account_id,
                strategy_id=ctx.strategy_id,
                order=ctx.order_req,
            )
        if ok_risk:
            return None

        log_event(
            logger,
            event_type="ORDER_REJECTED_BY_RISK",
            message=risk_reason,
            tenant_id=ctx.tenant_id,
            broker_account_id=ctx.broker_account_id,
            strategy_id=ctx.strategy_id,
            correlation_id=ctx.correlation_id,
            request_id=ctx.request_id,
            instrument=ctx.order_req.symbol,
            level=logging.WARNING,
            origin="risk",
        )
        return _rejected_response(risk_reason)


class ProfitGuardInterceptor:
    def evaluate(self, ctx: OrderInterceptionContext) -> Optional[OrderResponse]:
        if ctx.is_exit_order:
            log_event(
                logger,
                event_type="PROFIT_CHECK_SKIPPED_EXIT_ORDER",
                message="Skipping profit guard for exit order",
                level=logging.INFO,
                tenant_id=ctx.tenant_id,
                broker_account_id=ctx.broker_account_id,
                strategy_id=ctx.strategy_id,
                correlation_id=ctx.correlation_id,
                request_id=ctx.request_id,
                instrument=ctx.order_req.symbol,
            )
            return None

        profit_ok = True
        profit_reason = "profit_checks_disabled"
        if ctx.profit_engine is not None and bool(
            getattr(ctx.settings, "enable_profit_checks", False)
        ):
            profit_decision = ctx.profit_engine.check_order_allowed(
                tenant_id=ctx.tenant_id,
                broker_account_id=ctx.broker_account_id,
                strategy_id=ctx.strategy_id,
            )
            profit_ok = bool(getattr(profit_decision, "allowed", False))
            profit_reason = (
                getattr(profit_decision, "reason", None) or "blocked_by_profit_engine"
            )
        if profit_ok:
            return None

        log_event(
            logger,
            event_type="ORDER_REJECTED_BY_PROFIT",
            message=profit_reason,
            level=logging.WARNING,
            tenant_id=ctx.tenant_id,
            broker_account_id=ctx.broker_account_id,
            strategy_id=ctx.strategy_id,
            correlation_id=ctx.correlation_id,
            request_id=ctx.request_id,
            instrument=ctx.order_req.symbol,
            origin="profit",
        )
        return _rejected_response(profit_reason)


class GlobalKillSwitchInterceptor:
    def evaluate(self, ctx: OrderInterceptionContext) -> Optional[OrderResponse]:
        if not bool(
            getattr(ctx.settings, "order_router_enforce_global_kill_switch", False)
        ):
            return None
        if ctx.is_exit_order:
            return None
        if not _is_true_env("GLOBAL_KILL"):
            return None
        reason = "global_kill_switch_enabled"
        log_event(
            logger,
            event_type="ORDER_REJECTED_GLOBAL_KILL",
            message=reason,
            level=logging.WARNING,
            tenant_id=ctx.tenant_id,
            broker_account_id=ctx.broker_account_id,
            strategy_id=ctx.strategy_id,
            correlation_id=ctx.correlation_id,
            request_id=ctx.request_id,
            instrument=ctx.order_req.symbol,
            origin="global_kill_switch",
        )
        return _rejected_response(reason)


class PositionOwnershipInterceptor:
    def __init__(self, *, store: Optional[PositionOwnershipStore] = None) -> None:
        self._store = store or PositionOwnershipStore()

    @staticmethod
    def _unknown_mode(settings: Any) -> str:
        mode = str(
            getattr(
                settings,
                "position_ownership_unknown_mode",
                UNKNOWN_MODE_BLOCK_ENTRIES,
            )
            or UNKNOWN_MODE_BLOCK_ENTRIES
        ).strip()
        return mode.lower() if mode else UNKNOWN_MODE_BLOCK_ENTRIES

    @staticmethod
    def _is_explicit_system_exit_bypass(order_req: OrderRequest) -> bool:
        return bool(getattr(order_req, "position_ownership_bypass", False))

    @staticmethod
    def _reject_message(
        *,
        owner: Optional[str],
        requested: StrategyId,
        contract_text: str,
    ) -> str:
        owner_text = owner or UNKNOWN_OWNER
        return (
            f"CONTRACT_LOCKED owner={owner_text} "
            f"requested={requested} contract={contract_text}"
        )

    def evaluate(self, ctx: OrderInterceptionContext) -> Optional[OrderResponse]:
        if not bool(getattr(ctx.settings, "position_ownership_enabled", False)):
            return None

        store = ctx.position_ownership_store or self._store
        ctx.position_ownership_store = store

        contract_key, unknown_reason = derive_contract_key(ctx.order_req)
        contract_text = render_contract(contract_key)
        ctx.position_ownership_contract_key = contract_key
        ctx.position_ownership_contract_text = contract_text
        ctx.position_ownership_unknown_reason = unknown_reason

        if (
            contract_key is not None
            and getattr(ctx.order_req, "purpose", None) == OrderPurpose.ENTRY
            and str(os.getenv("TRADE_MODE", "PAPER") or "PAPER").strip().upper() == "LIVE"
            and not str(getattr(contract_key, "broker_token", "") or "").strip()
        ):
            reason = (
                f"MISSING_BROKER_TOKEN contract={contract_text} "
                "live entry orders require broker-token-backed contract identity"
            )
            log_event(
                logger,
                event_type="ORDER_REJECTED_MISSING_BROKER_TOKEN",
                message=reason,
                level=logging.WARNING,
                tenant_id=ctx.tenant_id,
                broker_account_id=ctx.broker_account_id,
                strategy_id=ctx.strategy_id,
                correlation_id=ctx.correlation_id,
                request_id=ctx.request_id,
                instrument=ctx.order_req.symbol,
                contract=contract_text,
            )
            return _rejected_response(reason)

        unknown_mode = self._unknown_mode(ctx.settings)
        decision = store.try_acquire(
            tenant_id=ctx.tenant_id,
            broker_account_id=ctx.broker_account_id,
            contract_key=contract_key,
            strategy_id=ctx.strategy_id,
            is_exit_order=ctx.is_exit_order,
            unknown_mode=unknown_mode,
        )
        if decision.allowed:
            ctx.position_ownership_lock_acquired = bool(decision.acquired_pending)
            return None

        allow_system_exit = bool(
            getattr(ctx.settings, "position_ownership_allow_system_exit", True)
        )
        explicit_bypass = self._is_explicit_system_exit_bypass(ctx.order_req)
        owner = decision.owner

        if ctx.is_exit_order and allow_system_exit and explicit_bypass:
            if owner not in (None, UNKNOWN_OWNER):
                owner_strategy_id = StrategyId(owner)
                owner_decision = store.try_acquire(
                    tenant_id=ctx.tenant_id,
                    broker_account_id=ctx.broker_account_id,
                    contract_key=contract_key,
                    strategy_id=owner_strategy_id,
                    is_exit_order=True,
                    unknown_mode=unknown_mode,
                )
                if owner_decision.allowed:
                    ctx.position_ownership_lock_acquired = bool(
                        owner_decision.acquired_pending
                    )
                    ctx.position_ownership_fill_owner_strategy_id = owner_strategy_id
            log_event(
                logger,
                event_type="ORDER_POSITION_OWNERSHIP_BYPASS",
                message="system_exit_bypass",
                level=logging.WARNING,
                tenant_id=ctx.tenant_id,
                broker_account_id=ctx.broker_account_id,
                strategy_id=ctx.strategy_id,
                correlation_id=ctx.correlation_id,
                request_id=ctx.request_id,
                instrument=ctx.order_req.symbol,
                owner=owner,
                contract=contract_text,
            )
            return None

        reason = self._reject_message(
            owner=owner,
            requested=ctx.strategy_id,
            contract_text=contract_text,
        )
        if unknown_reason:
            reason = f"{reason} detail={unknown_reason}"
        log_event(
            logger,
            event_type="ORDER_REJECTED_CONTRACT_LOCKED",
            message=reason,
            level=logging.WARNING,
            tenant_id=ctx.tenant_id,
            broker_account_id=ctx.broker_account_id,
            strategy_id=ctx.strategy_id,
            correlation_id=ctx.correlation_id,
            request_id=ctx.request_id,
            instrument=ctx.order_req.symbol,
            owner=owner,
            contract=contract_text,
        )
        return _rejected_response(reason)


class ExposureLimitInterceptor:
    """RISK-5.3: Block entries that breach per-account exposure limits."""

    def evaluate(self, ctx: OrderInterceptionContext) -> Optional[OrderResponse]:
        if ctx.is_exit_order:
            return None  # Exits always pass
        limits = ctx.exposure_limits
        if limits is None or not limits.enabled:
            return None

        snapshot = compute_exposure_snapshot(ctx.positions)
        underlying = str(
            (ctx.instrument_meta or {}).get("underlying", "")
        ).upper() or None
        ref_price = ctx.reference_price or 0.0
        qty = int(ctx.order_req.quantity or 0)
        notional = qty * ref_price

        side = str(getattr(ctx.order_req, "side", "") or "").upper()
        is_short_option = side in {"SELL", "S"} and "OPT" in str(
            (ctx.instrument_meta or {}).get("instrument_type", "")
        ).upper()

        allowed, code, message = check_exposure_limits(
            snapshot, limits,
            new_order_underlying=underlying,
            new_order_notional=notional,
            is_new_short_option=is_short_option,
        )
        if not allowed:
            log_event(
                logger,
                event_type="ORDER_REJECTED_EXPOSURE_LIMIT",
                message=message,
                level=logging.WARNING,
                tenant_id=ctx.tenant_id,
                broker_account_id=ctx.broker_account_id,
                strategy_id=ctx.strategy_id,
                correlation_id=ctx.correlation_id,
                request_id=ctx.request_id,
                instrument=ctx.order_req.symbol,
                reason_code=code,
            )
            return _rejected_response(f"Exposure limit: {message}")
        return None


class CircuitBreakerInterceptor:
    """RISK-5.4: Block entries when circuit breakers are tripped."""

    def evaluate(self, ctx: OrderInterceptionContext) -> Optional[OrderResponse]:
        if ctx.is_exit_order:
            return None  # Exits always pass
        cb = ctx.circuit_breaker
        if cb is None:
            return None

        allowed, reason = cb.is_entry_allowed()
        if not allowed:
            log_event(
                logger,
                event_type="ORDER_REJECTED_CIRCUIT_BREAKER",
                message=f"Circuit breaker: {reason}",
                level=logging.WARNING,
                tenant_id=ctx.tenant_id,
                broker_account_id=ctx.broker_account_id,
                strategy_id=ctx.strategy_id,
                correlation_id=ctx.correlation_id,
                request_id=ctx.request_id,
                instrument=ctx.order_req.symbol,
            )
            return _rejected_response(f"Circuit breaker: {reason}")
        return None


class BrokerTokenGuardInterceptor:
    """C3: Fail-closed guard rejecting LIVE entry orders with empty broker_token.

    Architecture §4 requires that any component unable to derive the
    normalized OwnershipKey (which depends on broker_token) must fail
    closed for fresh entries.
    """

    def evaluate(self, ctx: OrderInterceptionContext) -> Optional[OrderResponse]:
        if ctx.is_exit_order:
            return None
        # Only enforce in LIVE mode
        trade_mode = str(
            getattr(ctx.settings, "trade_mode", "") or os.getenv("TRADE_MODE", "PAPER")
        ).strip().upper()
        if trade_mode != "LIVE":
            return None

        contract_key = ctx.position_ownership_contract_key
        if contract_key is None:
            return None

        if not contract_key.broker_token:
            reason = (
                f"LIVE entry rejected: broker_token is empty for "
                f"{contract_key.as_log_text()}. §4 requires broker_token "
                f"for OwnershipKey derivation (fail-closed)."
            )
            log_event(
                logger,
                event_type="ORDER_REJECTED_MISSING_BROKER_TOKEN",
                message=reason,
                tenant_id=ctx.tenant_id,
                broker_account_id=ctx.broker_account_id,
                strategy_id=ctx.strategy_id,
                correlation_id=ctx.correlation_id,
                request_id=ctx.request_id,
                instrument=ctx.order_req.symbol,
                level=logging.WARNING,
                origin="broker_token_guard",
            )
            return _rejected_response(reason)
        return None


def build_default_interceptors(
    *,
    position_ownership_store: Optional[PositionOwnershipStore] = None,
) -> tuple[OrderInterceptor, ...]:
    return (
        CapitalResizeInterceptor(),
        CapitalGuardInterceptor(),
        RiskGuardInterceptor(),
        GlobalKillSwitchInterceptor(),
        ExposureLimitInterceptor(),
        ProfitGuardInterceptor(),
        PositionOwnershipInterceptor(store=position_ownership_store),
        BrokerTokenGuardInterceptor(),
        CircuitBreakerInterceptor(),
    )


__all__ = [
    "BrokerTokenGuardInterceptor",
    "OrderInterceptionContext",
    "OrderInterceptor",
    "CapitalResizeInterceptor",
    "CapitalGuardInterceptor",
    "RiskGuardInterceptor",
    "ProfitGuardInterceptor",
    "GlobalKillSwitchInterceptor",
    "PositionOwnershipInterceptor",
    "ExposureLimitInterceptor",
    "CircuitBreakerInterceptor",
    "build_default_interceptors",
]
