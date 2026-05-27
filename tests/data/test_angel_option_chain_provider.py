from __future__ import annotations

from datetime import date, datetime

from app.data.angel_option_chain_provider import AngelOptionChainProvider, listed_option_expiries


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


def test_angel_provider_stamps_underlying_ltp_and_vix_from_context_quotes():
    class ContextFetcher:
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
                    "bid": "42.5",
                    "ask": "43.0",
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
                "999001": {
                    "exchange": "NSE",
                    "symbolToken": "999001",
                    "ltp": "25075.5",
                },
                "999002": {
                    "exchange": "NSE",
                    "symbolToken": "999002",
                    "ltp": "14.2",
                },
            }
            return [
                payloads[token]
                for tokens in exchange_to_tokens.values()
                for token in tokens
                if token in payloads
            ]

    fetcher = ContextFetcher()
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
                "symbol": "NIFTY19MAY2625200PE",
                "expiry": "19MAY2026",
                "strike": "2520000",
                "exch_seg": "NFO",
                "token": "222",
            },
            {
                "symbol": "Nifty 50",
                "name": "NIFTY",
                "exch_seg": "NSE",
                "token": "999001",
            },
            {
                "symbol": "India VIX",
                "name": "INDIA VIX",
                "exch_seg": "NSE",
                "token": "999002",
            },
        ],
        batch_size=10,
    )

    quotes = provider.fetch_chain(
        underlying="NIFTY",
        expiry=date(2026, 5, 19),
        snapshot_ts=datetime(2026, 5, 19, 4, 30),
    )

    assert fetcher.calls == [
        ("FULL", {"NFO": ["111", "222"]}),
        ("LTP", {"NSE": ["999001", "999002"]}),
    ]
    assert str(quotes[0].normalized().underlying_ltp) == "25075.5"
    assert str(quotes[0].normalized().vix) == "14.2"


def test_angel_provider_enriches_iv_and_greeks_from_option_greek_rest_rows():
    class GreekFetcher(FakeQuoteFetcher):
        def __init__(self):
            super().__init__()
            self.greek_calls = []

        def fetch_market_quotes(self, *, mode, exchange_to_tokens):
            self.calls.append((mode, exchange_to_tokens))
            payloads = {
                "111": {
                    "exchange": "NFO",
                    "symbolToken": "111",
                    "ltp": "42.8",
                    "opnInterest": "120000",
                    "tradeVolume": "1500",
                    "depth": {
                        "buy": [{"price": "42.5"}],
                        "sell": [{"price": "43.0"}],
                    },
                },
                "222": {
                    "exchange": "NFO",
                    "symbolToken": "222",
                    "ltp": "38.2",
                    "opnInterest": "90000",
                    "tradeVolume": "1200",
                    "bid": "38.0",
                    "ask": "38.4",
                },
            }
            return [
                payloads[token]
                for tokens in exchange_to_tokens.values()
                for token in tokens
                if token in payloads
            ]

        def fetch_option_greeks(self, *, underlying, expiry):
            self.greek_calls.append((underlying, expiry))
            return [
                {
                    "expiry": "19MAY2026",
                    "strikePrice": "25200",
                    "optionType": "CE",
                    "impliedVolatility": "13.5",
                    "delta": "0.42",
                    "gamma": "0.0012",
                    "theta": "-3.4",
                    "vega": "9.8",
                },
                {
                    "expiry": "19MAY2026",
                    "strikePrice": "25200",
                    "optionType": "PE",
                    "impliedVolatility": "14.2",
                    "delta": "-0.58",
                    "gamma": "0.0011",
                    "theta": "-3.8",
                    "vega": "9.4",
                },
            ]

    fetcher = GreekFetcher()
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
                "symbol": "NIFTY19MAY2625200PE",
                "expiry": "19MAY2026",
                "strike": "2520000",
                "exch_seg": "NFO",
                "token": "222",
            },
        ],
        batch_size=10,
    )

    quotes = provider.fetch_chain(
        underlying="NIFTY",
        expiry=date(2026, 5, 19),
        snapshot_ts=datetime(2026, 5, 19, 4, 30),
    )

    assert fetcher.greek_calls == [("NIFTY", date(2026, 5, 19))]
    ce = quotes[0].normalized()
    pe = quotes[1].normalized()
    assert str(ce.iv) == "13.5"
    assert str(ce.delta) == "0.42"
    assert str(ce.gamma) == "0.0012"
    assert str(ce.theta) == "-3.4"
    assert str(ce.vega) == "9.8"
    assert str(pe.iv) == "14.2"
    assert str(pe.delta) == "-0.58"


def test_listed_option_expiries_uses_provider_calendar_not_weekday_assumption():
    scrip_master = [
        {
            "symbol": "NIFTY19MAY2625200CE",
            "expiry": "19MAY2026",
            "strike": "2520000",
            "exch_seg": "NFO",
            "token": "111",
        },
        {
            "symbol": "NIFTY26MAY2625200CE",
            "expiry": "26MAY2026",
            "strike": "2520000",
            "exch_seg": "NFO",
            "token": "222",
        },
        {
            "symbol": "NIFTY21MAY2625200CE",
            "expiry": "21MAY2026",
            "strike": "2520000",
            "exch_seg": "NSE",
            "token": "333",
        },
    ]

    assert listed_option_expiries(
        scrip_master,
        underlying="NIFTY",
        on_or_after=date(2026, 5, 20),
    ) == [date(2026, 5, 26)]
