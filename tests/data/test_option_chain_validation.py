from __future__ import annotations

from datetime import date, datetime, timezone

from app.data.option_chain_provider import OptionQuote
from app.data.option_chain_validation import (
    OptionChainValidationConfig,
    compare_angel_to_nse,
    expected_missing_reference_fields,
)


def _quote(**overrides):
    base = {
        "snapshot_ts": datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
        "source_ts": datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
        "underlying": "NIFTY",
        "expiry": date(2026, 5, 19),
        "strike": 23600,
        "option_type": "CE",
        "trading_symbol": "NIFTY19MAY2623600CE",
        "exchange": "NFO",
        "symbol_token": "23600CE",
        "provider": "angel",
        "oi": 1200,
        "volume": 5000,
        "iv": "12.10",
        "bid": "80.00",
        "ask": "80.50",
        "ltp": "80.25",
        "underlying_ltp": "23649.95",
        "vix": "17.5",
    }
    base.update(overrides)
    return OptionQuote(**base)


def test_compare_angel_to_nse_accepts_values_inside_tolerance():
    angel = [_quote()]
    nse = [
        _quote(
            provider="nse_web",
            oi=1200,
            volume=5100,
            iv="12.15",
            bid="80.05",
            ask="80.55",
            ltp="80.30",
        )
    ]

    report = compare_angel_to_nse(angel, nse)

    assert report.ok is True
    assert report.compared_contracts == 1
    assert report.to_dict()["mismatches"] == []


def test_compare_angel_to_nse_reports_missing_contracts_and_field_diffs():
    angel = [
        _quote(),
        _quote(strike=23700, option_type="PE", trading_symbol="NIFTY19MAY2623700PE"),
    ]
    nse = [
        _quote(provider="nse_web", oi=1300, iv="13.40", ltp="84.00"),
        _quote(
            provider="nse_web",
            strike=23800,
            option_type="CE",
            trading_symbol="NIFTY19MAY2623800CE",
        ),
    ]

    report = compare_angel_to_nse(angel, nse, metadata={"source": "test"})
    payload = report.to_dict()

    assert report.ok is False
    assert report.compared_contracts == 1
    assert payload["angel_only_contracts"] == [{"strike": 23700, "option_type": "PE"}]
    assert payload["nse_only_contracts"] == [{"strike": 23800, "option_type": "CE"}]
    assert payload["mismatches"][0]["field_diffs"][0]["field"] == "oi"
    assert payload["metadata"] == {"source": "test"}


def test_compare_angel_to_nse_counts_missing_iv_by_provider():
    angel = [_quote(iv=None)]
    nse = [_quote(provider="nse_web")]

    report = compare_angel_to_nse(angel, nse)

    assert report.missing_angel_iv == 1
    assert report.missing_nse_iv == 0
    assert report.to_dict()["mismatches"][0]["field_diffs"][0]["field"] == "iv"


def test_compare_angel_to_nse_can_skip_expected_missing_reference_fields():
    angel = [_quote()]
    nse = [
        _quote(
            provider="nse_web",
            iv=None,
            bid=None,
            ask=None,
            quality_flags={
                "missing_reference_fields_expected": ["ask", "bid", "iv"],
            },
        )
    ]
    skipped = expected_missing_reference_fields(nse)

    report = compare_angel_to_nse(
        angel,
        nse,
        config=OptionChainValidationConfig(skip_missing_reference_fields=skipped),
    )

    assert skipped == ("ask", "bid", "iv")
    assert report.ok is True
    assert report.missing_nse_iv == 0
    assert report.to_dict()["mismatches"] == []
