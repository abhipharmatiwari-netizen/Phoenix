"""Strategy naming contract with canonical validation and legacy alias support."""

from __future__ import annotations

import logging
from threading import Lock
from typing import Iterable

from app.strategies.migration import LEGACY_TO_CANONICAL
from app.strategies.registry import CANONICAL_STRATEGY_NAMES

logger = logging.getLogger(__name__)

_EXPLICIT_ALIAS_TO_CANONICAL = {
    "options_generic": "option_strategy",
    "delta": "delta_strangle",
    "ema20": "ema20_strategy",
    "ema_20_strategy": "ema20_strategy",
}
STRATEGY_ALIASES: dict[str, str] = {
    **{str(alias).strip().lower(): canonical for alias, canonical in LEGACY_TO_CANONICAL.items()},
    **_EXPLICIT_ALIAS_TO_CANONICAL,
}
_ALIAS_USAGE_LOCK = Lock()
_ALIAS_USAGE: dict[str, int] = {}


def _normalize_name(name: str) -> str:
    return str(name or "").strip().lower()


def _record_alias_usage(alias: str, canonical: str) -> None:
    if not alias or alias == canonical:
        return
    key = f"{alias}->{canonical}"
    with _ALIAS_USAGE_LOCK:
        _ALIAS_USAGE[key] = int(_ALIAS_USAGE.get(key, 0)) + 1


def validate_canonical_name(name: str) -> bool:
    """
    Validate that a name is a recognized strategy identifier.
    
    Returns:
        True if name is canonical or a recognized alias, False otherwise.
        
    Raises:
        ValueError: Never. Always returns bool for compatibility with existing code.
    """
    name_str = _normalize_name(name)
    return (
        name_str in CANONICAL_STRATEGY_NAMES
        or name_str in STRATEGY_ALIASES
    )


def validate_canonical_name_strict(name: str) -> None:
    """
    Strictly validate a strategy name. Raises on invalid names.
    
    Args:
        name: Strategy name to validate.
        
    Raises:
        ValueError: If name is not a canonical strategy name.
    """
    name_str = _normalize_name(name)
    if not name_str:
        raise ValueError("Strategy name cannot be empty or None")
    if name_str not in CANONICAL_STRATEGY_NAMES:
        raise ValueError(
            f"Invalid strategy name '{name_str}'. "
            f"Must be one of: {sorted(CANONICAL_STRATEGY_NAMES)}"
        )


def all_canonical_strategy_names() -> set[str]:
    """Return set of all authorized canonical strategy names."""
    return set(CANONICAL_STRATEGY_NAMES)


def is_strategy_alias(name: str) -> bool:
    """Return True when the provided name is a recognized non-canonical alias."""
    name_str = _normalize_name(name)
    canonical = STRATEGY_ALIASES.get(name_str)
    return bool(canonical and canonical != name_str)


def strategy_alias_usage_snapshot() -> dict[str, int]:
    """Return a copy of the in-memory alias usage counters."""
    with _ALIAS_USAGE_LOCK:
        return dict(sorted(_ALIAS_USAGE.items()))


def reset_strategy_alias_usage() -> None:
    """Clear alias usage counters (used by tests and diagnostics)."""
    with _ALIAS_USAGE_LOCK:
        _ALIAS_USAGE.clear()


def canonicalize_strategy_name(
    name: str,
    *,
    source: str = "unknown",
    warn_alias: bool = True,
    preserve_unknown: bool = False,
) -> str:
    """
    Return the canonical name if valid.

    Recognized aliases are converted to canonical names. Unknown names return ""
    unless preserve_unknown=True, in which case the normalized input is returned.
    """
    name_str = _normalize_name(name)
    if not name_str:
        return ""
    if name_str in CANONICAL_STRATEGY_NAMES:
        return name_str
    canonical = STRATEGY_ALIASES.get(name_str)
    if canonical:
        _record_alias_usage(name_str, canonical)
        if warn_alias:
            logger.info(
                "canonicalize_strategy_name: alias '%s' from %s -> '%s'",
                name_str,
                source,
                canonical,
            )
        return canonical
    if preserve_unknown:
        return name_str
    logger.warning(
        "canonicalize_strategy_name: Unknown strategy name '%s' from %s",
        name_str,
        source,
    )
    return ""


def canonicalize_strategy_names(
    names: Iterable[str] | None,
    *,
    source: str = "unknown",
    warn_alias: bool = True,
    preserve_unknown: bool = False,
) -> list[str]:
    """
    Return deduplicated canonical names, optionally preserving unknown entries.
    """
    out: list[str] = []
    seen: set[str] = set()
    for name in names or []:
        canonical = canonicalize_strategy_name(
            str(name or ""),
            source=source,
            warn_alias=warn_alias,
            preserve_unknown=preserve_unknown,
        )
        if canonical and canonical not in seen:
            seen.add(canonical)
            out.append(canonical)
    return out


__all__ = [
    "CANONICAL_STRATEGY_NAMES",
    "STRATEGY_ALIASES",
    "all_canonical_strategy_names",
    "canonicalize_strategy_name",
    "canonicalize_strategy_names",
    "is_strategy_alias",
    "reset_strategy_alias_usage",
    "strategy_alias_usage_snapshot",
    "validate_canonical_name",
    "validate_canonical_name_strict",
]
