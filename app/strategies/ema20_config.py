from __future__ import annotations

from dataclasses import dataclass
from datetime import time as dt_time
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

import yaml

from app.core.underlyings import canonical_underlying_key
from app.strategies.adaptive.dynamic_policy import (
    DynamicPolicyConfig,
    parse_dynamic_policy_config,
)
from app.strategies.naming import canonicalize_strategy_name
EMA20_STRATEGY_NAMES = {"ema20_strategy"}
_UNDERLYING_SECTION_MAP = {
    "NG": "ng",
    "NIFTY": "nifty",
    "BANKNIFTY": "banknifty",
    "FINNIFTY": "finnifty",
    "SENSEX": "sensex",
    "MIDCPNIFTY": "midcpnifty",
}


@dataclass(frozen=True)
class Ema20Config:
    underlying_label: str
    signal_timeframe: int
    ema_period: int
    min_atr: Optional[float]
    require_rsi_falling: bool
    use_adx_filter: bool
    adx_period: int
    min_adx: float
    require_bearish_di: bool
    min_di_spread: float
    sl_pct: float
    tp_pct: float
    trail_buffer_pct: float
    trail_trigger_pct: Optional[float]
    first_entry_time: Optional[dt_time]
    square_off_time: Optional[dt_time]
    hard_sl_points: Optional[float]
    skip_entry_hours_ist: Tuple[int, ...]
    min_ema_gap_pct: float
    dynamic_policy: DynamicPolicyConfig
    # PHX#182: partial profit booking at first target.
    tp1_pct: Optional[float] = None
    tp1_qty_pct: float = 0.0
    # PHX#183: give-back guardrail — exit if profit retraces from peak.
    giveback_pct: Optional[float] = None
    giveback_arm_pct: Optional[float] = None
    # PHX#184: time-decay accelerator — tighten exits near square-off.
    decay_tighten_minutes_before_eod: Optional[int] = None
    decay_tp_multiplier: float = 1.0
    decay_trail_buffer_multiplier: float = 1.0

    def first_entry_time_hhmm(self) -> Optional[str]:
        return self.first_entry_time.strftime("%H:%M") if self.first_entry_time else None

    def square_off_time_hhmm(self) -> Optional[str]:
        return self.square_off_time.strftime("%H:%M") if self.square_off_time else None


def _parse_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    txt = str(value).strip().lower()
    if txt in {"1", "true", "yes", "y", "on"}:
        return True
    if txt in {"0", "false", "no", "n", "off"}:
        return False
    return bool(default)


def parse_hhmm(value: Any, *, strict: bool = False, field_name: str = "time") -> Optional[dt_time]:
    if value in (None, ""):
        return None
    try:
        txt = str(value).strip()
        hh_s, mm_s = txt.split(":", 1)
        hh = int(hh_s)
        mm = int(mm_s)
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError(f"{field_name} out of range: {txt}")
        return dt_time(hour=hh, minute=mm)
    except Exception as exc:
        if strict:
            raise ValueError(f"Invalid {field_name}: {value}") from exc
        return None


def parse_hour_list(value: Any, *, strict: bool = False, field_name: str = "skip_entry_hours_ist") -> Tuple[int, ...]:
    if value in (None, ""):
        return tuple()
    if isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [x.strip() for x in str(value).split(",")]
    out: set[int] = set()
    for item in items:
        txt = str(item).strip()
        if not txt:
            continue
        try:
            hh = int(txt)
        except Exception as exc:
            if strict:
                raise ValueError(f"Invalid {field_name} value: {item}") from exc
            continue
        if not (0 <= hh <= 23):
            if strict:
                raise ValueError(f"{field_name} hour out of range: {hh}")
            continue
        out.add(hh)
    return tuple(sorted(out))


def _to_int(value: Any, *, default: int, strict: bool, field_name: str, min_value: Optional[int] = None) -> int:
    if value in (None, ""):
        out = int(default)
    else:
        try:
            out = int(value)
        except Exception as exc:
            if strict:
                raise ValueError(f"Invalid {field_name}: {value}") from exc
            out = int(default)
    if min_value is not None and out < min_value:
        if strict:
            raise ValueError(f"{field_name} must be >= {min_value}, got {out}")
        out = max(out, min_value)
    return out


