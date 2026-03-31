from __future__ import annotations

import re
from typing import Any

_SYMBOL_PREFIX_PATTERN = re.compile(r"^(?P<underlying>[A-Z]+)\d{1,2}[A-Z]{3}\d{2}")
_UNDERLYING_ALIASES = {
    "NATURALGAS": "NG",
    "NATURALGASFUT": "NG",
    "NIFTY50": "NIFTY",
    "NIFTYBANK": "BANKNIFTY",
    "NIFTYFINSERVICES": "FINNIFTY",
    "NIFTYFINSERVICE": "FINNIFTY",
    "NIFTYMIDSELECT": "MIDCPNIFTY",
}

_UNDERLYING_LOOKUP_ALIASES = {
    "NG": ("NATURALGAS",),
    "NIFTY": ("NIFTY50",),
    "BANKNIFTY": ("NIFTYBANK",),
    "FINNIFTY": ("NIFTYFINSERVICES", "NIFTYFINSERVICE"),
    "MIDCPNIFTY": ("NIFTYMIDSELECT",),
}


def canonical_underlying_key(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    clean = re.sub(r"[^A-Z0-9]", "", text)
    if not clean:
        return ""
    symbol_match = _SYMBOL_PREFIX_PATTERN.match(clean)
    if symbol_match:
        clean = symbol_match.group("underlying")
    for suffix in ("IDX", "FUT"):
        if clean.endswith(suffix) and len(clean) > len(suffix):
            clean = clean[: -len(suffix)]
            break
    return _UNDERLYING_ALIASES.get(clean, clean)


def underlying_lookup_candidates(value: Any) -> tuple[str, ...]:
    canonical = canonical_underlying_key(value)
    if not canonical:
        return tuple()
    aliases = _UNDERLYING_LOOKUP_ALIASES.get(canonical, ())
    return tuple(dict.fromkeys((canonical, *aliases)))


__all__ = ["canonical_underlying_key", "underlying_lookup_candidates"]
