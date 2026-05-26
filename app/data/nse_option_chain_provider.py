"""Validation-only NSE option-chain provider.

This adapter normalizes the public NSE option-chain payload into the same
``OptionQuote`` contract used by Angel ingestion. It is intentionally a
validation source only; strategy/order code must not depend on this web source
for live execution.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import logging
from typing import Any, Protocol
from urllib.parse import urlencode
from urllib.request import Request, build_opener
from zoneinfo import ZoneInfo

from app.data.option_chain_provider import OptionQuote


logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
NSE_OPTION_CHAIN_URL = "https://www.nseindia.com/option-chain"
NSE_OPTION_CHAIN_API_URL = "https://www.nseindia.com/api/option-chain-indices"
NSE_MARKET_OPTION_CHAIN_URL = "https://www.nseindia.com/market-data/option-chain"
NSE_LIVE_EQUITY_DERIVATIVES_API_URL = (
    "https://www.nseindia.com/api/liveEquity-derivatives"
)
NSE_LIVE_EQUITY_INDEX_PARAMS = {
    "NIFTY": "nse50_opt",
}
NSE_LIVE_EQUITY_OI_LOT_SIZE = {
    "NIFTY": 65,
}
NSE_LIVE_EQUITY_SOURCE = "live_equity_derivatives"


class NseOptionChainClient(Protocol):
    def fetch_option_chain(self, *, symbol: str) -> Mapping[str, Any]:
        ...


@dataclass(frozen=True)
class NseWebOptionChainClient:
    """Small NSE web client for operator-triggered validation pulls.

    The initial page request establishes cookies expected by the JSON endpoint.
    Use this sparingly for validation/cross-checking, not as a production market
    data feed.
    """

    timeout_seconds: float = 10.0
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0 Safari/537.36"
    )
    opener_factory: Callable[..., Any] = build_opener

    def fetch_option_chain(self, *, symbol: str) -> Mapping[str, Any]:
        resolved_symbol = str(symbol or "").strip().upper()
        if not resolved_symbol:
            raise ValueError("NSE option-chain symbol is required")
        opener = self.opener_factory()
        headers = self._headers(referer=NSE_OPTION_CHAIN_URL)
        opener.open(
            Request(NSE_OPTION_CHAIN_URL, headers=headers),
            timeout=float(self.timeout_seconds),
        ).close()
        api_url = f"{NSE_OPTION_CHAIN_API_URL}?{urlencode({'symbol': resolved_symbol})}"
        try:
            with opener.open(
                Request(api_url, headers=self._headers(referer=NSE_OPTION_CHAIN_URL)),
                timeout=float(self.timeout_seconds),
            ) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
        except Exception as exc:  # noqa: BLE001 - validation fallback may still work.
            logger.warning(
                "NSE option-chain API failed for symbol=%s; trying live-derivatives fallback: %s",
                resolved_symbol,
                exc,
            )
            fallback_payload = self._fetch_live_equity_derivatives(
                opener,
                symbol=resolved_symbol,
            )
            if fallback_payload is not None:
                return fallback_payload
            raise
        if not isinstance(payload, Mapping):
            raise ValueError(f"NSE option-chain payload type={type(payload).__name__} is invalid")
        if _has_option_chain_rows(payload):
            return payload

        fallback_payload = self._fetch_live_equity_derivatives(
            opener,
            symbol=resolved_symbol,
        )
        if fallback_payload is not None:
            return fallback_payload
        raise ValueError(
            f"NSE option-chain payload contained no comparable rows for symbol={resolved_symbol}"
        )

    def _fetch_live_equity_derivatives(
        self,
        opener: Any,
        *,
        symbol: str,
    ) -> Mapping[str, Any] | None:
        index_param = NSE_LIVE_EQUITY_INDEX_PARAMS.get(symbol)
        if not index_param:
            return None
        try:
            opener.open(
                Request(
                    NSE_MARKET_OPTION_CHAIN_URL,
                    headers=self._headers(referer=NSE_MARKET_OPTION_CHAIN_URL),
                ),
                timeout=float(self.timeout_seconds),
            ).close()
        except Exception as exc:  # noqa: BLE001 - fallback API may still work.
            logger.debug("NSE market option-chain warmup failed: %s", exc)
        fallback_url = (
            f"{NSE_LIVE_EQUITY_DERIVATIVES_API_URL}?"
            f"{urlencode({'index': index_param})}"
        )
        with opener.open(
            Request(
                fallback_url,
                headers=self._headers(referer=NSE_MARKET_OPTION_CHAIN_URL),
            ),
            timeout=float(self.timeout_seconds),
        ) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        if not isinstance(payload, Mapping):
            raise ValueError(
                f"NSE live-derivatives payload type={type(payload).__name__} is invalid"
            )
        if not _has_live_equity_rows(payload):
            return None
        tagged_payload = dict(payload)
        tagged_payload["__phoenix_nse_source"] = NSE_LIVE_EQUITY_SOURCE
        tagged_payload["__phoenix_nse_symbol"] = symbol
        return tagged_payload

    def _headers(self, *, referer: str) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": referer,
            "Connection": "keep-alive",
        }


@dataclass(frozen=True)
class NseOptionChainProvider:
    """Normalize NSE option-chain JSON into ``OptionQuote`` rows."""

    client: NseOptionChainClient | Mapping[str, Any]
    provider_name: str = "nse_web"

    def fetch_chain(
        self,
        *,
        underlying: str,
        expiry: date,
        snapshot_ts: datetime,
    ) -> Sequence[OptionQuote]:
        payload = (
            self.client.fetch_option_chain(symbol=underlying)
            if hasattr(self.client, "fetch_option_chain")
            else self.client
        )
        return parse_nse_option_chain_payload(
            payload,
            underlying=underlying,
            expiry=expiry,
            snapshot_ts=snapshot_ts,
            provider=self.provider_name,
        )


def parse_nse_option_chain_payload(
    payload: Mapping[str, Any],
    *,
    underlying: str,
    expiry: date,
    snapshot_ts: datetime,
    provider: str = "nse_web",
) -> list[OptionQuote]:
    records = payload.get("records")
    filtered = payload.get("filtered")
    record_map = records if isinstance(records, Mapping) else {}
    filtered_map = filtered if isinstance(filtered, Mapping) else {}
    source_ts = _parse_nse_timestamp(
        _first(record_map, "timestamp", "timeStamp")
        or _first(filtered_map, "timestamp", "timeStamp")
    )
    underlying_ltp = _first(record_map, "underlyingValue", "underlying_value")
    if underlying_ltp is None:
        underlying_ltp = _first(filtered_map, "underlyingValue", "underlying_value")

    rows = _data_rows(record_map) or _data_rows(filtered_map)
    normalized_underlying = str(underlying or "").strip().upper()
    normalized_provider = str(provider or "").strip().lower() or "nse_web"
    snapshot = _aware_utc(snapshot_ts)

    live_rows = _live_equity_rows(payload)
    if not rows and live_rows:
        return _parse_live_equity_derivatives_rows(
            live_rows,
            payload=payload,
            underlying=normalized_underlying,
            expiry=expiry,
            snapshot=snapshot,
            provider=normalized_provider,
        )

    quotes: list[OptionQuote] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        row_expiry = _parse_expiry(_first(row, "expiryDate", "expiry"))
        if row_expiry != expiry:
            continue
        strike = _parse_strike(_first(row, "strikePrice", "strike"))
        if strike <= 0:
            continue
        for option_type in ("CE", "PE"):
            leg = row.get(option_type)
            if not isinstance(leg, Mapping):
                continue
            identifier = _optional_text(_first(leg, "identifier"))
            symbol = identifier or _fallback_symbol(
                underlying=normalized_underlying,
                expiry=expiry,
                strike=strike,
                option_type=option_type,
            )
            leg_underlying_ltp = _first(leg, "underlyingValue", "underlying_value")
            quotes.append(
                OptionQuote(
                    snapshot_ts=snapshot,
                    source_ts=_parse_nse_timestamp(
                        _first(leg, "lastUpdateTime", "lastUpdate", "timestamp")
                    )
                    or source_ts,
                    underlying=normalized_underlying,
                    expiry=expiry,
                    strike=strike,
                    option_type=option_type,
                    trading_symbol=symbol,
                    exchange="NFO",
                    symbol_token=identifier,
                    provider=normalized_provider,
                    oi=_first(leg, "openInterest", "oi"),
                    volume=_first(leg, "totalTradedVolume", "volume"),
                    iv=_first(leg, "impliedVolatility", "iv"),
                    bid=_first(leg, "bidprice", "bidPrice", "bid"),
                    ask=_first(leg, "askPrice", "askprice", "ask"),
                    ltp=_first(leg, "lastPrice", "ltp"),
                    underlying_ltp=leg_underlying_ltp or underlying_ltp,
                    raw_hash=_raw_hash(leg),
                    quality_flags={"validation_source_only": True},
                )
            )
    return sorted(quotes, key=lambda q: (q.strike, q.option_type))


def _parse_live_equity_derivatives_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    payload: Mapping[str, Any],
    underlying: str,
    expiry: date,
    snapshot: datetime,
    provider: str,
) -> list[OptionQuote]:
    source_ts = _parse_nse_timestamp(_first(payload, "timestamp", "timeStamp"))
    quotes: list[OptionQuote] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        row_expiry = _parse_expiry(_first(row, "expiryDate", "expiry"))
        if row_expiry != expiry:
            continue
        row_underlying = str(_first(row, "underlying", "symbol") or "").strip().upper()
        identifier = _optional_text(_first(row, "identifier"))
        if row_underlying and row_underlying != underlying:
            continue
        if (
            not row_underlying
            and identifier
            and not identifier.upper().startswith(f"OPTIDX{underlying}")
        ):
            continue
        strike = _parse_strike(_first(row, "strikePrice", "strike"))
        if strike <= 0:
            continue
        option_type = _parse_live_equity_option_type(
            _first(row, "optionType", "option_type")
        )
        if option_type is None:
            continue
        symbol = identifier or _fallback_symbol(
            underlying=underlying,
            expiry=expiry,
            strike=strike,
            option_type=option_type,
        )
        quotes.append(
            OptionQuote(
                snapshot_ts=snapshot,
                source_ts=source_ts,
                underlying=underlying,
                expiry=expiry,
                strike=strike,
                option_type=option_type,
                trading_symbol=symbol,
                exchange="NFO",
                symbol_token=identifier,
                provider=provider,
                oi=_scale_live_equity_open_interest(
                    _first(row, "openInterest", "oi"),
                    underlying=underlying,
                ),
                volume=_first(row, "totalTradedVolume", "volume"),
                iv=None,
                bid=None,
                ask=None,
                ltp=_first(row, "lastPrice", "ltp"),
                underlying_ltp=_first(row, "underlyingValue", "underlying_ltp"),
                raw_hash=_raw_hash(row),
                quality_flags={
                    "validation_source_only": True,
                    "nse_source": NSE_LIVE_EQUITY_SOURCE,
                    "nse_open_interest_unit": "underlying_units",
                    "nse_open_interest_lot_size": NSE_LIVE_EQUITY_OI_LOT_SIZE.get(
                        underlying
                    ),
                    "reference_contract_coverage": "partial",
                    "missing_reference_fields_expected": ["ask", "bid", "iv"],
                    "non_equivalent_reference_fields_expected": [
                        "ltp",
                        "oi",
                        "volume",
                    ],
                },
            )
        )
    return sorted(quotes, key=lambda q: (q.strike, q.option_type))


def _data_rows(section: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    data = section.get("data")
    if isinstance(data, Sequence) and not isinstance(data, (str, bytes)):
        return [row for row in data if isinstance(row, Mapping)]
    return []


def _live_equity_rows(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if payload.get("__phoenix_nse_source") != NSE_LIVE_EQUITY_SOURCE:
        return []
    data = payload.get("data")
    if isinstance(data, Sequence) and not isinstance(data, (str, bytes)):
        return [row for row in data if isinstance(row, Mapping)]
    return []


def _has_option_chain_rows(payload: Mapping[str, Any]) -> bool:
    records = payload.get("records")
    filtered = payload.get("filtered")
    record_map = records if isinstance(records, Mapping) else {}
    filtered_map = filtered if isinstance(filtered, Mapping) else {}
    return bool(_data_rows(record_map) or _data_rows(filtered_map))


def _has_live_equity_rows(payload: Mapping[str, Any]) -> bool:
    data = payload.get("data")
    return isinstance(data, Sequence) and not isinstance(data, (str, bytes)) and bool(data)


def _first(payload: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload and payload[key] not in (None, ""):
            return payload[key]
    return None


def _parse_nse_timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return _aware_utc(value)
    text = str(value).strip().replace("Z", "+00:00")
    for fmt in (
        "%d-%b-%Y %H:%M:%S",
        "%d-%b-%Y %H:%M",
        "%d-%m-%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=IST).astimezone(timezone.utc)
        except ValueError:
            continue
    try:
        return _aware_utc(datetime.fromisoformat(text))
    except ValueError:
        logger.debug("Unable to parse NSE timestamp %r", value)
        return None


def _parse_expiry(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%d-%b-%Y", "%d%b%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def _parse_live_equity_option_type(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    if text in {"CE", "CALL"}:
        return "CE"
    if text in {"PE", "PUT"}:
        return "PE"
    return None


def _scale_live_equity_open_interest(value: Any, *, underlying: str) -> int | None:
    oi = _optional_int(value)
    if oi is None:
        return None
    lot_size = NSE_LIVE_EQUITY_OI_LOT_SIZE.get(underlying)
    if lot_size is None:
        return oi
    return oi * int(lot_size)


def _parse_strike(value: Any) -> int:
    parsed = _decimal(value)
    if parsed is None:
        return 0
    return int(parsed.to_integral_value())


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except Exception:
        try:
            return int(float(value))
        except Exception:
            return None


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _fallback_symbol(
    *,
    underlying: str,
    expiry: date,
    strike: int,
    option_type: str,
) -> str:
    return f"{underlying}{expiry.strftime('%d%b%y').upper()}{strike}{option_type}"


def _raw_hash(payload: Mapping[str, Any]) -> str:
    text = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=IST).astimezone(timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "NSE_LIVE_EQUITY_DERIVATIVES_API_URL",
    "NSE_LIVE_EQUITY_OI_LOT_SIZE",
    "NSE_LIVE_EQUITY_SOURCE",
    "NSE_MARKET_OPTION_CHAIN_URL",
    "NSE_OPTION_CHAIN_API_URL",
    "NSE_OPTION_CHAIN_URL",
    "NseOptionChainProvider",
    "NseWebOptionChainClient",
    "parse_nse_option_chain_payload",
]
