"""Angel SmartAPI option-chain provider backed by scrip-master tokens."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any, Protocol

from app.data.option_chain_provider import OptionQuote


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
                    option_type=_option_type_from_symbol(symbol),
                    trading_symbol=symbol,
                    exchange=exchange,
                    symbol_token=str(token) if token is not None else None,
                    provider=self.provider_name,
                    oi=_first(payload, "opnInterest", "openInterest", "oi"),
                    volume=_first(payload, "tradeVolume", "volume", "totTradedQty", "tottrdqty"),
                    iv=_first(payload, "impliedVolatility", "impliedvolatility", "iv"),
                    bid=_best_bid(payload),
                    ask=_best_ask(payload),
                    ltp=_first(payload, "ltp", "lastTradedPrice", "last_price"),
                    underlying_ltp=_first(payload, "underlyingValue", "underlying_ltp"),
                    raw_hash=_raw_hash(payload) if payload else None,
                    quality_flags=quality_flags,
                )
            )
        return quotes

    def _fetch_quote_payloads(
        self,
        rows: Sequence[Mapping[str, Any]],
    ) -> dict[tuple[str, str], Mapping[str, Any]]:
        fetched: dict[tuple[str, str], Mapping[str, Any]] = {}
        for exchange, tokens in _batched_exchange_tokens(rows, self.batch_size):
            payloads = self._call_quote_fetcher({exchange: tokens})
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

    def _call_quote_fetcher(
        self,
        exchange_to_tokens: dict[str, list[str]],
    ) -> Sequence[Mapping[str, Any]]:
        method = getattr(self.quote_fetcher, "fetch_market_quotes", None)
        if callable(method):
            return method(mode="FULL", exchange_to_tokens=exchange_to_tokens)
        if callable(self.quote_fetcher):
            return self.quote_fetcher(mode="FULL", exchange_to_tokens=exchange_to_tokens)
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
        return _aware_utc(value)
    text = str(value).strip().replace("Z", "+00:00")
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d%b%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        return _aware_utc(datetime.fromisoformat(text))
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


__all__ = [
    "AngelOptionChainProvider",
    "MarketQuoteFetcher",
]
