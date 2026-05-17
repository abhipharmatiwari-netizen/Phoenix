from __future__ import annotations

from datetime import date, datetime, timezone

from app.data.option_chain_repository import OptionChainRepository


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.executed = []
        self.description = [
            ("snapshot_ts",),
            ("source_ts",),
            ("ingested_at",),
            ("underlying",),
            ("expiry",),
            ("strike",),
            ("option_type",),
            ("trading_symbol",),
            ("exchange",),
            ("symbol_token",),
            ("oi",),
            ("volume",),
            ("iv",),
            ("bid",),
            ("ask",),
            ("ltp",),
            ("underlying_ltp",),
            ("vix",),
            ("provider",),
            ("raw_hash",),
            ("quality_flags",),
        ]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params):
        self.executed.append((sql, params))

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, rows):
        self.cursor_obj = FakeCursor(rows)

    def cursor(self):
        return self.cursor_obj


def _row(snapshot_ts, strike=25200):
    return (
        snapshot_ts,
        None,
        snapshot_ts,
        "NIFTY",
        date(2026, 5, 19),
        strike,
        "CE",
        f"NIFTY19MAY26{strike}CE",
        "NFO",
        str(strike),
        120000,
        1500,
        "11.25",
        "42.5",
        "43.0",
        "42.8",
        "25140.5",
        None,
        "angel",
        "hash",
        {},
    )


def test_fetch_latest_snapshot_returns_only_latest_rows_at_or_before_decision_time():
    older = datetime(2026, 5, 19, 4, 29, tzinfo=timezone.utc)
    latest = datetime(2026, 5, 19, 4, 30, tzinfo=timezone.utc)
    conn = FakeConnection([_row(latest, 25200), _row(latest, 25300), _row(older, 25200)])

    quotes = OptionChainRepository(conn).fetch_latest_snapshot(
        underlying="nifty",
        expiry=date(2026, 5, 19),
        decision_ts=latest,
        min_snapshot_ts=older,
        provider="Angel",
    )

    assert [quote.strike for quote in quotes] == [25200, 25300]
    sql, params = conn.cursor_obj.executed[0]
    assert "snapshot_ts <= %(decision_ts)s" in sql
    assert params["underlying"] == "NIFTY"
    assert params["provider"] == "angel"


def test_fetch_candidate_window_filters_exact_contract_and_time_window():
    start = datetime(2026, 5, 19, 4, 30, tzinfo=timezone.utc)
    end = datetime(2026, 5, 19, 5, 0, tzinfo=timezone.utc)
    conn = FakeConnection([_row(start, 25200), _row(end, 25200)])

    quotes = OptionChainRepository(conn).fetch_candidate_window(
        underlying="NIFTY",
        expiry=date(2026, 5, 19),
        strike=25200,
        option_type="ce",
        start_ts=start,
        end_ts=end,
    )

    assert len(quotes) == 2
    sql, params = conn.cursor_obj.executed[0]
    assert "strike = %(strike)s" in sql
    assert params["option_type"] == "CE"
    assert params["provider"] is None
