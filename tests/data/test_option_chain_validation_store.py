from __future__ import annotations

from datetime import date, datetime, timezone
import json

from app.data.option_chain_validation_store import (
    INSERT_VALIDATION_REPORT_SQL,
    OptionChainValidationReportStore,
)


class FakeCursor:
    def __init__(self):
        self.sql = None
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, row):
        self.sql = sql
        self.row = row

    def fetchone(self):
        return (42,)


class FakeConn:
    def __init__(self):
        self.cursor_obj = FakeCursor()
        self.commits = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1


def test_validation_report_store_persists_summary_and_full_payload():
    conn = FakeConn()
    payload = {
        "underlying": "NIFTY",
        "expiry": "2026-05-19",
        "ok": False,
        "compared_contracts": 1,
        "angel_only_contracts": [{"strike": 23700, "option_type": "CE"}],
        "nse_only_contracts": [],
        "mismatches": [{"strike": 23600, "option_type": "CE", "field_diffs": []}],
        "missing_angel_iv": 1,
        "missing_nse_iv": 0,
        "metadata": {"auto_realtime_validation": True},
    }

    stored = OptionChainValidationReportStore(conn, commit=True).insert_report(
        payload=payload,
        validation_ts=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
        snapshot_ts=datetime(2026, 5, 18, 9, 59, tzinfo=timezone.utc),
        underlying="nifty",
        expiry=date(2026, 5, 19),
        primary_provider="angel",
        reference_provider="nse_web",
        status="MISMATCH",
        severity="WARN",
        primary_quote_count=10,
        reference_quote_count=11,
    )

    row = conn.cursor_obj.row
    assert stored.report_id == 42
    assert stored.mismatch_count == 1
    assert conn.commits == 1
    assert conn.cursor_obj.sql == INSERT_VALIDATION_REPORT_SQL
    assert row["underlying"] == "NIFTY"
    assert row["primary_quote_count"] == 10
    assert row["reference_quote_count"] == 11
    assert row["primary_only_count"] == 1
    assert row["missing_primary_iv"] == 1
    assert json.loads(row["report_payload"])["metadata"]["auto_realtime_validation"] is True
