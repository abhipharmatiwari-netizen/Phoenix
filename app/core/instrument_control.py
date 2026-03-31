"""
In-memory controls for enabling instruments and whitelisting strategies.
Used by admin endpoints to gate trading per underlying.
"""

from __future__ import annotations

import threading
from typing import Dict, List, Optional, Set

from app.strategies.naming import canonicalize_strategy_name, canonicalize_strategy_names


class InstrumentController:
    """
    In-memory controller for instrument enable flags and per-instrument allowed strategies.
    """

    # Initialize controller state from an optional snapshot.
    def __init__(self, initial: Optional[Dict[str, dict]] = None) -> None:
        self._lock = threading.Lock()
        self._state: Dict[str, dict] = {}
        for key, cfg in (initial or {}).items():
            allowed_raw = cfg.get("allowed_strategies", []) or []
            allowed = canonicalize_strategy_names(
                [str(x) for x in allowed_raw],
                source=f"instrument_control.init.{str(key).upper()}",
                warn_alias=False,
                preserve_unknown=True,
            )
            self._state[str(key).upper()] = {
                "enabled": bool(cfg.get("enabled", True)),
                "allowed_strategies": set(allowed),
            }

    # Return whether an underlying is enabled for trading.
    def is_enabled(self, underlying_label: str) -> bool:
        with self._lock:
            return self._state.get(str(underlying_label).upper(), {}).get(
                "enabled", True
            )

    # Set enable/disable flag for a given underlying.
    def set_enabled(self, underlying_label: str, enabled: bool) -> None:
        key = str(underlying_label).upper()
        with self._lock:
            entry = self._state.setdefault(
                key, {"enabled": True, "allowed_strategies": set()}
            )
            entry["enabled"] = bool(enabled)

    # Update the allowed strategies list for an underlying.
    def update_allowed_strategies(
        self, underlying_label: str, allowed: Optional[List[str] | Set[str]]
    ) -> None:
        key = str(underlying_label).upper()
        canonical = canonicalize_strategy_names(
            [str(x) for x in (allowed or [])],
            source=f"instrument_control.update.{key}",
            warn_alias=False,
            preserve_unknown=True,
        )
        with self._lock:
            entry = self._state.setdefault(
                key, {"enabled": True, "allowed_strategies": set()}
            )
            entry["allowed_strategies"] = set(canonical)

    # Check if a strategy is allowed for an underlying.
    def is_strategy_allowed(self, underlying_label: str, strategy_name: str) -> bool:
        key = str(underlying_label).upper()
        canonical_name = canonicalize_strategy_name(
            str(strategy_name or "").strip(),
            source=f"instrument_control.is_strategy_allowed.{key}",
            warn_alias=False,
            preserve_unknown=True,
        )
        if not canonical_name:
            return False
        with self._lock:
            entry = self._state.get(key)
            if entry is None:
                return True
            allowed = entry.get("allowed_strategies") or set()
            # If list is empty, allow all; otherwise enforce membership.
            if not allowed:
                return True
            return canonical_name in allowed

    # Return a snapshot of current instrument enablement state.
    def snapshot(self) -> Dict[str, dict]:
        with self._lock:
            return {
                k: {
                    "enabled": v.get("enabled", True),
                    "allowed_strategies": sorted(v.get("allowed_strategies", set())),
                }
                for k, v in self._state.items()
            }
