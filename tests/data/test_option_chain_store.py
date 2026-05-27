from __future__ import annotations

from datetime import date, datetime, timezone
import json

import pytest

from app.data.option_chain_provider import OptionQuote
from app.data.option_chain_store import (
    UPSERT_OPTION_CHAIN_SQL,
    OptionChainStore,
    option_quote_to_row,
)


class FakeCursor:
    def __init__(self):
        self.executed_sql = None
        self.executed_rows = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def executemany(self, sql, rows):
        self.executed_sql = sql
        self.executed_rows = list(rows)


class FakeConnection:
    def __init__(self):
        self.cursor_obj = FakeCursor()
        self.commits = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1


def _quote(**overrides):
    base = {
        "snapshot_ts": datetime(2026, 5, 19, 10, 0, tzinfo=timezone.utc),
        "source_ts": datetime(2026, 5, 19, 9, 59, 30, tzinfo=timezone.utc),
        "underlying": "NIFTY",
        "expiry": date(2026, 5, 19),
        "strike": 25200,
        "option_type": "CE",
        "trading_symbol": "NIFTY19MAY2625200CE",
        "exchange": "NFO",
        "symbol_token": "12345",
        "provider": "angel",
        "oi": 120000,
        "volume": 1500,
        "iv": "11.25",
        "delta": "0.42",
        "gamma": "0.0012",
        "theta": "-3.4",
        "vega": "9.8",
        "bid": "42.5",
        "ask": "43.0",
        "ltp": "42.8",
        "underlying_ltp": "25140.5",
        "vix": "12.4",
        "raw_hash": "abc123",
    }
    base.update(overrides)
    return OptionQuote(**base)


def test_option_quote_to_row_serializes_quality_flags_for_jsonb():
    row = option_quote_to_row(_quote(symbol_token=None, quality_flags={"source": "test"}))
    flags = json.loads(row["quality_flags"])

    assert row["underlying"] == "NIFTY"
    assert row["provider"] == "angel"
    assert str(row["delta"]) == "0.42"
    assert flags == {"missing_symbol_token": True, "source": "test"}


def test_option_quote_to_row_recomputes_iv_only_required_flag_as_optional():
    row = option_quote_to_row(
        _quote(
            iv=None,
            quality_flags={"missing_required_fields": ["iv"]},
        )
    )
    flags = json.loads(row["quality_flags"])

    assert flags == {"missing_optional_fields": ["iv"]}


def test_option_quote_to_row_rejects_invalid_option_type():
    with pytest.raises(ValueError, match="invalid option_type"):
        option_quote_to_row(_quote(option_type="XX"))


def test_store_upserts_rows_with_parameterized_sql_and_optional_commit():
    conn = FakeConnection()
    store = OptionChainStore(conn, commit=True)

    count = store.upsert_quotes([_quote(), _quote(strike=25300)])

    assert count == 2
    assert conn.commits == 1
    assert "ON CONFLICT" in conn.cursor_obj.executed_sql
    assert "%(snapshot_ts)s" in conn.cursor_obj.executed_sql
    assert conn.cursor_obj.executed_sql == UPSERT_OPTION_CHAIN_SQL
    assert len(conn.cursor_obj.executed_rows) == 2
    assert conn.cursor_obj.executed_rows[1]["strike"] == 25300


def test_store_noops_empty_quote_batch():
    conn = FakeConnection()

    assert OptionChainStore(conn, commit=True).upsert_quotes([]) == 0
    assert conn.commits == 0
    assert conn.cursor_obj.executed_sql is None
