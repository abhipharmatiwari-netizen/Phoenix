"""Option-chain ingestion contracts for OI/ML strategies.

This module intentionally contains no broker calls. Providers adapt their raw
payloads into ``OptionQuote`` rows; downstream guards can then reason about
freshness and completeness without knowing which vendor produced the data.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Protocol, Sequence


VALID_OPTION_TYPES = frozenset({"CE", "PE"})
REQUIRED_QUOTE_FIELDS = (
    "oi",
    "volume",
    "bid",
    "ask",
    "ltp",
)
OPTIONAL_QUOTE_FIELDS = (
    "iv",
)


class OptionChainProvider(Protocol):
    """Provider interface for one option-chain snapshot."""

    provider_name: str

    def fetch_chain(
        self,
        *,
        underlying: str,
        expiry: date,
        snapshot_ts: datetime,
    ) -> Sequence["OptionQuote"]:
        """Return normalized quotes for ``underlying`` and ``expiry``."""


@dataclass(frozen=True)
class OptionQuote:
    """Normalized option-chain quote at a single snapshot timestamp."""

    snapshot_ts: datetime
    underlying: str
    expiry: date
    strike: int
    option_type: str
    trading_symbol: str
    exchange: str
    provider: str
    source_ts: datetime | None = None
    ingested_at: datetime | None = None
    symbol_token: str | None = None
    oi: int | None = None
    volume: int | None = None
    iv: Decimal | float | int | str | None = None
    delta: Decimal | float | int | str | None = None
    gamma: Decimal | float | int | str | None = None
    theta: Decimal | float | int | str | None = None
    vega: Decimal | float | int | str | None = None
    bid: Decimal | float | int | str | None = None
    ask: Decimal | float | int | str | None = None
    ltp: Decimal | float | int | str | None = None
    underlying_ltp: Decimal | float | int | str | None = None
    vix: Decimal | float | int | str | None = None
    raw_hash: str | None = None
    quality_flags: Mapping[str, Any] = field(default_factory=dict)

    def normalized(self) -> "OptionQuote":
        """Return a canonical copy suitable for persistence."""
        ingested_at = self.ingested_at or datetime.now(timezone.utc)
        return replace(
            self,
            snapshot_ts=_aware_utc(self.snapshot_ts),
            source_ts=_aware_utc(self.source_ts) if self.source_ts else None,
            ingested_at=_aware_utc(ingested_at),
            underlying=str(self.underlying or "").strip().upper(),
            option_type=str(self.option_type or "").strip().upper(),
            trading_symbol=str(self.trading_symbol or "").strip(),
            exchange=str(self.exchange or "").strip().upper(),
            provider=str(self.provider or "").strip().lower(),
            symbol_token=(str(self.symbol_token).strip() or None)
            if self.symbol_token is not None
            else None,
            strike=int(self.strike),
            oi=_optional_int(self.oi),
            volume=_optional_int(self.volume),
            iv=_optional_decimal(self.iv),
            delta=_optional_decimal(self.delta),
            gamma=_optional_decimal(self.gamma),
            theta=_optional_decimal(self.theta),
            vega=_optional_decimal(self.vega),
            bid=_optional_decimal(self.bid),
            ask=_optional_decimal(self.ask),
            ltp=_optional_decimal(self.ltp),
            underlying_ltp=_optional_decimal(self.underlying_ltp),
            vix=_optional_decimal(self.vix),
            raw_hash=(str(self.raw_hash).strip() or None)
            if self.raw_hash is not None
            else None,
            quality_flags=dict(self.quality_flags or {}),
        )


def quality_flags_for_quote(
    quote: OptionQuote,
    *,
    max_source_lag_seconds: int = 120,
    max_future_source_seconds: int = 65,
) -> dict[str, Any]:
    """Compute data-quality flags without rejecting the quote."""
    q = quote.normalized()
    missing: list[str] = []
    if not q.underlying:
        missing.append("underlying")
    if not q.trading_symbol:
        missing.append("trading_symbol")
    if not q.exchange:
        missing.append("exchange")
    if not q.provider:
        missing.append("provider")
    for field_name in REQUIRED_QUOTE_FIELDS:
        if getattr(q, field_name) is None:
            missing.append(field_name)

    flags: dict[str, Any] = dict(q.quality_flags or {})
    if missing:
        flags["missing_required_fields"] = sorted(set(missing))
    else:
        flags.pop("missing_required_fields", None)

    optional_missing = [
        field_name
        for field_name in OPTIONAL_QUOTE_FIELDS
        if getattr(q, field_name) is None
    ]
    if optional_missing:
        flags["missing_optional_fields"] = sorted(set(optional_missing))
    else:
        flags.pop("missing_optional_fields", None)

    if not q.symbol_token:
        flags["missing_symbol_token"] = True
    else:
        flags.pop("missing_symbol_token", None)

    if q.option_type not in VALID_OPTION_TYPES:
        flags["invalid_option_type"] = q.option_type
    else:
        flags.pop("invalid_option_type", None)

    if q.bid is not None and q.ask is not None and q.ask < q.bid:
        flags["bad_bid_ask"] = True
    else:
        flags.pop("bad_bid_ask", None)

    if q.source_ts is not None:
        lag_seconds = (q.snapshot_ts - q.source_ts).total_seconds()
        if lag_seconds < -max(0, int(max_future_source_seconds)):
            flags["future_source_seconds"] = int(abs(lag_seconds))
            flags.pop("stale_source_seconds", None)
        elif lag_seconds > max_source_lag_seconds:
            flags["stale_source_seconds"] = int(lag_seconds)
            flags.pop("future_source_seconds", None)
        else:
            flags.pop("stale_source_seconds", None)
            flags.pop("future_source_seconds", None)
    return flags


def is_quote_usable_for_live_entry(quote: OptionQuote) -> bool:
    """Return True only when the quote is complete enough for live gates."""
    flags = quality_flags_for_quote(quote)
    hard_flags = {
        "missing_required_fields",
        "missing_symbol_token",
        "invalid_option_type",
        "bad_bid_ask",
        "future_source_seconds",
        "stale_source_seconds",
    }
    return not any(name in flags for name in hard_flags)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except Exception:
        try:
            return int(float(value))
        except Exception:
            return None


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


__all__ = [
    "OptionChainProvider",
    "OptionQuote",
    "OPTIONAL_QUOTE_FIELDS",
    "REQUIRED_QUOTE_FIELDS",
    "VALID_OPTION_TYPES",
    "is_quote_usable_for_live_entry",
    "quality_flags_for_quote",
]
