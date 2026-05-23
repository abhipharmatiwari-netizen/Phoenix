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
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional, Protocol

from app.brokers.base import OrderPurpose, OrderRequest, OrderResponse
from app.core.identifiers import BrokerAccountId, StrategyId, TenantId
from app.core.logging_utils import log_event
from app.data.option_chain_provider import OptionQuote
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
from app.risk.kill_switch import KillSwitchScope
from app.risk.option_sell_guard import (
    OptionSellGuardConfig,
    OptionSellGuardContext,
    OptionSellStructure,
    evaluate_option_sell_guard,
)
from app.risk.profit_engine import ProfitEngine
from app.risk.risk_engine import RiskEngine
from app.strategies.identifiers import OI_ML_CE_SELLER_ID

logger = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))


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

        # Issue #220: env-var kill switch can choose to block exits too via
        # GLOBAL_KILL_BLOCK_EXITS=1. Default (env GLOBAL_KILL=1 alone) is
        # SOFT — entries blocked, exits allowed.
        if _is_true_env("GLOBAL_KILL"):
            env_block_exits = _is_true_env("GLOBAL_KILL_BLOCK_EXITS")
            if not ctx.is_exit_order or env_block_exits:
                reason = (
                    "global_kill_switch_enabled_block_exits"
                    if (ctx.is_exit_order and env_block_exits)
                    else "global_kill_switch_enabled"
                )
                log_event(
                    logger,
                    event_type=(
                        "ORDER_REJECTED_GLOBAL_KILL_BLOCK_EXITS"
                        if (ctx.is_exit_order and env_block_exits)
                        else "ORDER_REJECTED_GLOBAL_KILL"
                    ),
                    message=reason,
                    level=logging.WARNING,
                    tenant_id=ctx.tenant_id,
                    broker_account_id=ctx.broker_account_id,
                    strategy_id=ctx.strategy_id,
                    correlation_id=ctx.correlation_id,
                    request_id=ctx.request_id,
                    instrument=ctx.order_req.symbol,
                    origin="global_kill_switch_env",
                )
                return _rejected_response(reason)
            # SOFT env trip + exit order: fall through to the durable KSM
            # check below in case a durable record sets block_exits=True at
            # a more specific scope.

        # Check durable KillSwitchManager (Architecture §12.1 — primary path in LIVE).
        try:
            from app.hub.runtime import get_hub_runtime
            ksm = getattr(get_hub_runtime(), "kill_switch_manager", None)
            if ksm is not None:
                # Issue #220: hierarchical query that also surfaces whether
                # any active record at any matching scope is a HARD trip
                # (block_exits=True). For exit orders, only HARD trips
                # reject; entries are always rejected when tripped.
                tripped, ksm_block_exits = ksm.is_tripped_for_scope_with_block_exits(
                    tenant_id=str(ctx.tenant_id or "") or None,
                    account_id=str(ctx.broker_account_id or "") or None,
                    strategy_id=str(ctx.strategy_id or "") or None,
                )
                if tripped:
                    if ctx.is_exit_order and not ksm_block_exits:
                        # SOFT trip + exit order: allow (current default).
                        # No log event — exit flow under SOFT trip is the
                        # intended path for legacy auto-trip recovery.
                        return None
                    reason = (
                        "kill_switch_manager_tripped_block_exits"
                        if ctx.is_exit_order
                        else "kill_switch_manager_tripped"
                    )
                    log_event(
                        logger,
                        event_type=(
                            "ORDER_REJECTED_KILL_SWITCH_MANAGER_BLOCK_EXITS"
                            if ctx.is_exit_order
                            else "ORDER_REJECTED_KILL_SWITCH_MANAGER"
                        ),
                        message=reason,
                        level=logging.WARNING,
                        tenant_id=ctx.tenant_id,
                        broker_account_id=ctx.broker_account_id,
                        strategy_id=ctx.strategy_id,
                        correlation_id=ctx.correlation_id,
                        request_id=ctx.request_id,
                        instrument=ctx.order_req.symbol,
                        origin="kill_switch_manager",
                        is_exit_order=bool(ctx.is_exit_order),
                        block_exits=bool(ksm_block_exits),
                    )
                    return _rejected_response(reason)
        except Exception as _ks_exc:
            # §93: In LIVE mode fail CLOSED when durable kill-switch state is
            # unavailable — a missing KillSwitchManager means we cannot prove
            # the kill switch is inactive.  In non-LIVE modes, fail open so the
            # env-var path remains the fallback without disrupting PAPER/SHADOW.
            _live = str(os.getenv("TRADE_MODE", "PAPER") or "PAPER").strip().upper() == "LIVE"
            if _live:
                reason = "kill_switch_manager_unavailable"
                log_event(
                    logger,
                    event_type="ORDER_REJECTED_KILL_SWITCH_UNAVAILABLE",
                    message="Durable kill-switch state unavailable in LIVE — failing closed",
                    level=logging.ERROR,
                    tenant_id=ctx.tenant_id,
                    broker_account_id=ctx.broker_account_id,
                    strategy_id=ctx.strategy_id,
                    correlation_id=ctx.correlation_id,
                    request_id=ctx.request_id,
                    instrument=ctx.order_req.symbol,
                    origin="kill_switch_manager",
                    error=str(_ks_exc),
                )
                return _rejected_response(reason)

        return None


