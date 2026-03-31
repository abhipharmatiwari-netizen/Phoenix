"""
Strategy package exports for the legacy single-tenant runner.
Provides access to all strategy classes in one place.
"""

from __future__ import annotations

from importlib import import_module
from typing import Dict, Tuple

__all__ = [
    "OptionStrategy",
    "DeltaStrangleStrategy",
    "Ema20Strategy",
    "CeOrbStrategy",
    "CePdhRangeBreakoutStrategy",
    "PutMomentumScalperStrategy",
    "PutReversionWriterStrategy",
    "NiftyWeeklyCreditSpreadStrategy",
    "ExclusiveNiftyCeBuyStrategy",
    "PutBuyStrategy",
    "PutBuyLiveStrategy",
]

_LAZY_EXPORTS: Dict[str, Tuple[str, str]] = {
    "OptionStrategy": (".option_strategy", "OptionStrategy"),
    "DeltaStrangleStrategy": (".delta_strangle", "DeltaStrangleStrategy"),
    "Ema20Strategy": (".ema20_strategy", "Ema20Strategy"),
    "CeOrbStrategy": (".ce_orb", "CeOrbStrategy"),
    "CePdhRangeBreakoutStrategy": (".ce_pdh_rb", "CePdhRangeBreakoutStrategy"),
    "PutMomentumScalperStrategy": (
        ".put_momentum_scalper",
        "PutMomentumScalperStrategy",
    ),
    "PutReversionWriterStrategy": (
        ".put_reversion_writer",
        "PutReversionWriterStrategy",
    ),
    "NiftyWeeklyCreditSpreadStrategy": (
        ".nifty_weekly_credit_spreads",
        "NiftyWeeklyCreditSpreadStrategy",
    ),
    "ExclusiveNiftyCeBuyStrategy": (
        ".exclusive_nifty_ce_buy",
        "ExclusiveNiftyCeBuyStrategy",
    ),
    "PutBuyStrategy": (".strategy_put_buy", "PutBuyStrategy"),
    "PutBuyLiveStrategy": (".put_buy_live", "PutBuyLiveStrategy"),
}


def __getattr__(name: str):
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, symbol_name = target
    module = import_module(module_name, __name__)
    value = getattr(module, symbol_name)
    globals()[name] = value
    return value
