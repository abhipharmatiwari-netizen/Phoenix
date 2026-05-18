"""Read-side helpers for option_chain_1m snapshots."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable

from app.data.option_chain_provider import OptionQuote


FETCH_SNAPSHOT_SQL = """
SELECT snapshot_ts, source_ts, ingested_at,
       underlying, expiry, strike, option_type,
       trading_symbol, exchange, symbol_token,
       oi, volume, iv, bid, ask, ltp,
       underlying_ltp, vix,
       provider, raw_hash, quality_flags
FROM public.option_chain_1m
WHERE underlying = %(underlying)s
  AND expiry = %(expiry)s
  AND snapshot_ts <= %(decision_ts)s
  AND snapshot_ts >= %(min_snapshot_ts)s
  AND (%(provider)s::text IS NULL OR provider = %(provider)s::text)
ORDER BY snapshot_ts DESC, strike ASC, option_type ASC
"""


FETCH_CANDIDATE_WINDOW_SQL = """
SELECT snapshot_ts, source_ts, ingested_at,
       underlying, expiry, strike, option_type,
       trading_symbol, exchange, symbol_token,
       oi, volume, iv, bid, ask, ltp,
       underlying_ltp, vix,
       provider, raw_hash, quality_flags
FROM public.option_chain_1m
WHERE underlying = %(underlying)s
  AND expiry = %(expiry)s
  AND strike = %(strike)s
  AND option_type = %(option_type)s
  AND snapshot_ts >= %(start_ts)s
  AND snapshot_ts <= %(end_ts)s
  AND (%(provider)s::text IS NULL OR provider = %(provider)s::text)
ORDER BY snapshot_ts ASC
"""


class OptionChainRepository:
    """Read-only repository for ML feature and label builders."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def fetch_latest_snapshot(
        self,
        *,
        underlying: str,
        expiry: date,
        decision_ts: datetime,
        min_snapshot_ts: datetime,
        provider: str | None = None,
    ) -> list[OptionQuote]:
        params = {
            "underlying": str(underlying).strip().upper(),
            "expiry": expiry,
            "decision_ts": decision_ts,
            "min_snapshot_ts": min_snapshot_ts,
            "provider": _provider(provider),
        }
        rows = _execute_rows(self.conn, FETCH_SNAPSHOT_SQL, params)
        if not rows:
            return []
        latest_ts = rows[0]["snapshot_ts"]
        return [_row_to_quote(row) for row in rows if row["snapshot_ts"] == latest_ts]

    def fetch_candidate_window(
        self,
        *,
        underlying: str,
        expiry: date,
        strike: int,
        option_type: str,
        start_ts: datetime,
        end_ts: datetime,
        provider: str | None = None,
    ) -> list[OptionQuote]:
        params = {
            "underlying": str(underlying).strip().upper(),
            "expiry": expiry,
            "strike": int(strike),
            "option_type": str(option_type).strip().upper(),
            "start_ts": start_ts,
            "end_ts": end_ts,
            "provider": _provider(provider),
        }
        return [_row_to_quote(row) for row in _execute_rows(self.conn, FETCH_CANDIDATE_WINDOW_SQL, params)]


def _execute_rows(conn: Any, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
        description = getattr(cur, "description", None)
    if not rows:
        return []
    if rows and isinstance(rows[0], dict):
        return list(rows)
    columns = [desc[0] for desc in description or []]
    return [dict(zip(columns, row)) for row in rows]


def _row_to_quote(row: dict[str, Any]) -> OptionQuote:
    return OptionQuote(
        snapshot_ts=row["snapshot_ts"],
        source_ts=row.get("source_ts"),
        ingested_at=row.get("ingested_at"),
        underlying=row["underlying"],
        expiry=row["expiry"],
        strike=row["strike"],
        option_type=row["option_type"],
        trading_symbol=row["trading_symbol"],
        exchange=row["exchange"],
        symbol_token=row.get("symbol_token"),
        provider=row["provider"],
        oi=row.get("oi"),
        volume=row.get("volume"),
        iv=row.get("iv"),
        bid=row.get("bid"),
        ask=row.get("ask"),
        ltp=row.get("ltp"),
        underlying_ltp=row.get("underlying_ltp"),
        vix=row.get("vix"),
        raw_hash=row.get("raw_hash"),
        quality_flags=row.get("quality_flags") or {},
    )


def _provider(value: str | None) -> str | None:
    text = str(value or "").strip().lower()
    return text or None


__all__ = [
    "FETCH_CANDIDATE_WINDOW_SQL",
    "FETCH_SNAPSHOT_SQL",
    "OptionChainRepository",
]
