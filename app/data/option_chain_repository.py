"""Read-side helpers for option_chain_1m snapshots."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from app.data.option_chain_provider import OptionQuote


ANGEL_PROVIDER = "angel"
NSE_IV_REFERENCE_PROVIDER = "nse_web"
DEFAULT_IV_ENRICHMENT_MAX_AGE_SECONDS = 120


FETCH_SNAPSHOT_SQL = """
SELECT snapshot_ts, source_ts, ingested_at,
       underlying, expiry, strike, option_type,
       trading_symbol, exchange, symbol_token,
       oi, volume, iv, delta, gamma, theta, vega, bid, ask, ltp,
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
       oi, volume, iv, delta, gamma, theta, vega, bid, ask, ltp,
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


FETCH_REFERENCE_IV_SQL = """
SELECT snapshot_ts, strike, option_type, iv, provider
FROM public.option_chain_1m
WHERE underlying = %(underlying)s
  AND expiry = %(expiry)s
  AND provider = %(reference_provider)s
  AND snapshot_ts >= %(min_reference_ts)s
  AND snapshot_ts <= %(max_snapshot_ts)s
  AND iv IS NOT NULL
ORDER BY snapshot_ts DESC, strike ASC, option_type ASC
"""


FETCH_REFERENCE_CONTRACT_IV_SQL = """
SELECT snapshot_ts, strike, option_type, iv, provider
FROM public.option_chain_1m
WHERE underlying = %(underlying)s
  AND expiry = %(expiry)s
  AND strike = %(strike)s
  AND option_type = %(option_type)s
  AND provider = %(reference_provider)s
  AND snapshot_ts >= %(min_reference_ts)s
  AND snapshot_ts <= %(max_snapshot_ts)s
  AND iv IS NOT NULL
ORDER BY snapshot_ts DESC
"""


class OptionChainRepository:
    """Read-only repository for ML feature and label builders."""

    def __init__(
        self,
        conn: Any,
        *,
        iv_enrichment_provider: str | None = NSE_IV_REFERENCE_PROVIDER,
        iv_enrichment_max_age_seconds: int = DEFAULT_IV_ENRICHMENT_MAX_AGE_SECONDS,
    ) -> None:
        self.conn = conn
        self.iv_enrichment_provider = _provider(iv_enrichment_provider)
        self.iv_enrichment_max_age_seconds = _non_negative_seconds(
            iv_enrichment_max_age_seconds
        )

    def fetch_latest_snapshot(
        self,
        *,
        underlying: str,
        expiry: date,
        decision_ts: datetime,
        min_snapshot_ts: datetime,
        provider: str | None = None,
    ) -> list[OptionQuote]:
        selected_provider = _provider(provider)
        params = {
            "underlying": str(underlying).strip().upper(),
            "expiry": expiry,
            "decision_ts": decision_ts,
            "min_snapshot_ts": min_snapshot_ts,
            "provider": selected_provider,
        }
        rows = _execute_rows(self.conn, FETCH_SNAPSHOT_SQL, params)
        if not rows:
            return []
        latest_ts = rows[0]["snapshot_ts"]
        quotes = [_row_to_quote(row) for row in rows if row["snapshot_ts"] == latest_ts]
        if selected_provider != ANGEL_PROVIDER:
            return quotes
        return self._enrich_missing_iv_from_reference(
            quotes,
            underlying=params["underlying"],
            expiry=expiry,
            reference_sql=FETCH_REFERENCE_IV_SQL,
        )

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
        selected_provider = _provider(provider)
        normalized_option_type = str(option_type).strip().upper()
        params = {
            "underlying": str(underlying).strip().upper(),
            "expiry": expiry,
            "strike": int(strike),
            "option_type": normalized_option_type,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "provider": selected_provider,
        }
        quotes = [
            _row_to_quote(row)
            for row in _execute_rows(self.conn, FETCH_CANDIDATE_WINDOW_SQL, params)
        ]
        if selected_provider != ANGEL_PROVIDER:
            return quotes
        return self._enrich_missing_iv_from_reference(
            quotes,
            underlying=params["underlying"],
            expiry=expiry,
            reference_sql=FETCH_REFERENCE_CONTRACT_IV_SQL,
            reference_params={
                "strike": int(strike),
                "option_type": normalized_option_type,
            },
        )

    def _enrich_missing_iv_from_reference(
        self,
        quotes: Sequence[OptionQuote],
        *,
        underlying: str,
        expiry: date,
        reference_sql: str,
        reference_params: dict[str, Any] | None = None,
    ) -> list[OptionQuote]:
        if not self.iv_enrichment_provider or not quotes:
            return list(quotes)

        normalized = [quote.normalized() for quote in quotes]
        missing_iv_quotes = [
            quote
            for quote in normalized
            if quote.provider == ANGEL_PROVIDER and quote.iv is None
        ]
        if not missing_iv_quotes:
            return list(quotes)

        min_quote_ts = min(quote.snapshot_ts for quote in missing_iv_quotes)
        max_quote_ts = max(quote.snapshot_ts for quote in missing_iv_quotes)
        params = {
            "underlying": str(underlying).strip().upper(),
            "expiry": expiry,
            "reference_provider": self.iv_enrichment_provider,
            "min_reference_ts": min_quote_ts
            - timedelta(seconds=self.iv_enrichment_max_age_seconds),
            "max_snapshot_ts": max_quote_ts,
        }
        params.update(reference_params or {})
        reference_rows = _execute_rows(self.conn, reference_sql, params)
        references = _reference_rows_by_contract(reference_rows)
        if not references:
            return list(quotes)

        enriched: list[OptionQuote] = []
        changed = False
        for quote in normalized:
            if quote.provider != ANGEL_PROVIDER or quote.iv is not None:
                enriched.append(quote)
                continue
            reference = _select_reference_row(
                references.get((quote.strike, quote.option_type), ()),
                quote_ts=quote.snapshot_ts,
                max_age_seconds=self.iv_enrichment_max_age_seconds,
            )
            if reference is None:
                enriched.append(quote)
                continue
            enriched.append(_quote_with_reference_iv(quote, reference))
            changed = True
        return enriched if changed else list(quotes)


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
        delta=row.get("delta"),
        gamma=row.get("gamma"),
        theta=row.get("theta"),
        vega=row.get("vega"),
        bid=row.get("bid"),
        ask=row.get("ask"),
        ltp=row.get("ltp"),
        underlying_ltp=row.get("underlying_ltp"),
        vix=row.get("vix"),
        raw_hash=row.get("raw_hash"),
        quality_flags=row.get("quality_flags") or {},
    )


