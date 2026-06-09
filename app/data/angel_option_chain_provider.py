"""Angel SmartAPI option-chain provider backed by scrip-master tokens."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import logging
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from app.data.option_chain_provider import OptionQuote


logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


class MarketQuoteFetcher(Protocol):
    def fetch_market_quotes(
        self,
        *,
        mode: str,
        exchange_to_tokens: dict[str, list[str]],
    ) -> Sequence[Mapping[str, Any]]:
        ...


@dataclass(frozen=True)
class AngelOptionChainProvider:
    """Build a chain from Angel scrip master rows and FULL quote payloads."""

    quote_fetcher: MarketQuoteFetcher | Callable[..., Sequence[Mapping[str, Any]]]
    scrip_master: Any
    provider_name: str = "angel"
    option_segments: tuple[str, ...] = ("NFO", "NSEFO")
    batch_size: int = 50

    def fetch_chain(
        self,
        *,
        underlying: str,
        expiry: date,
        snapshot_ts: datetime,
    ) -> Sequence[OptionQuote]:
        rows = _option_rows_for_expiry(
            self.scrip_master,
            underlying=underlying,
            expiry=expiry,
            option_segments=self.option_segments,
        )
        quote_payloads = self._fetch_quote_payloads(rows)
        greek_payloads = self._fetch_greek_payloads(
            underlying=underlying,
            expiry=expiry,
        )
        context_payloads = self._fetch_context_payloads(underlying)
        underlying_ltp = _ltp_from_payload(context_payloads.get("underlying"))
        vix = _ltp_from_payload(context_payloads.get("vix"))
        snapshot = _aware_utc(snapshot_ts)

        quotes: list[OptionQuote] = []
        for row in rows:
            token = _row_value(row, "token", "symboltoken", "symbolToken")
            exchange = str(_row_value(row, "exch_seg", "exchange") or "NFO").strip().upper()
            symbol = str(_row_value(row, "symbol", "tradingsymbol", "trading_symbol") or "").strip()
            payload = quote_payloads.get((exchange, str(token)))
            quality_flags = {}
            if payload is None:
                quality_flags["missing_quote_payload"] = True
                payload = {}
            option_type = _option_type_from_symbol(symbol)
            greek = greek_payloads.get((_strike_from_row(row), option_type), {})
            quote_iv = _first(payload, "impliedVolatility", "impliedvolatility", "iv")
            if greek_payloads and not greek:
                quality_flags["missing_option_greek_payload"] = True
            quotes.append(
                OptionQuote(
                    snapshot_ts=snapshot,
                    source_ts=_parse_datetime(
                        _first(
                            payload,
                            "exchFeedTime",
                            "exchangeFeedTime",
                            "exchTradeTime",
                            "exchangeTradeTime",
                            "lastTradeTime",
                        )
                    ),
                    underlying=str(underlying).strip().upper(),
                    expiry=expiry,
                    strike=_strike_from_row(row),
                    option_type=option_type,
                    trading_symbol=symbol,
                    exchange=exchange,
                    symbol_token=str(token) if token is not None else None,
                    provider=self.provider_name,
                    oi=_first(payload, "opnInterest", "openInterest", "oi"),
                    volume=_first(payload, "tradeVolume", "volume", "totTradedQty", "tottrdqty"),
                    iv=quote_iv
                    if quote_iv is not None
                    else _first(greek, "impliedVolatility", "impliedvolatility", "iv"),
                    delta=_first(greek, "delta"),
                    gamma=_first(greek, "gamma"),
                    theta=_first(greek, "theta"),
                    vega=_first(greek, "vega"),
                    bid=_best_bid(payload),
                    ask=_best_ask(payload),
                    ltp=_first(payload, "ltp", "lastTradedPrice", "last_price"),
                    underlying_ltp=_first(payload, "underlyingValue", "underlying_ltp") or underlying_ltp,
                    vix=_first(payload, "vix", "indiaVix", "india_vix") or vix,
                    raw_hash=_raw_hash(payload) if payload else None,
                    quality_flags=quality_flags,
                )
            )
        return quotes

    def _fetch_greek_payloads(
        self,
        *,
        underlying: str,
        expiry: date,
    ) -> dict[tuple[int, str], Mapping[str, Any]]:
        method = getattr(self.quote_fetcher, "fetch_option_greeks", None)
        if not callable(method):
            return {}
        try:
            rows = method(underlying=str(underlying).strip().upper(), expiry=expiry)
        except Exception as exc:
            logger.warning(
                "Angel optionGreek enrichment failed underlying=%s expiry=%s error=%s",
                str(underlying).strip().upper(),
                expiry.isoformat(),
                type(exc).__name__,
            )
            return {}
        return _greek_payloads_by_contract(rows, expiry=expiry)

    def _fetch_quote_payloads(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        mode: str = "FULL",
    ) -> dict[tuple[str, str], Mapping[str, Any]]:
        fetched: dict[tuple[str, str], Mapping[str, Any]] = {}
        for exchange, tokens in _batched_exchange_tokens(rows, self.batch_size):
            payloads = self._call_quote_fetcher({exchange: tokens}, mode=mode)
            for payload in payloads:
                token = str(
                    _first(payload, "symbolToken", "symboltoken", "token") or ""
                ).strip()
                payload_exchange = str(
                    _first(payload, "exchange", "exch_seg") or exchange
                ).strip().upper()
                if token:
                    fetched[(payload_exchange, token)] = payload
        return fetched

    def _fetch_context_payloads(self, underlying: str) -> dict[str, Mapping[str, Any]]:
        rows = _context_rows_for_underlying(self.scrip_master, underlying=underlying)
        payloads = self._fetch_quote_payloads([row for _, row in rows], mode="LTP")
        out: dict[str, Mapping[str, Any]] = {}
        for label, row in rows:
            token = str(_row_value(row, "token", "symboltoken", "symbolToken") or "").strip()
            exchange = str(_row_value(row, "exch_seg", "exchange") or "NSE").strip().upper()
            payload = payloads.get((exchange, token))
            if payload is not None:
                out[label] = payload
        return out

    def _call_quote_fetcher(
        self,
        exchange_to_tokens: dict[str, list[str]],
        *,
        mode: str = "FULL",
    ) -> Sequence[Mapping[str, Any]]:
        method = getattr(self.quote_fetcher, "fetch_market_quotes", None)
        if callable(method):
            return method(mode=mode, exchange_to_tokens=exchange_to_tokens)
        if callable(self.quote_fetcher):
            return self.quote_fetcher(mode=mode, exchange_to_tokens=exchange_to_tokens)
        raise TypeError("quote_fetcher must expose fetch_market_quotes or be callable")


def _option_rows_for_expiry(
    scrip_master: Any,
    *,
    underlying: str,
    expiry: date,
    option_segments: Sequence[str],
) -> list[Mapping[str, Any]]:
    base = str(underlying or "").strip().upper()
    segments = {str(seg).strip().upper() for seg in option_segments}
    rows = []
    for row in _records(scrip_master):
        symbol = str(_row_value(row, "symbol", "tradingsymbol", "trading_symbol") or "").strip()
        if not symbol.upper().startswith(base):
            continue
        if not (symbol.upper().endswith("CE") or symbol.upper().endswith("PE")):
            continue
        exchange = str(_row_value(row, "exch_seg", "exchange") or "").strip().upper()
        if exchange not in segments:
            continue
        row_expiry = _parse_date(_row_value(row, "expiry", "expiry_dt"))
        if row_expiry != expiry:
            continue
        token = str(_row_value(row, "token", "symboltoken", "symbolToken") or "").strip()
        if not token:
            continue
        rows.append(row)
    return sorted(rows, key=lambda r: (_strike_from_row(r), _option_type_from_symbol(str(_row_value(r, "symbol") or ""))))


def listed_option_expiries(
    scrip_master: Any,
    *,
    underlying: str,
    option_segments: Sequence[str] = ("NFO", "NSEFO"),
    on_or_after: date | None = None,
) -> list[date]:
    """Return provider-listed option expiries for an underlying."""
    base = str(underlying or "").strip().upper()
    segments = {str(seg).strip().upper() for seg in option_segments}
    expiries: set[date] = set()
    for row in _records(scrip_master):
        symbol = str(_row_value(row, "symbol", "tradingsymbol", "trading_symbol") or "").strip()
        if not symbol.upper().startswith(base):
            continue
        if not (symbol.upper().endswith("CE") or symbol.upper().endswith("PE")):
            continue
        exchange = str(_row_value(row, "exch_seg", "exchange") or "").strip().upper()
        if exchange not in segments:
            continue
        parsed = _parse_date(_row_value(row, "expiry", "expiry_dt"))
        if parsed is None:
            continue
        if on_or_after is not None and parsed < on_or_after:
            continue
        expiries.add(parsed)
    return sorted(expiries)


def next_listed_option_expiry(
    scrip_master: Any,
    *,
    underlying: str,
    option_segments: Sequence[str] = ("NFO", "NSEFO"),
    on_or_after: date,
) -> date | None:
    """Return the first listed expiry on or after the supplied date."""
    expiries = listed_option_expiries(
        scrip_master,
        underlying=underlying,
        option_segments=option_segments,
        on_or_after=on_or_after,
    )
    return expiries[0] if expiries else None


def _context_rows_for_underlying(
    scrip_master: Any,
    *,
    underlying: str,
) -> list[tuple[str, Mapping[str, Any]]]:
    base = str(underlying or "").strip().upper()
    underlying_row: Mapping[str, Any] | None = None
    vix_row: Mapping[str, Any] | None = None
    for row in _records(scrip_master):
        symbol = str(_row_value(row, "symbol", "tradingsymbol", "trading_symbol") or "").strip()
        name = str(_row_value(row, "name") or "").strip()
        exchange = str(_row_value(row, "exch_seg", "exchange") or "").strip().upper()
        if exchange not in {"NSE", "INDICES"}:
            continue
        upper_symbol = symbol.upper()
        upper_name = name.upper()
        if vix_row is None and ("INDIA VIX" in upper_symbol or "INDIA VIX" in upper_name):
            vix_row = row
        if underlying_row is None and _is_underlying_index_row(
            upper_symbol,
            upper_name,
            base=base,
        ):
            underlying_row = row
        if underlying_row is not None and vix_row is not None:
            break

    rows: list[tuple[str, Mapping[str, Any]]] = []
    if underlying_row is not None:
        rows.append(("underlying", underlying_row))
    if vix_row is not None:
        rows.append(("vix", vix_row))
    return rows


def _is_underlying_index_row(symbol: str, name: str, *, base: str) -> bool:
    if base == "NIFTY":
        return symbol in {"NIFTY", "NIFTY 50"} or name in {"NIFTY", "NIFTY 50"}
    return symbol == base or name == base


def _batched_exchange_tokens(
    rows: Sequence[Mapping[str, Any]],
    batch_size: int,
) -> list[tuple[str, list[str]]]:
    by_exchange: dict[str, list[str]] = {}
    for row in rows:
        exchange = str(_row_value(row, "exch_seg", "exchange") or "NFO").strip().upper()
        token = str(_row_value(row, "token", "symboltoken", "symbolToken") or "").strip()
        if token:
            by_exchange.setdefault(exchange, []).append(token)

    batches: list[tuple[str, list[str]]] = []
    size = max(1, int(batch_size))
    for exchange, tokens in sorted(by_exchange.items()):
        unique_tokens = list(dict.fromkeys(tokens))
        for idx in range(0, len(unique_tokens), size):
            batches.append((exchange, unique_tokens[idx : idx + size]))
    return batches


def _records(scrip_master: Any) -> list[Mapping[str, Any]]:
    if hasattr(scrip_master, "to_dict"):
        try:
            records = scrip_master.to_dict("records")
            return [row for row in records if isinstance(row, Mapping)]
        except Exception:
            pass
    if isinstance(scrip_master, Sequence) and not isinstance(scrip_master, (str, bytes)):
        return [row for row in scrip_master if isinstance(row, Mapping)]
    return []


def _row_value(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def _first(payload: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload and payload[key] not in (None, ""):
            return payload[key]
    return None


def _parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%d%b%Y", "%Y-%m-%d", "%d-%b-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _aware_source_time(value)
    text = str(value).strip().replace("Z", "+00:00")
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d%b%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return _aware_source_time(datetime.strptime(text, fmt))
        except ValueError:
            continue
    try:
        return _aware_source_time(datetime.fromisoformat(text))
    except ValueError:
        return None


def _strike_from_row(row: Mapping[str, Any]) -> int:
    raw = _row_value(row, "strike", "strike_price", "strikePrice")
    value = _decimal(raw)
    if value is None:
        return 0
    if value > Decimal("100000"):
        value = value / Decimal("100")
    return int(value.to_integral_value())


def _option_type_from_symbol(symbol: str) -> str:
    upper = str(symbol or "").strip().upper()
    if upper.endswith("PE"):
        return "PE"
    return "CE"


def _best_bid(payload: Mapping[str, Any]) -> Any:
    direct = _first(payload, "bid", "bestBid", "bidPrice", "buyPrice")
    if direct is not None:
        return direct
    depth = payload.get("depth")
    if isinstance(depth, Mapping):
        buy = depth.get("buy")
        if isinstance(buy, Sequence) and buy:
            level = buy[0]
            if isinstance(level, Mapping):
                return _first(level, "price", "bid", "rate")
    return None


def _best_ask(payload: Mapping[str, Any]) -> Any:
    direct = _first(payload, "ask", "bestAsk", "askPrice", "sellPrice")
    if direct is not None:
        return direct
    depth = payload.get("depth")
    if isinstance(depth, Mapping):
        sell = depth.get("sell")
        if isinstance(sell, Sequence) and sell:
            level = sell[0]
            if isinstance(level, Mapping):
                return _first(level, "price", "ask", "rate")
    return None


def _ltp_from_payload(payload: Mapping[str, Any] | None) -> Any:
    if not payload:
        return None
    return _first(payload, "ltp", "lastTradedPrice", "last_price")


def _greek_payloads_by_contract(
    rows: Sequence[Mapping[str, Any]],
    *,
    expiry: date,
) -> dict[tuple[int, str], Mapping[str, Any]]:
    mapped: dict[tuple[int, str], Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        row_expiry = _parse_date(_first(row, "expiry", "expiryDate", "expirydate"))
        if row_expiry is not None and row_expiry != expiry:
            continue
        strike = _strike_from_greek(row)
        option_type = str(_first(row, "optionType", "option_type") or "").strip().upper()
        if strike <= 0 or option_type not in {"CE", "PE"}:
            continue
        mapped[(strike, option_type)] = row
    return mapped


def _strike_from_greek(row: Mapping[str, Any]) -> int:
    raw = _first(row, "strikePrice", "strike", "strike_price")
    value = _decimal(raw)
    if value is None:
        return 0
    if value > Decimal("100000"):
        value = value / Decimal("100")
    return int(value.to_integral_value())


def _raw_hash(payload: Mapping[str, Any]) -> str:
    text = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _aware_source_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=IST).astimezone(timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "AngelOptionChainProvider",
    "MarketQuoteFetcher",
    "listed_option_expiries",
    "next_listed_option_expiry",
]