def _to_float(
    value: Any,
    *,
    default: Optional[float],
    strict: bool,
    field_name: str,
    min_value: Optional[float] = None,
) -> Optional[float]:
    if value in (None, ""):
        out = default
    else:
        try:
            out = float(value)
        except Exception as exc:
            if strict:
                raise ValueError(f"Invalid {field_name}: {value}") from exc
            out = default
    if out is not None and min_value is not None and out < min_value:
        if strict:
            raise ValueError(f"{field_name} must be >= {min_value}, got {out}")
        out = max(out, min_value)
    return out


def _iter_instruments(items: Any) -> Iterable[Dict[str, Any]]:
    for item in items or []:
        if isinstance(item, str):
            yield {"label": item}
            continue
        if not isinstance(item, dict):
            continue
        label = item.get("label") or item.get("underlying") or item.get("instrument")
        if not label:
            continue
        out = dict(item)
        out["label"] = str(label)
        yield out


def _normalize_strategy_entries(entries: Any) -> list[Dict[str, Any]]:
    if isinstance(entries, list):
        return [e for e in entries if isinstance(e, dict)]
    if isinstance(entries, dict):
        out: list[Dict[str, Any]] = []
        for name, cfg in entries.items():
            if not isinstance(cfg, dict):
                continue
            row = dict(cfg)
            row.setdefault("name", name)
            out.append(row)
        return out
    return []


def _section_for_underlying(raw: Mapping[str, Any], underlying_label: str) -> Mapping[str, Any]:
    root = canonical_underlying_key(underlying_label)
    section_key = _UNDERLYING_SECTION_MAP.get(root)
    if not section_key:
        return {}
    section = raw.get(section_key, {})
    return section if isinstance(section, dict) else {}


def find_ema20_params_for_underlying(raw: Mapping[str, Any], underlying_label: str) -> Dict[str, Any]:
    entries = _normalize_strategy_entries(raw.get("strategies", []))
    for row in entries:
        name = canonicalize_strategy_name(
            str(row.get("name", "")).strip(),
            source="ema20_config.find_ema20_params",
        )
        if name not in EMA20_STRATEGY_NAMES:
            continue
        if not bool(row.get("enabled", True)):
            continue
        base_params = row.get("params", {}) if isinstance(row.get("params", {}), dict) else {}
        for inst in _iter_instruments(row.get("instruments", [])):
            if str(inst.get("label")) != str(underlying_label):
                continue
            inst_specific = {k: v for k, v in inst.items() if k != "label"}
            merged = dict(base_params)
            merged.update(inst_specific)
            return merged
    return {}


