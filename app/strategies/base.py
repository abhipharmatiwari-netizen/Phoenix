"""
Protocol definitions for strategy interfaces.
Strategies implement on_tick and on_bar hooks.
"""

from __future__ import annotations

from typing import Protocol, Any, Dict


# Common interface for strategy implementations.
class BaseStrategy(Protocol):
    def on_tick(self, label: str, ltp: float) -> None: ...

    def on_bar(
        self,
        label: str,
        timeframe_seconds: int,
        candle: Any,
        indicators: Dict[str, Any],
    ) -> None: ...

    def on_position_flat_by_sync(self, *, label: str, reason: str) -> None:
        """Called by POS_SYNC when STRONG-evidence flat confirmed. No-op default."""
        ...
