"""
Thread-safe in-memory switchboard for enabling or disabling strategies.
Used by admin endpoints and runtime checks.
"""

from __future__ import annotations

import threading
from typing import Dict

from app.strategies.naming import canonicalize_strategy_name


class StrategySwitchboard:
    """In-memory registry of strategy enable flags with thread safety."""

    # Initialize the switchboard with optional starting flags.
    def __init__(self, initial: Dict[str, bool] | None = None) -> None:
        self._lock = threading.Lock()
        self._flags: Dict[str, bool] = {}
        for key, enabled in (initial or {}).items():
            canonical = canonicalize_strategy_name(
                str(key or "").strip(),
                source="strategy_switch.init",
                warn_alias=False,
                preserve_unknown=True,
            )
            if not canonical:
                continue
            self._flags[canonical] = bool(enabled)

    # Check whether a strategy is enabled.
    def is_enabled(self, name: str) -> bool:
        canonical = canonicalize_strategy_name(
            str(name or "").strip(),
            source="strategy_switch.is_enabled",
            warn_alias=False,
            preserve_unknown=True,
        )
        if not canonical:
            return True
        with self._lock:
            return self._flags.get(canonical, True)

    # Enable or disable a strategy by name.
    def set_enabled(self, name: str, enabled: bool) -> None:
        canonical = canonicalize_strategy_name(
            str(name or "").strip(),
            source="strategy_switch.set_enabled",
            warn_alias=False,
            preserve_unknown=True,
        )
        if not canonical:
            raise ValueError("Strategy name cannot be empty or None")
        with self._lock:
            self._flags[canonical] = bool(enabled)

    # Return a snapshot copy of all strategy flags.
    def snapshot(self) -> Dict[str, bool]:
        with self._lock:
            return dict(self._flags)
