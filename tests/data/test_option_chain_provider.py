from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.data.option_chain_provider import (
    OptionQuote,
    is_quote_usable_for_live_entry,
    quality_flags_for_quote,
)


def _quote(**overrides):
    base = {
        "snapshot_ts": datetime(2026, 5, 19, 10, 0, tzinfo=timezone.utc),
        "source_ts": datetime(2026, 5, 19, 9, 59, tzinfo=timezone.utc),
        "underlying": "nifty",
        "expiry": date(2026, 5, 19),
        "strike": 25200,
        "option_type": "ce",
        "trading_symbol": "NIFTY19MAY2625200CE",
        "exchange": "nfo",
        "symbol_token": "12345",
        "provider": "Angel",
        "oi": "120000",
        "volume": "1500",
        "iv": "11.25",
        "delta": "0.42",
        "gamma": "0.0012",
        "theta": "-3.4",
        "vega": "9.8",
        "bid": "42.5",
        "ask": "43.0",
        "ltp": "42.8",
    }
    base.update(overrides)
    return OptionQuote(**base)


def test_option_quote_normalizes_contract_fields_and_numbers():
    quote = _quote().normalized()

    assert quote.underlying == "NIFTY"
    assert quote.option_type == "CE"
    assert quote.exchange == "NFO"
    assert quote.provider == "angel"
    assert quote.oi == 120000
    assert quote.iv == Decimal("11.25")
    assert quote.delta == Decimal("0.42")
    assert quote.gamma == Decimal("0.0012")
    assert quote.theta == Decimal("-3.4")
    assert quote.vega == Decimal("9.8")
    assert quote.bid == Decimal("42.5")


def test_quality_flags_mark_quote_unusable_when_symbol_token_missing():
    quote = _quote(symbol_token=None)

    assert quality_flags_for_quote(quote)["missing_symbol_token"] is True
    assert is_quote_usable_for_live_entry(quote) is False


def test_quality_flags_detect_missing_required_fields_and_bad_bid_ask():
    quote = _quote(oi=None, bid="44.0", ask="43.0")
    flags = quality_flags_for_quote(quote)

    assert flags["missing_required_fields"] == ["oi"]
    assert flags["bad_bid_ask"] is True
    assert is_quote_usable_for_live_entry(quote) is False


def test_missing_iv_is_optional_for_live_entry_but_still_reported():
    quote = _quote(iv=None)
    flags = quality_flags_for_quote(quote)

    assert "missing_required_fields" not in flags
    assert flags["missing_optional_fields"] == ["iv"]
    assert is_quote_usable_for_live_entry(quote) is True


def test_old_iv_only_required_flag_is_recomputed_as_optional():
    quote = _quote(
        iv=None,
        quality_flags={"missing_required_fields": ["iv"]},
    )
    flags = quality_flags_for_quote(quote)

    assert "missing_required_fields" not in flags
    assert flags["missing_optional_fields"] == ["iv"]
    assert is_quote_usable_for_live_entry(quote) is True


def test_quality_flags_detect_stale_source_timestamp():
    quote = _quote(source_ts=datetime(2026, 5, 19, 9, 55, tzinfo=timezone.utc))
    flags = quality_flags_for_quote(quote, max_source_lag_seconds=120)

    assert flags["stale_source_seconds"] == 300
    assert is_quote_usable_for_live_entry(quote) is False


def test_quality_flags_detect_future_source_timestamp():
    quote = _quote(source_ts=_quote().snapshot_ts + timedelta(seconds=30))
    flags = quality_flags_for_quote(quote)

    assert flags["future_source_seconds"] == 30
    assert "stale_source_seconds" not in flags
    assert is_quote_usable_for_live_entry(quote) is False


def test_complete_quote_is_usable_for_live_entry():
    assert quality_flags_for_quote(_quote()) == {}
    assert is_quote_usable_for_live_entry(_quote()) is True
