from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.data.oi_snapshotter import OiSnapshotter
from app.data.option_chain_provider import OptionQuote


IST = ZoneInfo("Asia/Kolkata")


class FakeProvider:
    provider_name = "fake"

    def __init__(self):
        self.calls = []

    def fetch_chain(self, *, underlying, expiry, snapshot_ts):
        self.calls.append((underlying, expiry, snapshot_ts))
        return [
            OptionQuote(
                snapshot_ts=snapshot_ts,
                underlying=underlying,
                expiry=expiry,
                strike=25200,
                option_type="CE",
                trading_symbol="NIFTY19MAY2625200CE",
                exchange="NFO",
                symbol_token="111",
                provider="fake",
                oi=120000,
                volume=1500,
                iv="11.25",
                bid="42.5",
                ask="43.0",
                ltp="42.8",
            )
        ]


class FakeStore:
    def __init__(self):
        self.quotes = []

    def upsert_quotes(self, quotes):
        self.quotes.extend(quotes)
        return len(quotes)


def test_snapshotter_capture_once_buckets_timestamp_and_persists_quotes():
    provider = FakeProvider()
    store = FakeStore()
    snapshotter = OiSnapshotter(
        provider=provider,
        store=store,
        clock=lambda: datetime(2026, 5, 19, 10, 0, 23, tzinfo=IST),
    )

    result = snapshotter.capture_once(
        underlying="nifty",
        expiry=date(2026, 5, 19),
    )

    assert result.provider == "fake"
    assert result.underlying == "NIFTY"
    assert result.snapshot_ts == datetime(2026, 5, 19, 10, 0, tzinfo=IST)
    assert result.fetched_count == 1
    assert result.stored_count == 1
    assert result.unusable_for_live_count == 0
    assert provider.calls[0][2] == result.snapshot_ts
    assert store.quotes[0].trading_symbol == "NIFTY19MAY2625200CE"


def test_snapshotter_run_session_can_be_bounded_for_supervised_jobs():
    provider = FakeProvider()
    store = FakeStore()
    snapshotter = OiSnapshotter(
        provider=provider,
        store=store,
        clock=lambda: datetime(2026, 5, 19, 10, 0, 0, tzinfo=IST),
        sleep=lambda seconds: None,
    )

    results = snapshotter.run_session(
        underlying="NIFTY",
        expiry=date(2026, 5, 19),
        max_snapshots=2,
    )

    assert len(results) == 2
    assert len(store.quotes) == 2