class OptionSellGuardInterceptor:
    """Fail-closed entry guard for the OI/ML CE seller.

    The pure guard in ``app.risk.option_sell_guard`` is intentionally reusable
    strategy logic. This interceptor is the hub authority layer: it requires a
    complete ``OrderRequest.strategy_context`` before an OI/ML entry can reach
    broker routing and preserves exit handling for EOD / break-glass paths.
    """

    _QUOTE_KEYS = ("quote", "option_quote", "short_quote")

    def evaluate(self, ctx: OrderInterceptionContext) -> Optional[OrderResponse]:
        if str(ctx.strategy_id) != OI_ML_CE_SELLER_ID:
            return None
        if ctx.is_exit_order:
            return None

        strategy_context = getattr(ctx.order_req, "strategy_context", None)
        if not isinstance(strategy_context, dict):
            return self._reject(ctx, ("missing_strategy_context",))

        context = dict(strategy_context)
        extra_reasons = self._validate_router_only_risk_context(ctx, context)
        if self._structure(context) is not OptionSellStructure.BEAR_CALL_SPREAD:
            extra_reasons.append("spread_only_v1_required")

        guard_context, guard_reasons = self._build_guard_context(ctx, context)
        extra_reasons.extend(guard_reasons)

        guard_result = None
        if guard_context is not None:
            guard_result = evaluate_option_sell_guard(
                guard_context,
                self._guard_config(ctx, context),
            )
            if not guard_result.allowed:
                extra_reasons.extend(str(reason) for reason in guard_result.reasons)

        deduped = _dedupe(extra_reasons)
        if deduped:
            self._trip_strategy_soft_kill_if_needed(ctx, deduped)
            metadata = dict(getattr(guard_result, "metadata", {}) or {})
            return self._reject(ctx, tuple(deduped), metadata=metadata)

        return None

    def _build_guard_context(
        self,
        ctx: OrderInterceptionContext,
        data: dict[str, Any],
    ) -> tuple[OptionSellGuardContext | None, list[str]]:
        reasons: list[str] = []
        quote = self._quote(data, reasons)
        return (
            OptionSellGuardContext(
                now=self._now(data),
                structure=self._structure(data) or "",
                quote=quote,
                ml_score=_first_present(data, "ml_score", "score", "probability"),
                predicted_mae_premium=_first_present(
                    data,
                    "predicted_mae_premium",
                    "mae_premium",
                    "predicted_mae",
                ),
                premium_received=_first_present(
                    data,
                    "premium_received",
                    "net_credit_points",
                    "credit_points",
                ),
                max_loss_rupees=_first_present(
                    data,
                    "max_loss_rupees",
                    "estimated_max_loss_rupees",
                    "spread_max_loss_rupees",
                ),
                vix=_first_present(data, "vix", "india_vix"),
                strategy_id=str(ctx.strategy_id),
                option_type=str(data.get("option_type") or "CE"),
                is_exit=False,
                tenant_id=str(ctx.tenant_id),
                account_id=str(ctx.broker_account_id),
                kill_switch_manager=self._kill_switch_manager(),
                metadata=data,
            ),
            reasons,
        )

    def _guard_config(
        self,
        ctx: OrderInterceptionContext,
        data: dict[str, Any],
    ) -> OptionSellGuardConfig:
        live = self._is_live(ctx)
        return OptionSellGuardConfig(
            strategy_id=OI_ML_CE_SELLER_ID,
            allow_naked=False,
            require_kill_switch_manager=live,
            max_quote_age_seconds=int(
                _float_or(
                    _first_present(
                        data,
                        "max_quote_age_seconds",
                        "max_data_age_seconds",
                    ),
                    120.0,
                )
            ),
            max_entry_vix=_float_or(data.get("max_entry_vix"), 22.0),
            min_spread_ml_score=_float_or(data.get("min_spread_ml_score"), 0.55),
            max_mae_to_premium=_float_or(data.get("max_mae_to_premium"), 1.20),
            max_spread_loss_rupees=_float_or(
                _first_present(
                    data,
                    "max_spread_loss_limit_rupees",
                    "max_spread_loss_rupees",
                ),
                5000.0,
            ),
        )

    @staticmethod
    def _structure(data: dict[str, Any]) -> OptionSellStructure | None:
        value = _first_present(data, "structure", "option_sell_structure")
        try:
            return OptionSellStructure(str(value).strip().upper())
        except Exception:
            return None

    def _quote(self, data: dict[str, Any], reasons: list[str]) -> OptionQuote | None:
        raw_quote = None
        for key in self._QUOTE_KEYS:
            if key in data:
                raw_quote = data.get(key)
                break
        if isinstance(raw_quote, OptionQuote):
            return raw_quote
        if not isinstance(raw_quote, dict):
            reasons.append("missing_option_quote")
            return None
        try:
            quote_data = dict(raw_quote)
            quote_data["snapshot_ts"] = _parse_datetime(quote_data.get("snapshot_ts"))
            quote_data["source_ts"] = _parse_optional_datetime(quote_data.get("source_ts"))
            quote_data["ingested_at"] = _parse_optional_datetime(quote_data.get("ingested_at"))
            quote_data["expiry"] = _parse_date(quote_data.get("expiry"))
            return OptionQuote(**quote_data)
        except Exception:
            reasons.append("invalid_option_quote")
            return None

    def _validate_router_only_risk_context(
        self,
        ctx: OrderInterceptionContext,
        data: dict[str, Any],
    ) -> list[str]:
        reasons: list[str] = []
        live = self._is_live(ctx)

        if live:
            pnl_fresh = _bool_or_none(
                _first_present(data, "pnl_fresh", "pnl_state_fresh")
            )
            pnl_age = _float_or_none(
                _first_present(data, "pnl_age_seconds", "pnl_snapshot_age_seconds")
            )
            max_pnl_age = _float_or(
                _first_present(data, "max_pnl_age_seconds", "max_risk_age_seconds"),
                120.0,
            )
            if pnl_fresh is False:
                reasons.append("stale_pnl_state")
            elif pnl_age is None and pnl_fresh is not True:
                reasons.append("missing_pnl_state")
            elif pnl_age is not None and pnl_age > max_pnl_age:
                reasons.append("stale_pnl_state")

        data_fresh = _bool_or_none(_first_present(data, "data_fresh", "quote_fresh"))
        data_age = _float_or_none(_first_present(data, "data_age_seconds", "quote_age_seconds"))
        max_data_age = _float_or(
            _first_present(data, "max_data_age_seconds", "max_quote_age_seconds"),
            120.0,
        )
        if data_fresh is False:
            reasons.append("stale_data_state")
        elif data_age is not None and data_age > max_data_age:
            reasons.append("stale_data_state")

        max_loss = _float_or_none(
            _first_present(
                data,
                "max_loss_rupees",
                "estimated_max_loss_rupees",
                "spread_max_loss_rupees",
            )
        )
        current_open_risk = _float_or_none(data.get("current_open_risk_rupees"))
        max_open_risk = _float_or_none(data.get("max_open_risk_rupees"))
        if max_open_risk is not None:
            if current_open_risk is None or max_loss is None:
                reasons.append("missing_open_risk_state")
            elif current_open_risk + max_loss > max_open_risk:
                reasons.append("open_risk_above_limit")

        self._validate_loss_limit(
            reasons,
            data=data,
            loss_key="daily_loss_rupees",
            pnl_key="daily_realized_pnl",
            limit_key="daily_loss_limit_rupees",
            reason="daily_loss_limit_breached",
            missing_reason="missing_daily_loss_state",
        )
        self._validate_loss_limit(
            reasons,
            data=data,
            loss_key="weekly_loss_rupees",
            pnl_key="weekly_realized_pnl",
            limit_key="weekly_loss_limit_rupees",
            reason="weekly_loss_limit_breached",
            missing_reason="missing_weekly_loss_state",
        )
        return reasons

    @staticmethod
    def _validate_loss_limit(
        reasons: list[str],
        *,
        data: dict[str, Any],
        loss_key: str,
        pnl_key: str,
        limit_key: str,
        reason: str,
        missing_reason: str,
    ) -> None:
        limit = _float_or_none(data.get(limit_key))
        if limit is None:
            return
        raw_loss = _float_or_none(data.get(loss_key))
        if raw_loss is None:
            pnl = _float_or_none(data.get(pnl_key))
            raw_loss = max(0.0, -pnl) if pnl is not None else None
        if raw_loss is None:
            reasons.append(missing_reason)
            return
        if raw_loss >= limit:
            reasons.append(reason)

    def _trip_strategy_soft_kill_if_needed(
        self,
        ctx: OrderInterceptionContext,
        reasons: list[str],
    ) -> None:
        if "daily_loss_limit_breached" not in reasons:
            return
        manager = self._kill_switch_manager()
        if manager is None:
            return
        try:
            manager.trip(
                KillSwitchScope.STRATEGY,
                OI_ML_CE_SELLER_ID,
                reason="oi_ml_ce_seller_daily_loss_limit_breached",
                actor="option_sell_guard",
                block_exits=False,
            )
        except ValueError:
            # Already tripped or clear-pending. That still leaves entries
            # blocked and exits governed by the existing SOFT/HARD state.
            return
        except Exception as exc:
            log_event(
                logger,
                event_type="OPTION_SELL_GUARD_KILL_SWITCH_TRIP_FAILED",
                message="OptionSellGuard failed to trip strategy kill switch",
                level=logging.ERROR,
                tenant_id=ctx.tenant_id,
                broker_account_id=ctx.broker_account_id,
                strategy_id=ctx.strategy_id,
                correlation_id=ctx.correlation_id,
                request_id=ctx.request_id,
                instrument=ctx.order_req.symbol,
                error=str(exc),
            )

    @staticmethod
    def _kill_switch_manager() -> Any:
        try:
            from app.hub.runtime import get_hub_runtime
            return getattr(get_hub_runtime(), "kill_switch_manager", None)
        except Exception:
            return None

    @staticmethod
    def _is_live(ctx: OrderInterceptionContext) -> bool:
        mode = str(
            getattr(ctx.settings, "trade_mode", "")
            or os.getenv("TRADE_MODE", "PAPER")
        ).strip().upper()
        return mode == "LIVE"

    @staticmethod
    def _now(data: dict[str, Any]) -> datetime:
        raw = _first_present(data, "decision_ts", "now", "created_at")
        parsed = _parse_optional_datetime(raw)
        return parsed or datetime.now(IST)

    def _reject(
        self,
        ctx: OrderInterceptionContext,
        reasons: tuple[str, ...],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> OrderResponse:
        message = "option_sell_guard_rejected:" + ",".join(reasons)
        log_event(
            logger,
            event_type="ORDER_REJECTED_OPTION_SELL_GUARD",
            message=message,
            level=logging.WARNING,
            tenant_id=ctx.tenant_id,
            broker_account_id=ctx.broker_account_id,
            strategy_id=ctx.strategy_id,
            correlation_id=ctx.correlation_id,
            request_id=ctx.request_id,
            instrument=ctx.order_req.symbol,
            reasons=list(reasons),
        )
        return OrderResponse(
            broker_order_id="",
            status="REJECTED",
            message=message,
            filled_quantity=0,
            average_price=None,
            details={
                "origin": "option_sell_guard",
                "reasons": list(reasons),
                "metadata": metadata or {},
            },
        )


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
                else:
                    reason = self._reject_message(
                        owner=owner_decision.owner or owner,
                        requested=ctx.strategy_id,
                        contract_text=contract_text,
                    )
                    reason = f"{reason} detail={owner_decision.reason}"
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
                        owner=owner_decision.owner or owner,
                        contract=contract_text,
                        reason=owner_decision.reason,
                    )
                    return _rejected_response(reason)
            else:
                reason = self._reject_message(
                    owner=owner,
                    requested=ctx.strategy_id,
                    contract_text=contract_text,
                )
                reason = f"{reason} detail={decision.reason}"
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
                    reason=decision.reason,
                )
                return _rejected_response(reason)
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


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _first_present(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data.get(key) not in (None, ""):
            return data.get(key)
    return None


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _float_or(value: Any, default: float) -> float:
    parsed = _float_or_none(value)
    return default if parsed is None else parsed


def _bool_or_none(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return None


def _parse_optional_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    return _parse_datetime(value)


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed


def _parse_date(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return date.fromisoformat(str(value).strip())


def build_default_interceptors(
    *,
    position_ownership_store: Optional[PositionOwnershipStore] = None,
) -> tuple[OrderInterceptor, ...]:
    return (
        CapitalResizeInterceptor(),
        CapitalGuardInterceptor(),
        RiskGuardInterceptor(),
        GlobalKillSwitchInterceptor(),
        OptionSellGuardInterceptor(),
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
    "OptionSellGuardInterceptor",
    "PositionOwnershipInterceptor",
    "ExposureLimitInterceptor",
    "CircuitBreakerInterceptor",
    "build_default_interceptors",
]
