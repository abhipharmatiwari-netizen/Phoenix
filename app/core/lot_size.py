"""
Helpers for resolving lot sizes from the universe configuration.
Provides cached pattern matching for symbol-to-lot conversions.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from app.core.strategy_config_loader import load_universe_from_yaml

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "universe.yaml"


# Load and cache symbol patterns to lot sizes from the universe config.
@lru_cache(maxsize=1)
def _lot_patterns() -> list[tuple[str, int]]:
    try:
        raw = load_universe_from_yaml(_CONFIG_PATH)
    except ValueError:
        return []
    patterns: list[tuple[str, int]] = []
    for entry in raw.get("underlyings") or []:
        pattern = entry.get("symbol_pattern") or entry.get("name")
        lot_size = entry.get("lot_size")
        if not pattern or lot_size is None:
            continue
        try:
            patterns.append((str(pattern).upper(), int(lot_size)))
        except (TypeError, ValueError):
            continue
    patterns.sort(key=lambda item: len(item[0]), reverse=True)
    return patterns


# Return the lot size for a given trading symbol.
def lot_size_for_symbol(symbol: str) -> int:
    sym = str(symbol or "").upper()
    if not sym:
        return 1
    for pattern, lot_size in _lot_patterns():
        if pattern and pattern in sym:
            return lot_size
    return 1


# Return the lot size for a symbol if a pattern matches; otherwise None.
def lot_size_for_symbol_optional(symbol: str) -> int | None:
    sym = str(symbol or "").upper()
    if not sym:
        return None
    for pattern, lot_size in _lot_patterns():
        if pattern and pattern in sym:
            return lot_size
    return None


# Convert a quantity to lots using the symbol's lot size.
def qty_to_lots(qty: int | float, symbol: str) -> float:
    lot_size = lot_size_for_symbol(symbol)
    if lot_size <= 0:
        return float(qty)
    return float(qty) / float(lot_size)
