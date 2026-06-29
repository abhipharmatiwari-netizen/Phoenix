"""
RiskEngine: high-level risk rules per tenant/account/strategy.

Current behavior:
- Enforce max_daily_loss using PnLEngine account-level snapshots.
- Avoid in-memory fallback state for critical risk decisions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from app.brokers.base import OrderPurpose, OrderRequest
from app.core.identifiers import BrokerAccountId, StrategyId, TenantId
from app.config.runtime_config import get_runtime_settings
from app.config.settings import get_settings
from app.core.logging_utils import log_event
from app.pnl.pnl_engine import PnLEngine
from app.risk.account_loss_guard import AccountLossGuard, account_loss_guard

logger = logging.getLogger(__name__)


# Risk configuration values per account.
@dataclass
class RiskConfig:
    """
    Per-account risk configuration.

    max_daily_loss is an absolute loss threshold (e.g., INR).
    """

    max_daily_loss: Optional[float] = None  # opt-in


# In-memory risk state per account.
@dataclass
class RiskState:
    """
    Backward-compatible placeholder for historical API contracts.

    Not used for risk-limit enforcement.
    """

    realized_pnl: float = 0.0


class RiskEngine:
    """
    Risk engine for account-level daily loss protection.

    Uses PnLEngine as the source of truth for realized/unrealized PnL. Missing
    PnL state can be configured as fail-open, but defaults to fail-closed.
    """

    def __init__(
        self,
        pnl_engine: Optional[PnLEngine] = None,
        account_loss_guard_store: Optional[AccountLossGuard] = None,
    ) -> None:
        self._configs: Dict[Tuple[TenantId, BrokerAccountId], RiskConfig] = {}
        self._pnl_engine = pnl_engine
        self._account_loss_guard = (
            account_loss_guard_store
            if account_loss_guard_store is not None
            else account_loss_guard
        )

    def get_config(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
    ) -> RiskConfig:
        key = (tenant_id, broker_account_id)
        cfg = self._configs.get(key)
        if cfg is None:
            cfg = RiskConfig()
            self._configs[key] = cfg
        return cfg

    def update_realized_pnl(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        delta_pnl: float,
    ) -> None:
        # Deprecated: in-memory realized PnL updates are intentionally ignored.
        logger.debug(
            "RiskEngine.update_realized_pnl ignored tenant=%s broker_account=%s delta=%s",
            tenant_id,
            broker_account_id,
            delta_pnl,
        )

    def _missing_pnl_decision(
        self,
        *,
        settings,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        max_daily_loss: float,
        reason: str,
        is_exit_order: bool,
        is_risk_reducing: bool,
    ) -> tuple[bool, str]:
        order_kind = "exit" if is_exit_order else ("adjust" if is_risk_reducing else "entry")
        strict_fail_closed = bool(
            getattr(settings, "risk_fail_closed_on_missing_pnl", False)
        )
        if strict_fail_closed:
            allowed = bool(is_exit_order or is_risk_reducing)
            resolved_reason = (
                "missing_pnl_fail_closed_exit_allowed"
                if allowed
                else "missing_pnl_fail_closed"
            )
            log_event(
                logger,
                event_type="RISK_CHECK",
                message="risk check decision",
                level=logging.INFO if allowed else logging.WARNING,
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                strategy_id=strategy_id,
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                max_daily_loss=float(max_daily_loss or 0.0),
                allowed=allowed,
                reason=resolved_reason,
                missing_pnl_reason=reason,
                order_kind=order_kind,
                risk_reducing=is_risk_reducing,
            )
            return allowed, resolved_reason
        fail_open = bool(getattr(settings, "risk_fail_open_on_missing_pnl", False))
        allowed = bool(fail_open)
        resolved_reason = reason
        if fail_open:
            resolved_reason = f"{reason}_fail_open"
        log_event(
            logger,
            event_type="RISK_CHECK",
            message="risk check decision",
            level=logging.INFO if allowed else logging.WARNING,
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            max_daily_loss=float(max_daily_loss or 0.0),
            allowed=allowed,
            reason=resolved_reason,
            order_kind=order_kind,
            risk_reducing=is_risk_reducing,
        )
        return allowed, resolved_reason

    def check_order_allowed(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        order: OrderRequest,
    ) -> tuple[bool, str]:
        base_settings = get_settings()
        settings = get_runtime_settings(
            base_settings=base_settings,
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
        )
        cfg = self.get_config(tenant_id, broker_account_id)
        is_exit_order = bool(getattr(order, "is_exit_order", False))
        is_risk_reducing = (
            is_exit_order
            or getattr(order, "purpose", None) == OrderPurpose.ADJUST
        )

        if not bool(getattr(settings, "risk_enable_daily_loss", False)):
            decision = True, "daily_loss_guard_disabled"
            log_event(
                logger,
                event_type="RISK_CHECK",
                message="risk check decision",
                level=logging.INFO,
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                strategy_id=strategy_id,
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                max_daily_loss=0.0,
                allowed=decision[0],
                reason=decision[1],
            )
            return decision

        max_daily_loss = getattr(settings, "risk_max_daily_loss", None)
        if max_daily_loss is None:
            max_daily_loss = cfg.max_daily_loss
        include_unrealized = bool(
            getattr(settings, "risk_include_unrealized_in_daily_loss", True)
        )
        if max_daily_loss is None:
            decision = True, "max_daily_loss_not_configured"
            log_event(
                logger,
                event_type="RISK_CHECK",
                message="risk check decision",
                level=logging.INFO,
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                strategy_id=strategy_id,
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                max_daily_loss=0.0,
                allowed=decision[0],
                reason=decision[1],
            )
            return decision

        guard_snapshot = None
        if self._account_loss_guard is not None:
            guard_snapshot = self._account_loss_guard.get_snapshot(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
            )

        guard_realized_value: Optional[float] = None
        guard_unrealized_value: Optional[float] = None
        guard_total_value: Optional[float] = None
        if guard_snapshot is not None:
            guard_realized_value = float(guard_snapshot.daily_realized_pnl)
            guard_unrealized_value = float(guard_snapshot.unrealized_pnl)
            guard_total_value = float(guard_snapshot.daily_total_pnl)
            guard_limit = float(max_daily_loss or guard_snapshot.max_daily_loss or 0.0)
            if bool(guard_snapshot.kill_switch_activated):
                reason = "kill_switch_active"
                log_event(
                    logger,
                    event_type="RISK_CHECK",
                    message="risk check decision",
                    level=logging.WARNING,
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    strategy_id=strategy_id,
                    realized_pnl=guard_realized_value,
                    unrealized_pnl=float(guard_unrealized_value or 0.0),
                    max_daily_loss=guard_limit,
                    allowed=False,
                    reason=reason,
                )
                return False, reason

        realized = None
        total = None
        if self._pnl_engine is not None:
            realized = self._pnl_engine.get_current_realized_pnl(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                strategy_id=None,
            )
            total = self._pnl_engine.get_current_total_pnl(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                strategy_id=None,
            )

        if (
            realized is None
            and guard_realized_value is None
            and (
                (not include_unrealized)
                or (guard_total_value is None and total is None)
            )
        ):
            missing_reason = (
                "pnl_engine_unavailable"
                if self._pnl_engine is None
                else "pnl_snapshot_unavailable"
            )
            return self._missing_pnl_decision(
                settings=settings,
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                strategy_id=strategy_id,
                max_daily_loss=float(max_daily_loss),
                reason=missing_reason,
                is_exit_order=is_exit_order,
                is_risk_reducing=is_risk_reducing,
            )

        if include_unrealized and guard_total_value is None and total is None:
            return self._missing_pnl_decision(
                settings=settings,
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                strategy_id=strategy_id,
                max_daily_loss=float(max_daily_loss),
                reason="pnl_snapshot_unavailable",
                is_exit_order=is_exit_order,
                is_risk_reducing=is_risk_reducing,
            )

        realized_value: Optional[float] = None
        if realized is None:
            if guard_realized_value is not None:
                realized_value = float(guard_realized_value)
        elif guard_realized_value is None:
            realized_value = float(realized)
        else:
            realized_value = min(float(realized), float(guard_realized_value))

        total_value: Optional[float] = None
        if include_unrealized:
            if total is None:
                if guard_total_value is not None:
                    total_value = float(guard_total_value)
            elif guard_total_value is None:
                total_value = float(total)
            else:
                total_value = min(float(total), float(guard_total_value))

        unrealized_value = 0.0
        derived_unrealized: Optional[float] = None
        if total_value is not None and realized_value is not None:
            derived_unrealized = float(total_value) - float(realized_value)
        if guard_unrealized_value is None:
            unrealized_value = float(derived_unrealized or 0.0)
        elif derived_unrealized is None:
            unrealized_value = float(guard_unrealized_value)
        else:
            unrealized_value = min(
                float(guard_unrealized_value),
                float(derived_unrealized),
            )

        comparison_value: Optional[float] = None
        if include_unrealized and total_value is not None:
            comparison_value = float(total_value)
        elif realized_value is not None:
            comparison_value = float(realized_value)

        if comparison_value is None:
            missing_reason = (
                "pnl_engine_unavailable"
                if self._pnl_engine is None
                else "pnl_snapshot_unavailable"
            )
            return self._missing_pnl_decision(
                settings=settings,
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                strategy_id=strategy_id,
                max_daily_loss=float(max_daily_loss),
                reason=missing_reason,
                is_exit_order=is_exit_order,
                is_risk_reducing=is_risk_reducing,
            )

        allowed = comparison_value > -abs(float(max_daily_loss))
        reason = "risk_ok"
        if not allowed:
            reason = (
                f"max_daily_loss_reached_current_{comparison_value:.2f}"
                f"_limit_{-abs(float(max_daily_loss)):.2f}"
            )

        log_event(
            logger,
            event_type="RISK_CHECK",
            message="risk check decision",
            level=logging.INFO if allowed else logging.WARNING,
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
            realized_pnl=float(realized_value or 0.0),
            unrealized_pnl=unrealized_value,
            max_daily_loss=float(max_daily_loss or 0.0),
            allowed=allowed,
            reason=reason,
        )
        return allowed, reason


__all__ = ["RiskEngine", "RiskConfig", "RiskState"]
