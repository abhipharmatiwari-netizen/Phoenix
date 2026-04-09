"""Position sizing framework — PHX-STRAT-003.

Size decisions incorporate:
  - Available capital and current utilization
  - Per-symbol volatility (ATR or realized vol)
  - Liquidity (average daily volume fraction)
  - Signal confidence (0.0–1.0)
  - Drawdown state (reduce size when in drawdown)
  - Hard lot-size constraints (exchange minimum)

Strategies cannot bypass sizing rules — the framework enforces
maximum sizes and always returns a valid (possibly zero) quantity.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class SizingInputs:
    """Inputs required for position sizing."""
    capital: float                   # total capital available (base currency)
    capital_used: float              # currently deployed capital
    symbol: str
    price: float                     # current price of the instrument
    volatility_pct: float            # realized volatility as % of price (e.g. 0.02 = 2%)
    confidence: float                # signal confidence 0.0–1.0
    current_drawdown_pct: float = 0.0   # current drawdown as fraction (0.05 = 5%)
    adv_fraction_limit: float = 0.01    # max fraction of average daily volume
    adv: Optional[float] = None         # average daily volume (contracts/shares)
    lot_size: int = 1                   # exchange minimum lot size
    max_position_pct: float = 0.10      # max position as fraction of capital


@dataclass
class SizingResult:
    qty: int                         # final quantity (may be 0)
    capped_by: str = ""              # which constraint was binding
    intended_qty_float: float = 0.0  # raw float before lot rounding
    capital_at_risk: float = 0.0     # qty * price


class PositionSizer:
    """Deterministic, policy-bounded position sizer."""

    def size(self, inputs: SizingInputs) -> SizingResult:
        """Calculate position size. Returns SizingResult with qty >= 0."""
        if inputs.price <= 0:
            return SizingResult(qty=0, capped_by="zero_price")

        # 1. Risk-based size: risk 1% of capital per trade, scaled by confidence
        risk_budget_pct = 0.01 * max(0.0, min(1.0, inputs.confidence))
        risk_budget = inputs.capital * risk_budget_pct

        # Stop-loss assumed at 2x volatility
        stop_distance = inputs.price * inputs.volatility_pct * 2.0 if inputs.volatility_pct > 0 else inputs.price * 0.02
        vol_based_qty = risk_budget / stop_distance if stop_distance > 0 else 0.0
        capped_by = "volatility_risk"

        # 2. Capital limit: max_position_pct of available capital
        capital_available = max(0.0, inputs.capital - inputs.capital_used)
        capital_qty = (capital_available * inputs.max_position_pct) / inputs.price
        if capital_qty < vol_based_qty:
            vol_based_qty = capital_qty
            capped_by = "capital_limit"

        # 3. Drawdown reduction: linearly reduce size as drawdown increases
        if inputs.current_drawdown_pct > 0:
            drawdown_scalar = max(0.0, 1.0 - inputs.current_drawdown_pct * 5.0)  # 0% drawdown=1.0x, 20% drawdown=0x
            if drawdown_scalar < 1.0:
                vol_based_qty *= drawdown_scalar
                capped_by = "drawdown_reduction"

        # 4. Liquidity limit
        if inputs.adv is not None and inputs.adv > 0:
            adv_qty = inputs.adv * inputs.adv_fraction_limit
            if adv_qty < vol_based_qty:
                vol_based_qty = adv_qty
                capped_by = "adv_limit"

        # 5. Confidence floor: zero size if confidence below threshold
        if inputs.confidence < 0.3:
            return SizingResult(
                qty=0,
                capped_by="confidence_too_low",
                intended_qty_float=vol_based_qty,
            )

        # 6. Lot size rounding
        lot = max(1, int(inputs.lot_size))
        raw_qty = max(0.0, vol_based_qty)
        final_qty = int(math.floor(raw_qty / lot) * lot)

        logger.debug(
            "position_sizing symbol=%s raw=%.2f final=%d capped_by=%s confidence=%.2f",
            inputs.symbol, raw_qty, final_qty, capped_by, inputs.confidence,
        )
        return SizingResult(
            qty=final_qty,
            capped_by=capped_by if final_qty < raw_qty else "",
            intended_qty_float=raw_qty,
            capital_at_risk=final_qty * inputs.price,
        )


# Module-level singleton
position_sizer = PositionSizer()


__all__ = ["PositionSizer", "SizingInputs", "SizingResult", "position_sizer"]
