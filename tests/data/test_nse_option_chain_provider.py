from __future__ import annotations

from datetime import date, datetime

from app.data.nse_option_chain_provider import (
    NseOptionChainProvider,
    parse_nse_option_chain_payload,
)


def _payload():
    return {
        "records": {
            "timestamp": "18-May-2026 15:30:00",
            "underlyingValue": 23649.95,
            "data": [
                {
                    "strikePrice": 23600,
                    "expiryDate": "19-May-2026",
                    "CE": {
                        "identifier": "OPTIDXNIFTY19-05-2026CE23600.00",
                        "openInterest": 1250,
                        "changeinOpenInterest": 100,
                        "totalTradedVolume": 3210,
                        "impliedVolatility": 11.75,
                        "lastPrice": 82.4,
                        "bidprice": 81.8,
                        "askPrice": 82.7,
                    },
                    "PE": {
                        "identifier": "OPTIDXNIFTY19-05-2026PE23600.00",
                        "openInterest": 8420,
                        "totalTradedVolume": 6432,
                        "impliedVolatility": 10.6,
                        "lastPrice": 61.25,
                        "bidprice": 60.9,
                        "askPrice": 61.6,
                    },
                },
                {
                    "strikePrice": 23600,
                    "expiryDate": "26-May-2026",
                    "CE": {"openInterest": 1},
                },
            ],
        }
    }


def test_parse_nse_option_chain_payload_maps_ce_and_pe_quotes():
    quotes = parse_nse_option_chain_payload(
        _payload(),
        underlying="nifty",
        expiry=date(2026, 5, 19),
        snapshot_ts=datetime(2026, 5, 18, 15, 30),
    )

    assert len(quotes) == 2
    ce, pe = [quote.normalized() for quote in quotes]
    assert ce.provider == "nse_web"
    assert ce.underlying == "NIFTY"
    assert ce.strike == 23600
    assert ce.option_type == "CE"
    assert ce.symbol_token == "OPTIDXNIFTY19-05-2026CE23600.00"
    assert ce.oi == 1250
    assert ce.volume == 3210
    assert str(ce.iv) == "11.75"
    assert str(ce.bid) == "81.8"
    assert str(ce.ask) == "82.7"
    assert str(ce.ltp) == "82.4"
    assert str(ce.underlying_ltp) == "23649.95"
    assert ce.source_ts.isoformat() == "2026-05-18T10:00:00+00:00"
    assert ce.quality_flags["validation_source_only"] is True
    assert pe.option_type == "PE"
    assert pe.oi == 8420


def test_nse_provider_uses_client_payload_and_filters_expiry():
    class FakeClient:
        def __init__(self):
            self.calls = []

        def fetch_option_chain(self, *, symbol):
            self.calls.append(symbol)
            return _payload()

    client = FakeClient()
    provider = NseOptionChainProvider(client)

    quotes = provider.fetch_chain(
        underlying="NIFTY",
        expiry=date(2026, 5, 19),
        snapshot_ts=datetime(2026, 5, 18, 15, 30),
    )

    assert client.calls == ["NIFTY"]
    assert len(quotes) == 2
