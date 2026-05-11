"""Schema-safe helpers for replay reads from indicator_bars."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Sequence, Tuple

import psycopg
from psycopg import sql

DEFAULT_TABLE = "indicator_bars"
DEFAULT_REGIME_SIDECAR_TABLE = "bar_regime"
_POSTGRES_IDENTIFIER_MAX_LEN = 63

_IDENTIFIER_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")

_COLUMN_NULL_SQL: Dict[str, str] = {
    "id": "NULL::BIGINT AS id",
    "ts_start": "NULL::TIMESTAMPTZ AS ts_start",
    "ts_end": "NULL::TIMESTAMPTZ AS ts_end",
    "label": "NULL::TEXT AS label",
    "timeframe_seconds": "NULL::INTEGER AS timeframe_seconds",
    "o": "NULL::DOUBLE PRECISION AS o",
    "h": "NULL::DOUBLE PRECISION AS h",
    "l": "NULL::DOUBLE PRECISION AS l",
    "c": "NULL::DOUBLE PRECISION AS c",
    "atr": "NULL::DOUBLE PRECISION AS atr",
    "rsi": "NULL::DOUBLE PRECISION AS rsi",
    "macd": "NULL::DOUBLE PRECISION AS macd",
    "macd_signal": "NULL::DOUBLE PRECISION AS macd_signal",
    "macd_hist": "NULL::DOUBLE PRECISION AS macd_hist",
    "ema_20": "NULL::DOUBLE PRECISION AS ema_20",
    "ema_30": "NULL::DOUBLE PRECISION AS ema_30",
    "ema_50": "NULL::DOUBLE PRECISION AS ema_50",
    "exclusive_nifty_ce_buy_ema20_30s": "NULL::DOUBLE PRECISION AS exclusive_nifty_ce_buy_ema20_30s",
    "adx": "NULL::DOUBLE PRECISION AS adx",
    "plus_di": "NULL::DOUBLE PRECISION AS plus_di",
    "minus_di": "NULL::DOUBLE PRECISION AS minus_di",
    "di_spread": "NULL::DOUBLE PRECISION AS di_spread",
}


@dataclass(frozen=True)
class ReplayTableSchema:
    """Column availability for the replay source table."""

    normalized_table: str
    available_columns: Tuple[str, ...]
    required_columns: Tuple[str, ...]
    optional_columns: Tuple[str, ...]

    @property
    def available_set(self) -> set[str]:
        return set(self.available_columns)

    @property
    def missing_optional_columns(self) -> Tuple[str, ...]:
        return tuple(col for col in self.optional_columns if col not in self.available_set)

    @property
    def missing_required_columns(self) -> Tuple[str, ...]:
        return tuple(col for col in self.required_columns if col not in self.available_set)


def normalize_table_name(raw: str, default: str = DEFAULT_TABLE) -> Tuple[str, Tuple[str, ...]]:
    """Normalize a potentially schema-qualified table name."""

    text = str(raw or "").strip() or default
    parts = [_sanitize_identifier(part) for part in text.split(".") if str(part).strip()]
    if not parts:
        parts = [_sanitize_identifier(default)]
    return ".".join(parts), tuple(parts)


def regime_sidecar_table_name(
    raw: str,
    default: str = DEFAULT_TABLE,
) -> Tuple[str, Tuple[str, ...]]:
    """Return the source-scoped bar_regime table for an indicator source table."""

    _, parts = normalize_table_name(raw, default)
    source_table = parts[-1] if parts else DEFAULT_TABLE
    if source_table == DEFAULT_TABLE:
        sidecar_table = DEFAULT_REGIME_SIDECAR_TABLE
    else:
        sidecar_table = _derived_identifier(source_table, "bar_regime")
    sidecar_parts = tuple([*parts[:-1], sidecar_table])
    return ".".join(sidecar_parts), sidecar_parts


def inspect_table_schema(
    dsn: str,
    table: str,
    *,
    required_columns: Sequence[str],
    optional_columns: Sequence[str],
) -> ReplayTableSchema:
    """Fetch the available replay columns without mutating the table."""

    normalized_table, table_parts = normalize_table_name(table)
    query = sql.SQL("SELECT * FROM {} WHERE FALSE").format(_qualified_identifier(table_parts))

    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            description = getattr(cur, "description", None) or ()

    available_columns = tuple(str(col.name) for col in description if getattr(col, "name", None))
    schema = ReplayTableSchema(
        normalized_table=normalized_table,
        available_columns=tuple(sorted(set(available_columns))),
        required_columns=tuple(required_columns),
        optional_columns=tuple(optional_columns),
    )
    if schema.missing_required_columns:
        raise RuntimeError(
            f"Replay source table {normalized_table!r} is missing required columns: "
            f"{', '.join(schema.missing_required_columns)}"
        )
    return schema


def build_select_list(
    schema: ReplayTableSchema,
    columns: Iterable[str],
) -> str:
    """Build a select list that tolerates missing optional columns."""

    available = schema.available_set
    parts = []
    for column in columns:
        if column in available:
            parts.append(column)
        else:
            parts.append(_COLUMN_NULL_SQL[column])
    return ", ".join(parts)


def _qualified_identifier(parts: Sequence[str]) -> sql.Composed:
    return sql.SQL(".").join(sql.Identifier(part) for part in parts)


def _sanitize_identifier(raw: object) -> str:
    text = "".join(ch for ch in str(raw or "").strip() if ch in _IDENTIFIER_CHARS)
    if not text:
        return DEFAULT_TABLE
    return text


def _derived_identifier(base: str, suffix: str) -> str:
    candidate = f"{base}_{suffix}"
    if len(candidate) <= _POSTGRES_IDENTIFIER_MAX_LEN:
        return candidate
    digest = hashlib.sha1(candidate.encode("ascii", "ignore")).hexdigest()[:8]
    keep = _POSTGRES_IDENTIFIER_MAX_LEN - len(digest) - 1
    return f"{candidate[:keep]}_{digest}"


__all__ = [
    "DEFAULT_TABLE",
    "DEFAULT_REGIME_SIDECAR_TABLE",
    "ReplayTableSchema",
    "build_select_list",
    "inspect_table_schema",
    "normalize_table_name",
    "regime_sidecar_table_name",
]
