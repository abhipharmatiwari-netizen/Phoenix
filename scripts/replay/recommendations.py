"""Replay recommendation helpers and strategy_env.yaml extraction."""

from __future__ import annotations

import copy
import json
from dataclasses import asdict, is_dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

import yaml

from scripts.replay.replay_engine import UNDERLYING_MAP

DEFAULT_STRATEGY_ENV_PATH = Path("app/config/strategy_env.yaml")


def load_strategy_defaults(
    *,
    strategy_id: str,
    underlying_key: str,
    fallback: Optional[Mapping[str, Any]] = None,
    config_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Resolve replay defaults from strategy_env.yaml when possible."""

    normalized_path = str(config_path or DEFAULT_STRATEGY_ENV_PATH)
    raw = _read_strategy_env(normalized_path)
    target_label = UNDERLYING_MAP[underlying_key]["underlying_label"]
    strategies = raw.get("strategies") or []

    for entry in strategies:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("name")) != str(strategy_id):
            continue
        params = dict(entry.get("params") or {})
        instruments = entry.get("instruments") or []
        if _instrument_match(instruments, target_label):
            overrides = _instrument_overrides(instruments, target_label)
            return {**dict(fallback or {}), **params, **overrides}

    return dict(fallback or {})


def build_yaml_patch_suggestions(
    *,
    strategy_env_path: str,
    recommendation_payloads: Mapping[str, Dict[str, Any]],
) -> str:
    """Render replay recommendations as a review-only YAML suggestions file."""

    suggestions = []
    for combo_key, payload in sorted(recommendation_payloads.items()):
        suggestions.append(
            {
                "combo": combo_key,
                "strategy": payload.get("strategy_id"),
                "underlying": payload.get("underlying_key"),
                "underlying_label": payload.get("underlying_label"),
                "current_config_params": payload.get("current_config_params") or {},
                "recommended_params": payload.get("recommended_params") or {},
                "evidence": payload.get("evidence") or {},
                "warnings": payload.get("warnings") or [],
                "review_only": True,
            }
        )
    return yaml.safe_dump(
        {
            "strategy_env_path": strategy_env_path,
            "auto_apply": False,
            "suggestions": suggestions,
        },
        sort_keys=False,
    )


def to_jsonable(value: Any) -> Any:
    """Convert nested replay data into a JSON-serializable structure."""

    if is_dataclass(value):
        return to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): to_jsonable(val) for key, val in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


@lru_cache(maxsize=4)
def _read_strategy_env(config_path: str) -> Dict[str, Any]:
    path = Path(config_path)
    if not path.exists():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if isinstance(loaded, dict):
        return copy.deepcopy(loaded)
    return {}


def _instrument_match(instruments: Iterable[Any], target_label: str) -> bool:
    for item in instruments:
        if isinstance(item, str) and item == target_label:
            return True
        if isinstance(item, dict) and str(item.get("label")) == target_label:
            return True
    return False


def _instrument_overrides(instruments: Iterable[Any], target_label: str) -> Dict[str, Any]:
    for item in instruments:
        if isinstance(item, dict) and str(item.get("label")) == target_label:
            return {
                str(key): value
                for key, value in item.items()
                if str(key) != "label"
            }
    return {}


__all__ = [
    "DEFAULT_STRATEGY_ENV_PATH",
    "build_yaml_patch_suggestions",
    "load_strategy_defaults",
    "to_jsonable",
]
