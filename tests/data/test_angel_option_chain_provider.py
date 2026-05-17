from __future__ import annotations

from datetime import date, datetime

from app.data.angel_option_chain_provider import AngelOptionChainProvider


class FakeQuoteFetcher:
    def __init__(self):
        self.calls = []

    def fetch_market_quotes(self, *, mode, exchange_to_tokens):
        self.calls.append((mode, exchange_to_tokens))
        payloads = {
            "111": {
                "exchange": "NFO",
                "symbolToken": "111",
                "ltp": "42.8",
                "opnInterest": "120000",
                "tradeVolume": "1500",
                "impliedVolatility": "11.25",
                "depth": {
                    "buy": [{"price": "42.5"}],
                    "sell": [{"price": "43.0"}],
                },
                "exchFeedTime": "2026-05-19T10:00:00+05:30",
            },
            "222": {
                "exchange": "NFO",
                "symbolToken": "222",
                "ltp": "38.2",
                "opnInterest": "90000",
                "tradeVolume": "1200",
                "impliedVolatility": "12.1",
                "bid": "38.0",
                "ask": "38.4",
            },
        }
        tokens = exchange_to_tokens["NFO"]
        return [payloads[token] for token in tokens if token in payloads]


def test_angel_provider_maps_scrip_master_and_full_quotes_to_option_quotes():
    fetcher = FakeQuoteFetcher()
    scrip_master = [
        {
            "symbol": "NIFTY19MAY2625200CE",
            "expiry": "19MAY2026",
            "strike": "2520000",
            "exch_seg": "NFO",
            "token": "111",
        },
        {
            "symbol": "NIFTY19MAY2625200PE",
            "expiry": "19MAY2026",
            "strike": "2520000",
            "exch_seg": "NFO",
            "token": "222",
        },
        {
            "symbol": "BANKNIFTY19MAY2653000CE",
            "expiry": "19MAY2026",
            "strike": "5300000",
            "exch_seg": "NFO",
            "token": "333",
        },
    ]
    provider = AngelOptionChainProvider(fetcher, scrip_master, batch_size=10)

    quotes = provider.fetch_chain(
        underlying="NIFTY",
        expiry=date(2026, 5, 19),
        snapshot_ts=datetime(2026, 5, 19, 4, 30),
    )

    assert fetcher.calls == [("FULL", {"NFO": ["111", "222"]})]
    assert len(quotes) == 2
    ce = quotes[0].normalized()
    assert ce.underlying == "NIFTY"
    assert ce.strike == 25200
    assert ce.option_type == "CE"
    assert ce.symbol_token == "111"
    assert ce.oi == 120000
    assert str(ce.bid) == "42.5"
    assert str(ce.ask) == "43.0"
    assert ce.raw_hash


def test_angel_provider_preserves_contract_with_missing_quote_payload_flag():
    fetcher = FakeQuoteFetcher()
    provider = AngelOptionChainProvider(
        fetcher,
        [
            {
                "symbol": "NIFTY19MAY2625200CE",
                "expiry": "19MAY2026",
                "strike": "2520000",
                "exch_seg": "NFO",
                "token": "111",
            },
            {
                "symbol": "NIFTY19MAY2625300CE",
                "expiry": "19MAY2026",
                "strike": "2530000",
                "exch_seg": "NFO",
                "token": "999",
            },
        ],
    )

    quotes = provider.fetch_chain(
        underlying="NIFTY",
        expiry=date(2026, 5, 19),
        snapshot_ts=datetime(2026, 5, 19, 4, 30),
    )

    missing = [quote for quote in quotes if quote.symbol_token == "999"][0]
    assert missing.quality_flags["missing_quote_payload"] is True
    assert missing.ltp is None
