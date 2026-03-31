"""Helper functions for strategy entry normalization in the legacy stream runner."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def normalize_strategy_entries(entries: Any) -> list[dict[str, Any]]:
    if isinstance(entries, list):
        return [entry for entry in entries if isinstance(entry, dict)]
    if isinstance(entries, dict):
        output: list[dict[str, Any]] = []
        for name, cfg in entries.items():
            if not isinstance(cfg, dict):
                continue
            row = dict(cfg)
            row.setdefault("name", name)
            output.append(row)
        return output
    return []


def iter_instruments(items: Any):
    for item in items or []:
        if isinstance(item, str):
            yield {"label": item}
            continue
        if not isinstance(item, dict):
            continue
        label = item.get("label") or item.get("underlying") or item.get("instrument")
        if not label:
            continue
        entry = dict(item)
        entry["label"] = label
        yield entry


def resolve_delta_qty_config(strategy_cfg: dict[str, Any]) -> tuple[int, dict[str, int]]:
    delta_cfg = strategy_cfg.get("delta", {}) if isinstance(strategy_cfg, dict) else {}
    raw_delta_qty = (
        delta_cfg.get("delta_qty_per_leg", 1) if isinstance(delta_cfg, dict) else 1
    )
    try:
        delta_qty_per_leg = max(1, int(raw_delta_qty))
    except (TypeError, ValueError):
        logger.warning("Invalid delta_qty_per_leg=%s, defaulting to 1", raw_delta_qty)
        delta_qty_per_leg = 1

    delta_qty_per_leg_by_underlying: dict[str, int] = {}
    if isinstance(delta_cfg, dict):
        raw_delta_map = delta_cfg.get("delta_qty_per_leg_by_underlying") or {}
        if isinstance(raw_delta_map, dict):
            for key, val in raw_delta_map.items():
                try:
                    qty_val = int(val)
                except (TypeError, ValueError):
                    logger.warning("Invalid delta qty override for %s: %s", key, val)
                    continue
                if qty_val <= 0:
                    continue
                delta_qty_per_leg_by_underlying[str(key).upper()] = qty_val
    return delta_qty_per_leg, delta_qty_per_leg_by_underlying


__all__ = [
    "normalize_strategy_entries",
    "iter_instruments",
    "resolve_delta_qty_config",
]
