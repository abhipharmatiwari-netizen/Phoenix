from __future__ import annotations

from datetime import date, datetime
import json
import logging
from urllib.error import HTTPError

from app.data.nse_option_chain_provider import (
    NSE_LIVE_EQUITY_SOURCE,
    NseOptionChainProvider,
    NseWebOptionChainClient,
    classify_nse_reference_error,
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


def test_parse_nse_live_equity_derivatives_fallback_payload():
    payload = {
        "__phoenix_nse_source": NSE_LIVE_EQUITY_SOURCE,
        "timestamp": "22-May-2026 15:30:00",
        "data": [
            {
                "underlying": "NIFTY",
                "expiryDate": "26-May-2026",
                "identifier": "OPTIDXNIFTY26-05-2026CE23800.00",
                "strikePrice": 23800,
                "optionType": "Call",
                "openInterest": 109580,
                "volume": 331429735,
                "lastPrice": 128.4,
                "underlyingValue": 23719.3,
            },
            {
                "underlying": "NIFTY",
                "expiryDate": "26-May-2026",
                "identifier": "OPTIDXNIFTY26-05-2026PE23800.00",
                "strikePrice": 23800,
                "optionType": "Put",
                "openInterest": 67131,
                "volume": 256472515,
                "lastPrice": 149.1,
                "underlyingValue": 23719.3,
            },
            {
                "underlying": "NIFTY",
                "expiryDate": "02-Jun-2026",
                "strikePrice": 23800,
                "optionType": "Call",
            },
        ],
    }

    quotes = parse_nse_option_chain_payload(
        payload,
        underlying="NIFTY",
        expiry=date(2026, 5, 26),
        snapshot_ts=datetime(2026, 5, 22, 15, 30),
    )

    assert len(quotes) == 2
    ce, pe = [quote.normalized() for quote in quotes]
    assert ce.option_type == "CE"
    assert ce.symbol_token == "OPTIDXNIFTY26-05-2026CE23800.00"
    assert ce.oi == 7122700
    assert ce.volume == 331429735
    assert str(ce.ltp) == "128.4"
    assert str(ce.underlying_ltp) == "23719.3"
    assert ce.bid is None
    assert ce.ask is None
    assert ce.iv is None
    assert ce.source_ts.isoformat() == "2026-05-22T10:00:00+00:00"
    assert ce.quality_flags["nse_source"] == NSE_LIVE_EQUITY_SOURCE
    assert ce.quality_flags["nse_open_interest_unit"] == "underlying_units"
    assert ce.quality_flags["nse_open_interest_lot_size"] == 65
    assert ce.quality_flags["reference_contract_coverage"] == "partial"
    assert ce.quality_flags["missing_reference_fields_expected"] == ["ask", "bid", "iv"]
    assert ce.quality_flags["non_equivalent_reference_fields_expected"] == [
        "ltp",
        "oi",
        "volume",
    ]
    assert pe.option_type == "PE"


def test_web_client_falls_back_to_live_equity_derivatives_when_option_chain_empty():
    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def read(self):
            return json.dumps(self.payload).encode("utf-8")

        def close(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

    class FakeOpener:
        def __init__(self):
            self.urls = []

        def open(self, request, timeout):
            self.urls.append(request.full_url)
            if "option-chain-indices" in request.full_url:
                return FakeResponse({})
            if "liveEquity-derivatives" in request.full_url:
                return FakeResponse(
                    {
                        "timestamp": "22-May-2026 15:30:00",
                        "data": [
                            {
                                "underlying": "NIFTY",
                                "expiryDate": "26-May-2026",
                                "strikePrice": 23800,
                                "optionType": "Call",
                            }
                        ],
                    }
                )
            return FakeResponse({"page": True})

    opener = FakeOpener()
    client = NseWebOptionChainClient(opener_factory=lambda: opener)

    payload = client.fetch_option_chain(symbol="nifty")

    assert payload["__phoenix_nse_source"] == NSE_LIVE_EQUITY_SOURCE
    assert payload["__phoenix_nse_symbol"] == "NIFTY"
    assert any("option-chain-indices" in url for url in opener.urls)
    assert any("liveEquity-derivatives" in url for url in opener.urls)


def test_web_client_falls_back_to_live_equity_derivatives_when_option_chain_errors(caplog):
    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def read(self):
            return json.dumps(self.payload).encode("utf-8")

        def close(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

    class FakeOpener:
        def __init__(self):
            self.urls = []

        def open(self, request, timeout):
            self.urls.append(request.full_url)
            if "option-chain-indices" in request.full_url:
                raise HTTPError(request.full_url, 404, "Not Found", None, None)
            if "liveEquity-derivatives" in request.full_url:
                return FakeResponse(
                    {
                        "timestamp": "22-May-2026 15:30:00",
                        "data": [
                            {
                                "underlying": "NIFTY",
                                "expiryDate": "26-May-2026",
                                "strikePrice": 23800,
                                "optionType": "Call",
                            }
                        ],
                    }
                )
            return FakeResponse({"page": True})

    opener = FakeOpener()
    client = NseWebOptionChainClient(opener_factory=lambda: opener)

    caplog.set_level(logging.INFO)
    payload = client.fetch_option_chain(symbol="nifty")

    assert payload["__phoenix_nse_source"] == NSE_LIVE_EQUITY_SOURCE
    assert any("option-chain-indices" in url for url in opener.urls)
    assert any("liveEquity-derivatives" in url for url in opener.urls)
    assert "using live-derivatives fallback" in caplog.text
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


def test_web_client_retries_timeout_before_recording_reference_failure():
    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def read(self):
            return json.dumps(self.payload).encode("utf-8")

        def close(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

    class FakeOpener:
        def __init__(self):
            self.urls = []
            self.live_calls = 0

        def open(self, request, timeout):
            self.urls.append(request.full_url)
            if "option-chain-indices" in request.full_url:
                raise TimeoutError("The read operation timed out")
            if "liveEquity-derivatives" in request.full_url:
                self.live_calls += 1
                if self.live_calls == 1:
                    raise TimeoutError("The read operation timed out")
                return FakeResponse(
                    {
                        "timestamp": "22-May-2026 15:30:00",
                        "data": [
                            {
                                "underlying": "NIFTY",
                                "expiryDate": "26-May-2026",
                                "strikePrice": 23800,
                                "optionType": "Call",
                            }
                        ],
                    }
                )
            return FakeResponse({"page": True})

    opener = FakeOpener()
    sleeps = []
    client = NseWebOptionChainClient(
        max_attempts=2,
        retry_backoff_seconds=0.1,
        retry_jitter_seconds=0,
        opener_factory=lambda: opener,
        sleep=sleeps.append,
    )

    payload = client.fetch_option_chain(symbol="NIFTY")

    assert payload["__phoenix_nse_source"] == NSE_LIVE_EQUITY_SOURCE
    assert opener.live_calls == 2
    assert sleeps == [0.1]
    assert classify_nse_reference_error(
        RuntimeError("The read operation timed out")
    ) == "provider_timeout"
