"""
ProfitEngine: decide when to sweep profits based on daily total PnL.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.config.settings import get_settings
from app.core.identifiers import BrokerAccountId, StrategyId, TenantId
from app.pnl.pnl_engine import PnLEngine


# Configuration for profit sweep thresholds.
@dataclass
class ProfitConfig:
    enable_daily_target: bool
    daily_target: Optional[float]


# Decision result for whether to sweep profits.
@dataclass
class ProfitDecision:
    should_sweep: bool
    reason: str
    current_total_pnl: float
    target: Optional[float]


class ProfitEngine:
    """
    Profit booking engine.

    For now, it only implements a simple per-(tenant, account, strategy) daily
    profit target: once total PnL >= target, we recommend sweeping profits
    (e.g. by closing all open positions for that account).

    Integration with actual square-off is handled by Hub/OrderRouter.
    """

    # Initialize with a PnL engine.
    def __init__(self, pnl_engine: PnLEngine) -> None:
        self._pnl_engine = pnl_engine

    # Resolve profit sweep config for a tenant/account/strategy.
    def _get_config_for(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        is_paper: bool,
    ) -> ProfitConfig:
        settings = get_settings()

        if not settings.profit_enable_daily_target:
            return ProfitConfig(enable_daily_target=False, daily_target=None)

        target: Optional[float] = None
        if is_paper and settings.profit_daily_target_paper is not None:
            target = settings.profit_daily_target_paper
        else:
            target = settings.profit_daily_target

        if target is None or target <= 0:
            return ProfitConfig(enable_daily_target=False, daily_target=None)

        return ProfitConfig(enable_daily_target=True, daily_target=target)

    # Decide whether the profit target is met and a sweep should occur.
    def check_should_sweep(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        is_paper: bool,
    ) -> ProfitDecision:
        """
        Determine whether profit sweep should be triggered for the given
        tenant/account/strategy based on daily total PnL.
        """
        cfg = self._get_config_for(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
            is_paper=is_paper,
        )
        if not cfg.enable_daily_target or cfg.daily_target is None:
            return ProfitDecision(
                should_sweep=False,
                reason="profit_target_disabled",
                current_total_pnl=0.0,
                target=None,
            )

        snapshot = self._pnl_engine.get_snapshot(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
        )
        if snapshot is None:
            return ProfitDecision(
                should_sweep=False,
                reason="no_pnl_snapshot",
                current_total_pnl=0.0,
                target=cfg.daily_target,
            )

        realized = float(snapshot.realized_pnl or 0.0)
        unrealized = float(snapshot.unrealized_pnl or 0.0)
        total = realized + unrealized

        if total >= cfg.daily_target:
            return ProfitDecision(
                should_sweep=True,
                reason="daily_target_reached",
                current_total_pnl=total,
                target=cfg.daily_target,
            )

        return ProfitDecision(
            should_sweep=False,
            reason="below_target",
            current_total_pnl=total,
            target=cfg.daily_target,
        )


__all__ = ["ProfitEngine", "ProfitDecision", "ProfitConfig"]

# Summary:
# - ProfitEngine implements a simple daily profit target per (tenant, account, strategy).
# - Hub's profit watchdog loop uses it to trigger one-time "sweep all positions"
#   orders via OrderRouter when daily total PnL >= configured target.
# - Feature is opt-in via PROFIT_ENABLE_DAILY_TARGET / PROFIT_DAILY_TARGET envs.
