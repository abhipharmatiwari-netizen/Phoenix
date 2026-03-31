from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, Optional

from app.strategies.naming import canonicalize_strategy_name


_RECOVERY_POSITION_MARKERS = frozenset(
    {
        "BROKER_SYNC",
        "POSITION_SYNC",
        "RECOVERY",
        "MANUAL_RECOVERY",
        "UNKNOWN",
        "__UNKNOWN__",
    }
)


def parse_datetime(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_positive_int(value: Any) -> Optional[int]:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def parse_non_negative_float(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out < 0.0:
        return None
    return out


def parse_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return bool(default)


def is_recovery_position_marker(value: Any) -> bool:
    marker = str(value or "").strip().upper()
    return bool(marker) and marker in _RECOVERY_POSITION_MARKERS


def find_restored_position(
    *,
    risk_manager: Any,
    instrument_meta: Dict[str, Dict[str, Any]],
    strategy_id: str,
    canonical_strategy_name: str,
    underlying_candidates: Iterable[str],
    side: str,
    extra_match: Optional[Callable[[str, Dict[str, Any], Dict[str, Any]], bool]] = None,
) -> Optional[tuple[str, Dict[str, Any]]]:
    restored_positions = getattr(risk_manager, "restored_positions", None)
    if not isinstance(restored_positions, dict) or not restored_positions:
        return None

    normalized_underlyings = {
        str(item or "").strip().upper() for item in underlying_candidates if str(item or "").strip()
    }
    candidates: list[tuple[datetime, str, Dict[str, Any]]] = []
    for raw_label, raw_entry in restored_positions.items():
        label = str(raw_label or "").strip()
        if not label or not isinstance(raw_entry, dict):
            continue
        if label not in instrument_meta:
            continue
        entry = dict(raw_entry)
        if str(entry.get("side") or "").strip().upper() != str(side or "").strip().upper():
            continue
        if parse_positive_int(entry.get("qty")) is None:
            continue

        strategy_names = (
            str(entry.get("strategy_name") or "").strip(),
            str(entry.get("template_name") or "").strip(),
        )
        strategy_match = False
        for raw_name in strategy_names:
            if not raw_name:
                continue
            if is_recovery_position_marker(raw_name):
                continue
            if raw_name == strategy_id:
                strategy_match = True
                break
            if (
                canonicalize_strategy_name(
                    raw_name,
                    source="restart_state.restore",
                    warn_alias=False,
                )
                == canonical_strategy_name
            ):
                strategy_match = True
                break
        if not strategy_match:
            continue

        meta = instrument_meta.get(label, {})
        restored_underlying = str(
            entry.get("underlying") or meta.get("underlying") or ""
        ).strip().upper()
        if normalized_underlyings and restored_underlying and restored_underlying not in normalized_underlyings:
            continue
        if extra_match is not None and not extra_match(label, entry, meta):
            continue

        entry_ts = parse_datetime(entry.get("entry_ts")) or datetime.now(timezone.utc)
        candidates.append((entry_ts, label, entry))

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    _entry_ts, label, entry = candidates[0]
    return label, entry


def sync_open_position_state(
    *,
    risk_manager: Any,
    label: str,
    side: str,
    qty: int,
    entry_price: float,
    strategy_context: Dict[str, Any],
) -> None:
    if risk_manager is None or not label:
        return

    existing = {}
    open_positions = getattr(risk_manager, "open_positions", None)
    if isinstance(open_positions, dict):
        existing = dict(open_positions.get(label, {}))

    if hasattr(risk_manager, "update_position_from_broker"):
        risk_manager.update_position_from_broker(
            label=label,
            side=side,
            qty=qty,
            entry_price=entry_price,
            exchange=existing.get("exchange"),
            symboltoken=existing.get("symboltoken"),
            tradingsymbol=existing.get("tradingsymbol"),
            template_name=existing.get("template_name"),
            strategy_name=existing.get("strategy_name"),
            strategy_context=dict(strategy_context),
        )
        return

    if isinstance(open_positions, dict) and label in open_positions:
        existing.update(
            {
                "side": side,
                "qty": qty,
                "entry_price": entry_price,
                "strategy_context": dict(strategy_context),
            }
        )
        open_positions[label] = existing
