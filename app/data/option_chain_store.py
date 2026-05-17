"""Postgres persistence helpers for 1-minute option-chain snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import json
from typing import Any, Iterable, Mapping

from app.data.option_chain_provider import (
    OptionQuote,
    VALID_OPTION_TYPES,
    quality_flags_for_quote,
)


UPSERT_OPTION_CHAIN_SQL = """
INSERT INTO public.option_chain_1m (
    snapshot_ts, source_ts, ingested_at,
    underlying, expiry, strike, option_type,
    trading_symbol, exchange, symbol_token,
    oi, volume, iv, bid, ask, ltp,
    underlying_ltp, vix,
    provider, raw_hash, quality_flags
)
VALUES (
    %(snapshot_ts)s, %(source_ts)s, %(ingested_at)s,
    %(underlying)s, %(expiry)s, %(strike)s, %(option_type)s,
    %(trading_symbol)s, %(exchange)s, %(symbol_token)s,
    %(oi)s, %(volume)s, %(iv)s, %(bid)s, %(ask)s, %(ltp)s,
    %(underlying_ltp)s, %(vix)s,
    %(provider)s, %(raw_hash)s, %(quality_flags)s::jsonb
)
ON CONFLICT (snapshot_ts, underlying, expiry, strike, option_type, provider)
DO UPDATE SET
    source_ts = EXCLUDED.source_ts,
    ingested_at = EXCLUDED.ingested_at,
    trading_symbol = EXCLUDED.trading_symbol,
    exchange = EXCLUDED.exchange,
    symbol_token = EXCLUDED.symbol_token,
    oi = EXCLUDED.oi,
    volume = EXCLUDED.volume,
    iv = EXCLUDED.iv,
    bid = EXCLUDED.bid,
    ask = EXCLUDED.ask,
    ltp = EXCLUDED.ltp,
    underlying_ltp = EXCLUDED.underlying_ltp,
    vix = EXCLUDED.vix,
    raw_hash = EXCLUDED.raw_hash,
    quality_flags = EXCLUDED.quality_flags,
    updated_at = NOW();
"""


@dataclass(frozen=True)
class OptionChainStore:
    """Small adapter around an existing Postgres connection."""

    conn: Any
    commit: bool = False

    def upsert_quotes(self, quotes: Iterable[OptionQuote]) -> int:
        rows = [option_quote_to_row(quote) for quote in quotes]
        if not rows:
            return 0
        with self.conn.cursor() as cur:
            cur.executemany(UPSERT_OPTION_CHAIN_SQL, rows)
        if self.commit and hasattr(self.conn, "commit"):
            self.conn.commit()
        return len(rows)


def option_quote_to_row(quote: OptionQuote) -> dict[str, Any]:
    """Convert a quote into the parameter dict used by the upsert SQL."""
    q = quote.normalized()
    if not q.underlying:
        raise ValueError("option quote missing underlying")
    if not q.provider:
        raise ValueError("option quote missing provider")
    if q.option_type not in VALID_OPTION_TYPES:
        raise ValueError(f"invalid option_type={q.option_type!r}")
    if not q.trading_symbol:
        raise ValueError("option quote missing trading_symbol")
    if not q.exchange:
        raise ValueError("option quote missing exchange")

    flags = quality_flags_for_quote(q)
    return {
        "snapshot_ts": q.snapshot_ts,
        "source_ts": q.source_ts,
        "ingested_at": q.ingested_at,
        "underlying": q.underlying,
        "expiry": q.expiry,
        "strike": q.strike,
        "option_type": q.option_type,
        "trading_symbol": q.trading_symbol,
        "exchange": q.exchange,
        "symbol_token": q.symbol_token,
        "oi": q.oi,
        "volume": q.volume,
        "iv": q.iv,
        "bid": q.bid,
        "ask": q.ask,
        "ltp": q.ltp,
        "underlying_ltp": q.underlying_ltp,
        "vix": q.vix,
        "provider": q.provider,
        "raw_hash": q.raw_hash,
        "quality_flags": json.dumps(_json_safe(flags), sort_keys=True),
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


__all__ = [
    "OptionChainStore",
    "UPSERT_OPTION_CHAIN_SQL",
    "option_quote_to_row",
]