def build_ema20_config(
    *,
    underlying_label: str,
    raw: Mapping[str, Any],
    params: Optional[Mapping[str, Any]] = None,
    strict: bool = False,
) -> Ema20Config:
    section = _section_for_underlying(raw, underlying_label)
    param_values = dict(params) if params is not None else find_ema20_params_for_underlying(raw, underlying_label)

    def _pick(key: str, *fallbacks: str) -> Any:
        if key in param_values:
            return param_values.get(key)
        for fb in fallbacks:
            if fb in param_values:
                return param_values.get(fb)
        if key in section:
            return section.get(key)
        for fb in fallbacks:
            if fb in section:
                return section.get(fb)
        return None

    cfg = Ema20Config(
        underlying_label=str(underlying_label),
        signal_timeframe=_to_int(
            _pick("timeframe_seconds", "signal_timeframe"),
            default=300,
            strict=strict,
            field_name="signal_timeframe",
            min_value=1,
        ),
        ema_period=_to_int(
            _pick("ema_period"),
            default=20,
            strict=strict,
            field_name="ema_period",
            min_value=2,
        ),
        min_atr=_to_float(
            _pick("min_atr"),
            default=None,
            strict=strict,
            field_name="min_atr",
            min_value=0.0,
        ),
        require_rsi_falling=_parse_bool(
            _pick("require_rsi_falling", "ema20_require_rsi_falling"),
            default=bool(section.get("ema20_require_rsi_falling", True)),
        ),
        use_adx_filter=_parse_bool(
            _pick("use_adx_filter", "ema20_use_adx_filter"),
            default=bool(section.get("ema20_use_adx_filter", False)),
        ),
        adx_period=_to_int(
            _pick("adx_period", "ema20_adx_period"),
            default=14,
            strict=strict,
            field_name="adx_period",
            min_value=2,
        ),
        min_adx=float(
            _to_float(
                _pick("min_adx", "ema20_min_adx"),
                default=18.0,
                strict=strict,
                field_name="min_adx",
                min_value=0.0,
            )
        ),
        require_bearish_di=_parse_bool(
            _pick("require_bearish_di", "ema20_require_bearish_di"),
            default=bool(section.get("ema20_require_bearish_di", True)),
        ),
        min_di_spread=float(
            _to_float(
                _pick("min_di_spread", "ema20_min_di_spread"),
                default=0.0,
                strict=strict,
                field_name="min_di_spread",
                min_value=0.0,
            )
        ),
        sl_pct=float(
            _to_float(
                _pick("sl_pct"),
                default=0.30,
                strict=strict,
                field_name="sl_pct",
                min_value=0.0,
            )
        ),
        tp_pct=float(
            _to_float(
                _pick("tp_pct"),
                default=0.30,
                strict=strict,
                field_name="tp_pct",
                min_value=0.0,
            )
        ),
        trail_buffer_pct=float(
            _to_float(
                _pick("trail_buffer_pct"),
                default=0.0,
                strict=strict,
                field_name="trail_buffer_pct",
                min_value=0.0,
            )
        ),
        trail_trigger_pct=_to_float(
            _pick("trail_trigger_pct"),
            default=None,
            strict=strict,
            field_name="trail_trigger_pct",
            min_value=0.0,
        ),
        first_entry_time=parse_hhmm(
            _pick("first_entry_time", "ema20_first_entry_time"),
            strict=strict,
            field_name="first_entry_time",
        ),
        square_off_time=parse_hhmm(
            _pick("square_off_time", "ema20_square_off_time"),
            strict=strict,
            field_name="square_off_time",
        ),
        hard_sl_points=_to_float(
            _pick("hard_sl_points", "ema20_hard_sl_points"),
            default=None,
            strict=strict,
            field_name="hard_sl_points",
            min_value=0.0,
        ),
        skip_entry_hours_ist=parse_hour_list(
            _pick("skip_entry_hours_ist", "ema20_skip_entry_hours_ist"),
            strict=strict,
            field_name="skip_entry_hours_ist",
        ),
        min_ema_gap_pct=float(
            _to_float(
                _pick("min_ema_gap_pct", "ema20_min_ema_gap_pct"),
                default=0.0,
                strict=strict,
                field_name="min_ema_gap_pct",
                min_value=0.0,
            )
        ),
        dynamic_policy=parse_dynamic_policy_config(
            _pick("dynamic_policy") or {},
            strict=strict,
        ),
        # PHX#182: partial booking at first target.
        tp1_pct=_to_float(
            _pick("tp1_pct"),
            default=None,
            strict=strict,
            field_name="tp1_pct",
            min_value=0.0,
        ),
        tp1_qty_pct=float(
            _to_float(
                _pick("tp1_qty_pct"),
                default=0.0,
                strict=strict,
                field_name="tp1_qty_pct",
                min_value=0.0,
            )
        ),
        # PHX#183: give-back guardrail.
        giveback_pct=_to_float(
            _pick("giveback_pct"),
            default=None,
            strict=strict,
            field_name="giveback_pct",
            min_value=0.0,
        ),
        giveback_arm_pct=_to_float(
            _pick("giveback_arm_pct"),
            default=None,
            strict=strict,
            field_name="giveback_arm_pct",
            min_value=0.0,
        ),
        # PHX#184: time-decay accelerator.
        decay_tighten_minutes_before_eod=_to_int(
            _pick("decay_tighten_minutes_before_eod"),
            default=0,
            strict=strict,
            field_name="decay_tighten_minutes_before_eod",
            min_value=0,
        ) or None,
        decay_tp_multiplier=float(
            _to_float(
                _pick("decay_tp_multiplier"),
                default=1.0,
                strict=strict,
                field_name="decay_tp_multiplier",
                min_value=0.0,
            )
        ),
        decay_trail_buffer_multiplier=float(
            _to_float(
                _pick("decay_trail_buffer_multiplier"),
                default=1.0,
                strict=strict,
                field_name="decay_trail_buffer_multiplier",
                min_value=0.0,
            )
        ),
    )

    if strict and cfg.first_entry_time and cfg.square_off_time:
        if cfg.first_entry_time >= cfg.square_off_time:
            raise ValueError(
                f"Invalid EMA20 times for {cfg.underlying_label}: first_entry_time "
                f"{cfg.first_entry_time.strftime('%H:%M')} must be before square_off_time "
                f"{cfg.square_off_time.strftime('%H:%M')}"
            )

    return cfg


def load_ema20_config_from_yaml(path: Path, *, underlying_label: str, strict: bool = False) -> Ema20Config:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raw = {}
    return build_ema20_config(underlying_label=underlying_label, raw=raw, strict=strict)
