from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.data.option_chain_repository import OptionChainRepository


class FakeCursor:
    def __init__(self, row_sets):
        self.row_sets = [list(row_set) for row_set in row_sets]
        self.rows = []
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
            ("delta",),
            ("gamma",),
            ("theta",),
            ("vega",),
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
        self.rows = self.row_sets.pop(0) if self.row_sets else []

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, rows):
        if rows and isinstance(rows[0], list):
            row_sets = rows
        else:
            row_sets = [rows]
        self.cursor_obj = FakeCursor(row_sets)

    def cursor(self):
        return self.cursor_obj


def _row(
    snapshot_ts,
    strike=25200,
    *,
    option_type="CE",
    iv="11.25",
    provider="angel",
    quality_flags=None,
):
    return (
        snapshot_ts,
        None,
        snapshot_ts,
        "NIFTY",
        date(2026, 5, 19),
        strike,
        option_type,
        f"NIFTY19MAY26{strike}{option_type}",
        "NFO",
        str(strike),
        120000,
        1500,
        iv,
        "0.42",
        "0.0012",
        "-3.4",
        "9.8",
        "42.5",
        "43.0",
        "42.8",
        "25140.5",
        None,
        provider,
        "hash",
        quality_flags or {},
    )


def _nse_iv_row(snapshot_ts, strike=25200, *, option_type="CE", iv="12.34"):
    return {
        "snapshot_ts": snapshot_ts,
        "strike": strike,
        "option_type": option_type,
        "iv": iv,
        "provider": "nse_web",
    }


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
    assert "%(provider)s::text IS NULL" in sql
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
    assert "%(provider)s::text IS NULL" in sql
    assert params["option_type"] == "CE"
    assert params["provider"] is None


def test_fetch_latest_snapshot_enriches_missing_angel_iv_from_nse_reference():
    older = datetime(2026, 5, 19, 4, 29, tzinfo=timezone.utc)
    latest = datetime(2026, 5, 19, 4, 30, tzinfo=timezone.utc)
    conn = FakeConnection(
        [
            [
                _row(
                    latest,
                    25200,
                    iv=None,
                    quality_flags={
                        "missing_required_fields": ["iv"],
                        "missing_optional_fields": ["iv"],
                    },
                ),
                _row(latest, 25300, iv="10.50"),
            ],
            [_nse_iv_row(latest, 25200, iv="12.34")],
        ]
    )

    quotes = OptionChainRepository(conn).fetch_latest_snapshot(
        underlying="nifty",
        expiry=date(2026, 5, 19),
        decision_ts=latest,
        min_snapshot_ts=older,
        provider="Angel",
    )

    enriched = next(quote for quote in quotes if quote.strike == 25200)
    untouched = next(quote for quote in quotes if quote.strike == 25300)
    assert str(enriched.iv) == "12.34"
    assert enriched.provider == "angel"
    assert enriched.quality_flags["iv_enrichment_mode"] == "read_time"
    assert enriched.quality_flags["iv_enriched_from_provider"] == "nse_web"
    assert "missing_required_fields" not in enriched.quality_flags
    assert "missing_optional_fields" not in enriched.quality_flags
    assert str(untouched.iv) == "10.50"

    assert len(conn.cursor_obj.executed) == 2
    sql, params = conn.cursor_obj.executed[1]
    assert "provider = %(reference_provider)s" in sql
    assert params["reference_provider"] == "nse_web"
    assert params["min_reference_ts"] == latest - timedelta(seconds=120)
    assert params["max_snapshot_ts"] == latest


def test_fetch_latest_snapshot_does_not_use_stale_nse_iv_reference():
    older = datetime(2026, 5, 19, 4, 29, tzinfo=timezone.utc)
    latest = datetime(2026, 5, 19, 4, 30, tzinfo=timezone.utc)
    stale_reference = latest - timedelta(seconds=121)
    conn = FakeConnection(
        [
            [_row(latest, 25200, iv=None)],
            [_nse_iv_row(stale_reference, 25200, iv="12.34")],
        ]
    )

    quotes = OptionChainRepository(conn).fetch_latest_snapshot(
        underlying="NIFTY",
        expiry=date(2026, 5, 19),
        decision_ts=latest,
        min_snapshot_ts=older,
        provider="angel",
    )

    assert quotes[0].iv is None


def test_fetch_latest_snapshot_skips_iv_enrichment_for_non_angel_provider():
    older = datetime(2026, 5, 19, 4, 29, tzinfo=timezone.utc)
    latest = datetime(2026, 5, 19, 4, 30, tzinfo=timezone.utc)
    conn = FakeConnection([_row(latest, 25200, iv=None, provider="nse_web")])

    quotes = OptionChainRepository(conn).fetch_latest_snapshot(
        underlying="NIFTY",
        expiry=date(2026, 5, 19),
        decision_ts=latest,
        min_snapshot_ts=older,
        provider="nse_web",
    )

    assert quotes[0].iv is None
    assert len(conn.cursor_obj.executed) == 1


def test_fetch_candidate_window_enriches_missing_angel_iv_by_contract():
    start = datetime(2026, 5, 19, 4, 30, tzinfo=timezone.utc)
    end = datetime(2026, 5, 19, 5, 0, tzinfo=timezone.utc)
    conn = FakeConnection(
        [
            [_row(start, 25200, iv=None), _row(end, 25200, iv=None)],
            [
                _nse_iv_row(start, 25200, iv="11.11"),
                _nse_iv_row(end - timedelta(seconds=30), 25200, iv="12.22"),
            ],
        ]
    )

    quotes = OptionChainRepository(conn).fetch_candidate_window(
        underlying="NIFTY",
        expiry=date(2026, 5, 19),
        strike=25200,
        option_type="ce",
        start_ts=start,
        end_ts=end,
        provider="angel",
    )

    assert [str(quote.iv) for quote in quotes] == ["11.11", "12.22"]
    sql, params = conn.cursor_obj.executed[1]
    assert "strike = %(strike)s" in sql
    assert params["reference_provider"] == "nse_web"
    assert params["strike"] == 25200
    assert params["option_type"] == "CE"