def _reference_rows_by_contract(
    rows: Iterable[dict[str, Any]],
) -> dict[tuple[int, str], list[dict[str, Any]]]:
    references: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("iv") is None:
            continue
        try:
            strike = int(row["strike"])
            option_type = str(row["option_type"]).strip().upper()
            snapshot_ts = _aware_utc(row["snapshot_ts"])
        except (KeyError, TypeError, ValueError):
            continue
        if option_type not in {"CE", "PE"}:
            continue
        normalized = dict(row)
        normalized["snapshot_ts"] = snapshot_ts
        references.setdefault((strike, option_type), []).append(normalized)

    for bucket in references.values():
        bucket.sort(key=lambda row: row["snapshot_ts"], reverse=True)
    return references


def _select_reference_row(
    rows: Iterable[dict[str, Any]],
    *,
    quote_ts: datetime,
    max_age_seconds: int,
) -> dict[str, Any] | None:
    quote_time = _aware_utc(quote_ts)
    max_age = timedelta(seconds=_non_negative_seconds(max_age_seconds))
    for row in rows:
        try:
            reference_time = _aware_utc(row["snapshot_ts"])
        except (KeyError, TypeError, ValueError):
            continue
        if reference_time > quote_time:
            continue
        if quote_time - reference_time <= max_age:
            return row
    return None


def _quote_with_reference_iv(
    quote: OptionQuote,
    reference: dict[str, Any],
) -> OptionQuote:
    reference_ts = _aware_utc(reference["snapshot_ts"])
    quote_ts = _aware_utc(quote.snapshot_ts)
    flags = _without_missing_iv_flags(dict(quote.quality_flags or {}))
    flags["iv_enrichment_mode"] = "read_time"
    flags["iv_enriched_from_provider"] = (
        _provider(reference.get("provider")) or NSE_IV_REFERENCE_PROVIDER
    )
    flags["iv_enrichment_snapshot_ts"] = reference_ts.isoformat()
    flags["iv_enrichment_age_seconds"] = int((quote_ts - reference_ts).total_seconds())
    return replace(quote, iv=reference.get("iv"), quality_flags=flags)


def _without_missing_iv_flags(flags: dict[str, Any]) -> dict[str, Any]:
    for key in ("missing_required_fields", "missing_optional_fields"):
        value = flags.get(key)
        if isinstance(value, (list, tuple, set, frozenset)):
            remaining = sorted(
                str(item)
                for item in value
                if str(item).strip().lower() != "iv"
            )
            if remaining:
                flags[key] = remaining
            else:
                flags.pop(key, None)
        elif str(value).strip().lower() == "iv":
            flags.pop(key, None)
    return flags


def _provider(value: str | None) -> str | None:
    text = str(value or "").strip().lower()
    return text or None


def _non_negative_seconds(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return DEFAULT_IV_ENRICHMENT_MAX_AGE_SECONDS


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "DEFAULT_IV_ENRICHMENT_MAX_AGE_SECONDS",
    "FETCH_CANDIDATE_WINDOW_SQL",
    "FETCH_REFERENCE_CONTRACT_IV_SQL",
    "FETCH_REFERENCE_IV_SQL",
    "FETCH_SNAPSHOT_SQL",
    "NSE_IV_REFERENCE_PROVIDER",
    "OptionChainRepository",
]
