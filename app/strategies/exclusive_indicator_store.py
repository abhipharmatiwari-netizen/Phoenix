from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

from app.config.settings import get_settings
from app.data.postgres import connect_with_retry, get_control_plane_dsn

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
EXCLUSIVE_NIFTY_CE_BUY_EMA20_30S_COLUMN = "exclusive_nifty_ce_buy_ema20_30s"


def _safe_identifier(raw: str, default: str) -> str:
    text = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in str(raw or ""))
    text = text.strip("_")
    return text or default


def _safe_table_name(raw: str, default: str) -> tuple[str, str]:
    text = str(raw or "").strip() or default
    parts = [_safe_identifier(part, "") for part in text.split(".") if part]
    if not parts:
        parts = [default]
    normalized = ".".join(parts)
    quoted = ".".join(f'"{part}"' for part in parts)
    return normalized, quoted


def _is_true(raw: object) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _to_optional_float(value: object) -> Optional[float]:
    if value is None:
        return None
    try:
        out = float(value)
    except Exception:
        return None
    if not math.isfinite(out):
        return None
    return out


def _session_bounds_utc(session_date: date) -> tuple[datetime, datetime]:
    start_ist = datetime.combine(session_date, time.min, IST)
    end_ist = start_ist + timedelta(days=1)
    return start_ist.astimezone(timezone.utc), end_ist.astimezone(timezone.utc)


@dataclass(frozen=True)
class IndicatorSessionRow:
    ts_start: datetime
    ts_end: Optional[datetime]
    o: float
    h: float
    l: float
    c: float
    atr: Optional[float]
    rsi: Optional[float]
    macd: Optional[float]
    macd_signal: Optional[float]
    macd_hist: Optional[float]
    adx: Optional[float]
    plus_di: Optional[float]
    minus_di: Optional[float]
    di_spread: Optional[float]
    private_ema20: Optional[float]


