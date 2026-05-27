"""Historical option-chain backfill parsing utilities."""

from __future__ import annotations

import csv
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from app.data.option_chain_provider import OptionQuote

IST = ZoneInfo("Asia/Kolkata")


def iter_option_quotes_from_file(
    path: str | Path,
    *,
    default_provider: str,
    default_underlying: str | None = None,
    default_exchange: str = "NFO",
    timestamp_timezone: str = "Asia/Kolkata",
) -> Iterable[OptionQuote]:
    file_path = Path(path)
    tz = ZoneInfo(timestamp_timezone)
    if file_path.suffix.lower() in {".jsonl", ".ndjson"}:
        with file_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                yield option_quote_from_mapping(
                    json.loads(line),
                    default_provider=default_provider,
                    default_underlying=default_underlying,
                    default_exchange=default_exchange,
                    timestamp_tz=tz,
                )
        return

    with file_path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            yield option_quote_from_mapping(
                row,
                default_provider=default_provider,
                default_underlying=default_underlying,
                default_exchange=default_exchange,
                timestamp_tz=tz,
            )


def option_quote_from_mapping(
    row: Mapping[str, Any],
    *,
    default_provider: str,
    default_underlying: str | None = None,
    default_exchange: str = "NFO",
    timestamp_tz: timezone | ZoneInfo = IST,
) -> OptionQuote:
    snapshot_ts = _parse_datetime(
        _field(row, "snapshot_ts", "timestamp", "datetime", "ts"),
        timestamp_tz=timestamp_tz,
    )
    if snapshot_ts is None:
        raise ValueError("backfill row missing snapshot_ts/timestamp")

    expiry = _parse_date(_field(row, "expiry", "expiry_date"))
    if expiry is None:
        raise ValueError("backfill row missing expiry")

    trading_symbol = str(
        _field(row, "trading_symbol", "tradingsymbol", "symbol") or ""
    ).strip()
    option_type = str(_field(row, "option_type", "right", "cp") or "").strip().upper()
    if not option_type and trading_symbol.upper().endswith(("CE", "PE")):
        option_type = trading_symbol[-2:].upper()

    return OptionQuote(
        snapshot_ts=snapshot_ts,
        source_ts=_parse_datetime(
            _field(row, "source_ts", "exchange_ts", "exch_feed_time"),
            timestamp_tz=timestamp_tz,
        ),
        ingested_at=_parse_datetime(
            _field(row, "ingested_at", "created_at"),
            timestamp_tz=timezone.utc,
        ),
        underlying=str(
            _field(row, "underlying", "base_symbol") or default_underlying or ""
        ).strip(),
        expiry=expiry,
        strike=_parse_strike(_field(row, "strike", "strike_price", "strikePrice")),
        option_type=option_type,
        trading_symbol=trading_symbol,
        exchange=str(_field(row, "exchange", "exch_seg") or default_exchange).strip(),
        symbol_token=_optional_text(_field(row, "symbol_token", "symboltoken", "token")),
        provider=str(_field(row, "provider", "source") or default_provider).strip(),
        oi=_field(row, "oi", "open_interest", "opnInterest"),
        volume=_field(row, "volume", "tradeVolume", "tottrdqty"),
        iv=_field(row, "iv", "implied_volatility", "impliedVolatility"),
        delta=_field(row, "delta"),
        gamma=_field(row, "gamma"),
        theta=_field(row, "theta"),
        vega=_field(row, "vega"),
        bid=_field(row, "bid", "best_bid", "bidPrice"),
        ask=_field(row, "ask", "best_ask", "askPrice"),
        ltp=_field(row, "ltp", "last_price", "close"),
        underlying_ltp=_field(row, "underlying_ltp", "spot", "underlyingValue"),
        vix=_field(row, "vix", "india_vix"),
        raw_hash=_optional_text(_field(row, "raw_hash")),
    )


def _field(row: Mapping[str, Any], *names: str) -> Any:
    lower_map = {str(k).strip().lower(): v for k, v in row.items()}
    for name in names:
        value = lower_map.get(str(name).strip().lower())
        if value not in (None, ""):
            return value
    return None


def _parse_datetime(
    value: Any,
    *,
    timestamp_tz: timezone | ZoneInfo,
) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d-%m-%Y %H:%M:%S"):
                try:
                    dt = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    dt = None  # type: ignore[assignment]
            if dt is None:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timestamp_tz)
    return dt.astimezone(timezone.utc)


def _parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d%b%Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def _parse_strike(value: Any) -> int:
    try:
        strike = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return 0
    if strike > Decimal("100000"):
        strike = strike / Decimal("100")
    return int(strike.to_integral_value())


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


__all__ = [
    "IST",
    "iter_option_quotes_from_file",
    "option_quote_from_mapping",
]
