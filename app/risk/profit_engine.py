"""
ProfitEngine: optional daily profit target guard to block new orders.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config.runtime_config import get_runtime_settings
from app.config.settings import get_settings
from app.core.identifiers import BrokerAccountId, StrategyId, TenantId
from app.core.logging_utils import log_event
from app.data.state_store import StateStore
from app.pnl.pnl_engine import PnLEngine
from app.pnl.profit_lock import ProfitLockManager

logger = logging.getLogger(__name__)


# Decision outcome for profit guard checks.
@dataclass
class ProfitDecision:
    allowed: bool
    reason: str


class ProfitEngine:
    """
    ProfitEngine enforces an optional daily profit target:

    - When disabled, it always allows orders.
    - When enabled and profit_daily_target is set, it blocks NEW orders once
      total_pnl >= profit_daily_target for a given (tenant, account).
    """

    # Initialize with PnL engine and optional state/lock managers.
    def __init__(
        self,
        pnl_engine: PnLEngine,
        *,
        state_store: StateStore | None = None,
        profit_lock_manager: ProfitLockManager | None = None,
    ) -> None:
        self._pnl_engine = pnl_engine
        self._state_store = state_store
        self._profit_lock_manager = profit_lock_manager

    # Check whether a new order is allowed under profit guard rules.
    def check_order_allowed(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
    ) -> ProfitDecision:
        base_settings = get_settings()
        settings = get_runtime_settings(
            base_settings=base_settings,
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
        )

        # Master toggle for profit checks
        if not settings.enable_profit_checks:
            return ProfitDecision(allowed=True, reason="profit_checks_disabled")

        if not settings.profit_enable_daily_target:
            return ProfitDecision(allowed=True, reason="profit_target_guard_disabled")

        target = settings.profit_daily_target
        if target is None or target <= 0:
            return ProfitDecision(allowed=True, reason="profit_target_not_configured")

        # §86: Prefer control_total_pnl (economic P&L: entry_premium − exit_cost)
        # over get_current_total_pnl (cash ledger) for the profit lock gate.
        # The cash ledger books the full short-option premium received on entry
        # immediately, making total_pnl spike to entry_price×qty on the first fill
        # and prematurely arming the profit lock.  Control PnL only accumulates
        # at economic close, giving the correct signal for whether the strategy
        # has actually captured profit.  Falls back to cash ledger when no
        # control data exists (non-short-option strategies, or before first fill).
        _control_pnl = None
        if hasattr(self._pnl_engine, "get_control_total_pnl"):
            try:
                _control_pnl = self._pnl_engine.get_control_total_pnl(
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                )
            except Exception:
                _control_pnl = None

        if _control_pnl is not None:
            total_pnl = float(_control_pnl)
        else:
            total_pnl = float(
                self._pnl_engine.get_current_total_pnl(
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                )
                or 0.0
            )

        signal_valid = None
        if self._state_store is not None:
            try:
                signal_valid = self._state_store.get_strategy_signal_valid(
                    broker_account_id
                )
            except Exception:
                signal_valid = None

        lock_active = False
        lock_floor = None
        if (
            self._profit_lock_manager is not None
            and getattr(settings, "profit_lock_enabled", True)
        ):
            lock_decision = self._profit_lock_manager.evaluate(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                total_pnl=total_pnl,
                target=target,
                giveback_pct=float(getattr(settings, "profit_lock_giveback_pct", 0.1)),
                signal_valid=signal_valid,
                require_signal=bool(getattr(settings, "profit_lock_require_signal", True)),
                exit_cooldown_seconds=float(
                    getattr(settings, "profit_lock_exit_cooldown_seconds", 30.0)
                ),
            )
            lock_active = lock_decision.lock_active
            lock_floor = lock_decision.lock_floor
        else:
            lock_active = total_pnl >= target

        if lock_active:
            # §87: Distinguish between "lock just triggered" vs "lock is active
            # from a historical peak" so the reason string is factually correct.
            lock_floor_val = lock_floor if lock_floor is not None else target
            peak_val = getattr(lock_decision, "peak_total_pnl", None) if (
                self._profit_lock_manager is not None
                and getattr(settings, "profit_lock_enabled", True)
            ) else None
            if peak_val is not None and total_pnl < float(target):
                # Lock is active from a historical peak, not the current value.
                reason = (
                    f"profit_lock_active: peak_pnl={peak_val:.2f} lock_floor={lock_floor_val:.2f} "
                    f"current={total_pnl:.2f} target={target:.2f}"
                )
            else:
                reason = (
                    f"daily profit target reached: total_pnl={total_pnl:.2f} >= target={target:.2f}"
                )
            if settings.profit_block_new_orders_on_target:
                decision = ProfitDecision(allowed=False, reason=reason)
            else:
                decision = ProfitDecision(
                    allowed=True, reason=reason + " (not enforced)"
                )
        else:
            reason = f"profit_target_not_reached: total_pnl={total_pnl:.2f}, target={target:.2f}"
            decision = ProfitDecision(allowed=True, reason=reason)

        _peak_for_log = getattr(lock_decision, "peak_total_pnl", None) if (
            self._profit_lock_manager is not None
            and getattr(settings, "profit_lock_enabled", True)
        ) else None
        log_event(
            logger,
            event_type="PROFIT_CHECK",
            message="profit check decision",
            level=logging.INFO if decision.allowed else logging.WARNING,
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
            total_pnl=total_pnl,
            profit_daily_target=float(target or 0.0),
            lock_active=bool(lock_active),
            lock_floor=lock_floor,
            peak_total_pnl=_peak_for_log,
            allowed=bool(decision.allowed),
            reason=decision.reason,
        )

        return decision


__all__ = ["ProfitEngine", "ProfitDecision"]