class ExclusiveNiftyCeIndicatorStore:
    def __init__(
        self,
        *,
        dsn: str,
        table_name: str = "indicator_bars",
        ema_column: str = EXCLUSIVE_NIFTY_CE_BUY_EMA20_30S_COLUMN,
        enable_backfill: bool = True,
    ) -> None:
        self._dsn = str(dsn or "").strip()
        self._table, self._table_sql = _safe_table_name(table_name, "indicator_bars")
        self._ema_column = _safe_identifier(
            ema_column, EXCLUSIVE_NIFTY_CE_BUY_EMA20_30S_COLUMN
        )
        self._ensure_schema()
        if enable_backfill:
            self.backfill_private_ema_history(
                label="NIFTY_IDX",
                timeframe_seconds=30,
                period=20,
            )

    @property
    def table_name(self) -> str:
        return self._table

    @property
    def ema_column(self) -> str:
        return self._ema_column

    def _conn(self, *, autocommit: bool = True):
        return connect_with_retry(self._dsn, autocommit=autocommit, max_attempts=3)

    def _ensure_schema(self) -> None:
        alter_sql = (
            f"ALTER TABLE {self._table_sql} ADD COLUMN IF NOT EXISTS {self._ema_column} "
            "DOUBLE PRECISION"
        )
        with self._conn(autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(alter_sql)

    def upsert_private_ema(
        self,
        *,
        label: str,
        timeframe_seconds: int,
        ts_start: datetime,
        ts_end: Optional[datetime],
        ema_value: Optional[float],
    ) -> None:
        ts_start_utc = _to_utc(ts_start)
        ts_end_utc = _to_utc(ts_end) if ts_end is not None else None
        query = f"""
            INSERT INTO {self._table_sql} (
                ts_start,
                ts_end,
                label,
                timeframe_seconds,
                {self._ema_column}
            )
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (label, timeframe_seconds, ts_start)
            DO UPDATE SET
                ts_end = COALESCE(EXCLUDED.ts_end, {self._table_sql}.ts_end),
                {self._ema_column} = EXCLUDED.{self._ema_column}
        """
        payload = (
            ts_start_utc,
            ts_end_utc,
            str(label or "").strip(),
            int(timeframe_seconds),
            _to_optional_float(ema_value),
        )
        with self._conn(autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(query, payload)

    def load_session_rows(
        self,
        *,
        label: str,
        timeframe_seconds: int,
        session_date: date,
        before_ts: Optional[datetime] = None,
        max_rows: int = 400,
    ) -> list[IndicatorSessionRow]:
        start_utc, end_utc = _session_bounds_utc(session_date)
        where_clauses = [
            "label = %s",
            "timeframe_seconds = %s",
            "ts_start >= %s",
            "ts_start < %s",
        ]
        params: list[object] = [
            str(label or "").strip(),
            int(timeframe_seconds),
            start_utc,
            end_utc,
        ]
        if before_ts is not None:
            where_clauses.append("ts_start < %s")
            params.append(_to_utc(before_ts))
        limit = max(1, int(max_rows or 0))
        params.append(limit)
        query = f"""
            SELECT *
            FROM (
                SELECT
                    ts_start,
                    ts_end,
                    o,
                    h,
                    l,
                    c,
                    atr,
                    rsi,
                    macd,
                    macd_signal,
                    macd_hist,
                    adx,
                    plus_di,
                    minus_di,
                    di_spread,
                    {self._ema_column}
                FROM {self._table_sql}
                WHERE {" AND ".join(where_clauses)}
                ORDER BY ts_start DESC
                LIMIT %s
            ) seeded
            ORDER BY ts_start ASC
        """
        rows: list[IndicatorSessionRow] = []
        with self._conn(autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(query, tuple(params))
                for row in cur.fetchall() or []:
                    rows.append(
                        IndicatorSessionRow(
                            ts_start=_to_utc(row[0]),
                            ts_end=_to_utc(row[1]) if row[1] is not None else None,
                            o=float(row[2]),
                            h=float(row[3]),
                            l=float(row[4]),
                            c=float(row[5]),
                            atr=_to_optional_float(row[6]),
                            rsi=_to_optional_float(row[7]),
                            macd=_to_optional_float(row[8]),
                            macd_signal=_to_optional_float(row[9]),
                            macd_hist=_to_optional_float(row[10]),
                            adx=_to_optional_float(row[11]),
                            plus_di=_to_optional_float(row[12]),
                            minus_di=_to_optional_float(row[13]),
                            di_spread=_to_optional_float(row[14]),
                            private_ema20=_to_optional_float(row[15]),
                        )
                    )
        return rows

    def backfill_private_ema_history(
        self,
        *,
        label: str,
        timeframe_seconds: int,
        period: int = 20,
    ) -> int:
        period_int = max(2, int(period))
        select_sql = f"""
            SELECT ts_start, c, {self._ema_column}
            FROM {self._table_sql}
            WHERE label = %s
              AND timeframe_seconds = %s
            ORDER BY ts_start ASC
        """
        updates: list[tuple[Optional[float], str, int, datetime]] = []
        k = 2.0 / (float(period_int) + 1.0)
        current_session: Optional[date] = None
        ema_state: Optional[float] = None
        session_count = 0

        with self._conn(autocommit=False) as conn:
            with conn.cursor() as cur:
                cur.execute(select_sql, (str(label or "").strip(), int(timeframe_seconds)))
                for ts_start_raw, close_raw, stored_raw in cur.fetchall() or []:
                    ts_start = _to_utc(ts_start_raw)
                    session_date = ts_start.astimezone(IST).date()
                    close_val = float(close_raw)
                    if current_session != session_date:
                        current_session = session_date
                        ema_state = close_val
                        session_count = 1
                    else:
                        session_count += 1
                        if ema_state is None:
                            ema_state = close_val
                        else:
                            ema_state = (close_val * k) + (ema_state * (1.0 - k))

                    computed = ema_state if session_count >= period_int else None
                    stored = _to_optional_float(stored_raw)
                    if computed is None and stored is None:
                        continue
                    if computed is not None and stored is not None and math.isclose(
                        computed,
                        stored,
                        rel_tol=1e-9,
                        abs_tol=1e-9,
                    ):
                        continue
                    updates.append(
                        (
                            computed,
                            str(label or "").strip(),
                            int(timeframe_seconds),
                            ts_start,
                        )
                    )

                if updates:
                    cur.executemany(
                        f"""
                        UPDATE {self._table_sql}
                        SET {self._ema_column} = %s
                        WHERE label = %s
                          AND timeframe_seconds = %s
                          AND ts_start = %s
                        """,
                        updates,
                    )
            conn.commit()
        if updates:
            logger.info(
                "Backfilled %d private EMA rows for %s tf=%ss column=%s",
                len(updates),
                str(label or "").strip(),
                int(timeframe_seconds),
                self._ema_column,
            )
        return len(updates)


def build_exclusive_nifty_ce_indicator_store(
    *,
    enable_backfill: bool = True,
) -> Optional[ExclusiveNiftyCeIndicatorStore]:
    indicator_dsn = str(os.getenv("INDICATOR_POSTGRES_DSN", "") or "").strip()
    if not indicator_dsn:
        if not _is_true(os.getenv("INDICATOR_POSTGRES_ENABLED", "0")):
            return None
        try:
            indicator_dsn = get_control_plane_dsn(get_settings())
        except Exception as exc:
            logger.warning(
                "Exclusive CE private indicator store unavailable: failed resolving Postgres DSN: %s",
                exc,
            )
            return None

    table_name = str(os.getenv("INDICATOR_POSTGRES_TABLE", "indicator_bars") or "").strip()
    table_name = table_name or "indicator_bars"
    try:
        return ExclusiveNiftyCeIndicatorStore(
            dsn=indicator_dsn,
            table_name=table_name,
            enable_backfill=enable_backfill,
        )
    except Exception as exc:
        logger.warning(
            "Exclusive CE private indicator store unavailable: %s",
            exc,
        )
        return None


__all__ = [
    "EXCLUSIVE_NIFTY_CE_BUY_EMA20_30S_COLUMN",
    "ExclusiveNiftyCeIndicatorStore",
    "IndicatorSessionRow",
    "build_exclusive_nifty_ce_indicator_store",
]
