"""
RISK-5.3: Per-account exposure limits.

Enforces max open positions, max per-underlying notional, and max short
options exposure. Exits always bypass.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class ExposureLimits:
    """Per-account exposure configuration."""

    max_open_positions: int = 20
    max_per_underlying_notional: float = 5_000_000.0
    max_short_options_legs: int = 10
    enabled: bool = True


@dataclass
class ExposureSnapshot:
    """Current exposure state for an account."""

    open_position_count: int
    per_underlying_notional: dict[str, float]
    short_options_legs: int


def compute_exposure_snapshot(positions: Any) -> ExposureSnapshot:
    """Compute exposure from a list of position dicts/objects."""
    if not positions:
        return ExposureSnapshot(
            open_position_count=0,
            per_underlying_notional={},
            short_options_legs=0,
        )

    open_count = 0
    notional_by_underlying: dict[str, float] = {}
    short_legs = 0

    for pos in positions:
        qty = abs(int(getattr(pos, "quantity", 0) or getattr(pos, "net_qty", 0) or 0))
        if qty == 0:
            continue

        open_count += 1
        underlying = str(
            getattr(pos, "underlying", None)
            or getattr(pos, "tradingsymbol", "UNKNOWN")
        ).upper()

        ltp = float(getattr(pos, "ltp", 0) or getattr(pos, "last_price", 0) or 0)
        notional = qty * ltp
        notional_by_underlying[underlying] = (
            notional_by_underlying.get(underlying, 0.0) + notional
        )

        # Detect short options
        product = str(getattr(pos, "product_type", "") or "").upper()
        side_raw = str(getattr(pos, "side", "") or getattr(pos, "net_side", "") or "").upper()
        is_short = side_raw in {"SELL", "SHORT", "S"}
        is_option = product in {"OPTSTK", "OPTIDX"} or "OPT" in str(
            getattr(pos, "instrument_type", "") or ""
        ).upper()
        if is_short and is_option:
            short_legs += 1

    return ExposureSnapshot(
        open_position_count=open_count,
        per_underlying_notional=notional_by_underlying,
        short_options_legs=short_legs,
    )


def check_exposure_limits(
    snapshot: ExposureSnapshot,
    limits: ExposureLimits,
    new_order_underlying: Optional[str] = None,
    new_order_notional: float = 0.0,
    is_new_short_option: bool = False,
) -> tuple[bool, Optional[str], Optional[str]]:
    """
    Check if a new entry order would breach exposure limits.

    Returns:
        (allowed, reason_code, message)
    """
    if not limits.enabled:
        return True, None, None

    # Max open positions
    if snapshot.open_position_count >= limits.max_open_positions:
        return (
            False,
            "EXPOSURE_MAX_POSITIONS",
            f"Open positions ({snapshot.open_position_count}) >= limit ({limits.max_open_positions})",
        )

    # Max per-underlying notional
    if new_order_underlying:
        ul = new_order_underlying.upper()
        current = snapshot.per_underlying_notional.get(ul, 0.0)
        projected = current + new_order_notional
        if projected > limits.max_per_underlying_notional:
            return (
                False,
                "EXPOSURE_MAX_NOTIONAL",
                f"{ul} notional ({projected:,.0f}) > limit ({limits.max_per_underlying_notional:,.0f})",
            )

    # Max short options legs
    if is_new_short_option:
        if snapshot.short_options_legs >= limits.max_short_options_legs:
            return (
                False,
                "EXPOSURE_MAX_SHORT_OPTIONS",
                f"Short option legs ({snapshot.short_options_legs}) >= limit ({limits.max_short_options_legs})",
            )

    return True, None, None


__all__ = [
    "ExposureLimits",
    "ExposureSnapshot",
    "compute_exposure_snapshot",
    "check_exposure_limits",
]
