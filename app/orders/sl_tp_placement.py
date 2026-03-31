"""
EXIT-5.2: Standardized stop-loss / target placement helpers.

Centralizes SL/TP logic with consistent tick offsets, broker quirk fallbacks
(e.g., SLM rejection → SL-L), and configurable timeouts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class SLOrderType(str, Enum):
    SLM = "SL-M"  # Stop-loss market (preferred)
    SLL = "SL-L"  # Stop-loss limit (fallback for brokers that reject SLM)


@dataclass
class SLTPConfig:
    """Configuration for SL/TP placement."""

    # Default tick offset for SL trigger above/below entry
    sl_tick_offset: float = 0.05
    # Default profit target percentage
    tp_pct: float = 0.50
    # Default stoploss percentage
    sl_pct: float = 0.30
    # Preferred SL order type
    preferred_sl_type: SLOrderType = SLOrderType.SLM
    # Fallback SL type when preferred is rejected
    fallback_sl_type: SLOrderType = SLOrderType.SLL
    # Limit offset for SL-L orders (distance from trigger for limit price)
    sl_limit_offset_pct: float = 0.5
    # Max retries for SL placement
    max_retries: int = 2
    # Timeout for SL confirmation (seconds)
    confirmation_timeout_seconds: float = 8.0


@dataclass
class SLTPParams:
    """Computed SL/TP prices for an order."""

    entry_price: float
    stoploss_trigger: float
    stoploss_limit: Optional[float]  # None for SL-M
    target_price: float
    sl_order_type: SLOrderType
    tick_size: float

    @property
    def risk_reward_ratio(self) -> float:
        risk = abs(self.entry_price - self.stoploss_trigger)
        reward = abs(self.target_price - self.entry_price)
        if risk == 0:
            return 0.0
        return reward / risk


def round_to_tick(price: float, tick_size: float) -> float:
    """Round price to nearest tick size."""
    if tick_size <= 0:
        return price
    return round(round(price / tick_size) * tick_size, 10)


def compute_sl_tp(
    entry_price: float,
    side: str,  # "BUY" or "SELL"
    *,
    sl_pct: Optional[float] = None,
    tp_pct: Optional[float] = None,
    tick_size: float = 0.05,
    config: Optional[SLTPConfig] = None,
) -> SLTPParams:
    """
    Compute SL/TP prices for an entry order.

    For BUY entries: SL below entry, TP above entry.
    For SELL entries: SL above entry, TP below entry.
    """
    cfg = config or SLTPConfig()
    sl_pct_val = sl_pct if sl_pct is not None else cfg.sl_pct
    tp_pct_val = tp_pct if tp_pct is not None else cfg.tp_pct

    is_buy = side.upper() in {"BUY", "B"}

    if is_buy:
        sl_trigger = round_to_tick(
            entry_price * (1 - sl_pct_val / 100), tick_size
        )
        target = round_to_tick(
            entry_price * (1 + tp_pct_val / 100), tick_size
        )
    else:
        sl_trigger = round_to_tick(
            entry_price * (1 + sl_pct_val / 100), tick_size
        )
        target = round_to_tick(
            entry_price * (1 - tp_pct_val / 100), tick_size
        )

    # Compute limit price for SL-L orders
    sl_limit = None
    if cfg.preferred_sl_type == SLOrderType.SLL:
        offset = entry_price * cfg.sl_limit_offset_pct / 100
        if is_buy:
            sl_limit = round_to_tick(sl_trigger - offset, tick_size)
        else:
            sl_limit = round_to_tick(sl_trigger + offset, tick_size)

    return SLTPParams(
        entry_price=entry_price,
        stoploss_trigger=sl_trigger,
        stoploss_limit=sl_limit,
        target_price=target,
        sl_order_type=cfg.preferred_sl_type,
        tick_size=tick_size,
    )


def compute_fallback_sl(
    params: SLTPParams,
    config: Optional[SLTPConfig] = None,
) -> SLTPParams:
    """
    Compute fallback SL params when primary SL order type is rejected.
    Switches SL-M → SL-L with a limit offset.
    """
    cfg = config or SLTPConfig()

    if params.sl_order_type == SLOrderType.SLM:
        # Fallback to SL-L
        offset = params.entry_price * cfg.sl_limit_offset_pct / 100
        is_buy = params.target_price > params.entry_price
        if is_buy:
            sl_limit = round_to_tick(
                params.stoploss_trigger - offset, params.tick_size
            )
        else:
            sl_limit = round_to_tick(
                params.stoploss_trigger + offset, params.tick_size
            )
        logger.info(
            "sl_fallback from=%s to=%s trigger=%.2f limit=%.2f",
            SLOrderType.SLM.value,
            SLOrderType.SLL.value,
            params.stoploss_trigger,
            sl_limit,
        )
        return SLTPParams(
            entry_price=params.entry_price,
            stoploss_trigger=params.stoploss_trigger,
            stoploss_limit=sl_limit,
            target_price=params.target_price,
            sl_order_type=SLOrderType.SLL,
            tick_size=params.tick_size,
        )

    # Already on fallback type — no further fallback
    logger.warning(
        "sl_fallback_exhausted current_type=%s no further fallback",
        params.sl_order_type.value,
    )
    return params


__all__ = [
    "SLOrderType",
    "SLTPConfig",
    "SLTPParams",
    "round_to_tick",
    "compute_sl_tp",
    "compute_fallback_sl",
]
