from __future__ import annotations

from datetime import date, datetime, timezone

from app.data.option_chain_backfill import (
    iter_option_quotes_from_file,
    option_quote_from_mapping,
)


def test_option_quote_from_mapping_supports_common_vendor_column_aliases():
    quote = option_quote_from_mapping(
        {
            "timestamp": "2026-05-19 10:00:00",
            "underlying": "NIFTY",
            "expiry_date": "2026-05-19",
            "strike_price": "25200",
            "right": "CE",
            "tradingsymbol": "NIFTY19MAY2625200CE",
            "exch_seg": "NFO",
            "token": "111",
            "open_interest": "120000",
            "tradeVolume": "1500",
            "implied_volatility": "11.25",
            "best_bid": "42.5",
            "best_ask": "43.0",
            "last_price": "42.8",
        },
        default_provider="truedata",
    ).normalized()

    assert quote.snapshot_ts == datetime(2026, 5, 19, 4, 30, tzinfo=timezone.utc)
    assert quote.expiry == date(2026, 5, 19)
    assert quote.provider == "truedata"
    assert quote.symbol_token == "111"
    assert quote.oi == 120000


def test_iter_option_quotes_from_csv_file(tmp_path):
    path = tmp_path / "chain.csv"
    path.write_text(
        "\n".join(
            [
                "snapshot_ts,underlying,expiry,strike,option_type,trading_symbol,exchange,symbol_token,oi,volume,iv,bid,ask,ltp",
                "2026-05-19T10:00:00+05:30,NIFTY,2026-05-19,25200,CE,NIFTY19MAY2625200CE,NFO,111,120000,1500,11.25,42.5,43.0,42.8",
            ]
        ),
        encoding="utf-8",
    )

    quotes = list(iter_option_quotes_from_file(path, default_provider="gdfl"))

    assert len(quotes) == 1
    quote = quotes[0].normalized()
    assert quote.provider == "gdfl"
    assert quote.snapshot_ts == datetime(2026, 5, 19, 4, 30, tzinfo=timezone.utc)
    assert quote.trading_symbol == "NIFTY19MAY2625200CE"
