import csv
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.core.bar_persister import BarPersister


def _sample_candle():
    start = datetime(2026, 2, 1, 9, 15, tzinfo=timezone.utc)
    return SimpleNamespace(
        start_ts=start,
        end_ts=start + timedelta(minutes=1),
        o=100.0,
        h=101.0,
        low=99.5,
        c=100.8,
    )


def _sample_indicators(
    adx: float = 23.5,
    plus_di: float = 18.5,
    minus_di: float = 26.5,
):
    return {
        "atr": 1.2,
        "rsi": 55.0,
        "macd": 0.4,
        "macd_signal": 0.2,
        "macd_hist": 0.2,
        "ema_20": 100.5,
        "adx": adx,
        "plus_di": plus_di,
        "minus_di": minus_di,
        "di_spread": abs(minus_di - plus_di),
    }


def test_persist_bar_csv_includes_adx_column(tmp_path):
    csv_path = tmp_path / "indicator_bars.csv"
    persister = BarPersister(csv_path=str(csv_path))

    persister.persist_bar("NIFTY_IDX", 60, _sample_candle(), _sample_indicators())
    persister.close()

    with csv_path.open("r", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    assert reader.fieldnames is not None
    assert "adx" in reader.fieldnames
    assert "plus_di" in reader.fieldnames
    assert "minus_di" in reader.fieldnames
    assert "di_spread" in reader.fieldnames
    assert len(rows) == 1
    assert float(rows[0]["adx"]) == pytest.approx(23.5)
    assert float(rows[0]["plus_di"]) == pytest.approx(18.5)
    assert float(rows[0]["minus_di"]) == pytest.approx(26.5)
    assert float(rows[0]["di_spread"]) == pytest.approx(8.0)


def test_existing_csv_without_adx_header_is_migrated(tmp_path):
    csv_path = tmp_path / "indicator_bars.csv"
    old_header = [
        "ts_start",
        "ts_end",
        "label",
        "timeframe_seconds",
        "o",
        "h",
        "l",
        "c",
        "atr",
        "rsi",
        "macd",
        "macd_signal",
        "macd_hist",
        "ema_20",
    ]
    old_row = [
        "2026-02-01T09:15:00+00:00",
        "2026-02-01T09:16:00+00:00",
        "NIFTY_IDX",
        "60",
        "100.0",
        "101.0",
        "99.5",
        "100.8",
        "1.2",
        "55.0",
        "0.4",
        "0.2",
        "0.2",
        "100.5",
    ]
    with csv_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(old_header)
        writer.writerow(old_row)

    persister = BarPersister(csv_path=str(csv_path))
    persister.persist_bar("NIFTY_IDX", 60, _sample_candle(), _sample_indicators(adx=31.0))
    persister.close()

    with csv_path.open("r", newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        row1 = next(reader)
        remaining_rows = list(reader)

    assert "adx" in header
    assert "plus_di" in header
    assert "minus_di" in header
    assert "di_spread" in header
    assert len(row1) == len(header)
    adx_idx = header.index("adx")
    plus_idx = header.index("plus_di")
    minus_idx = header.index("minus_di")
    spread_idx = header.index("di_spread")
    assert float(row1[adx_idx]) == pytest.approx(31.0)
    assert float(row1[plus_idx]) == pytest.approx(18.5)
    assert float(row1[minus_idx]) == pytest.approx(26.5)
    assert float(row1[spread_idx]) == pytest.approx(8.0)
    assert remaining_rows == []


def test_persist_bar_sqlite_includes_di_columns(tmp_path):
    persister = BarPersister(sqlite_path=":memory:")

    persister.persist_bar(
        "NIFTY_IDX",
        60,
        _sample_candle(),
        _sample_indicators(adx=19.75, plus_di=14.0, minus_di=24.5),
    )
    conn = persister._sqlite_conn
    assert conn is not None
    try:
        cols = {
            row[1] for row in conn.execute("PRAGMA table_info(indicator_bars)").fetchall()
        }
        assert "adx" in cols
        assert "plus_di" in cols
        assert "minus_di" in cols
        assert "di_spread" in cols
        vals = conn.execute(
            "SELECT adx, plus_di, minus_di, di_spread FROM indicator_bars ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        persister.close()

    assert vals is not None
    assert float(vals[0]) == pytest.approx(19.75)
    assert float(vals[1]) == pytest.approx(14.0)
    assert float(vals[2]) == pytest.approx(24.5)
    assert float(vals[3]) == pytest.approx(10.5)


class _DummyCursor:
    def __init__(self, conn):
        self._conn = conn
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, query, params=None):
        self._conn.calls.append((query, params))
        return self


class _DummyConn:
    def __init__(self):
        self.calls = []
        self.closed = False

    def cursor(self):
        return _DummyCursor(self)

    def close(self):
        self.closed = True


def test_persist_bar_postgres_includes_di_columns(monkeypatch):
    dummy_conn = _DummyConn()

    def _fake_connect_with_retry(dsn, **kwargs):
        assert dsn == "postgresql://user:pass@localhost:5432/testdb"
        return dummy_conn

    monkeypatch.setattr(
        "app.core.bar_persister.connect_with_retry",
        _fake_connect_with_retry,
    )

    persister = BarPersister(
        postgres_dsn="postgresql://user:pass@localhost:5432/testdb",
        postgres_table="indicator_bars",
    )
    persister.persist_bar(
        "NIFTY_IDX",
        60,
        _sample_candle(),
        _sample_indicators(adx=11.25, plus_di=12.0, minus_di=22.0),
    )
    persister.close()

    insert_calls = [
        (query, params)
        for query, params in dummy_conn.calls
        if isinstance(query, str) and "INSERT INTO" in query.upper()
    ]
    assert insert_calls, "expected at least one INSERT call"
    _, insert_params = insert_calls[-1]
    assert insert_params is not None
    assert float(insert_params[14]) == pytest.approx(11.25)  # adx
    assert float(insert_params[15]) == pytest.approx(12.0)  # plus_di
    assert float(insert_params[16]) == pytest.approx(22.0)  # minus_di
    assert float(insert_params[17]) == pytest.approx(10.0)  # di_spread
    assert dummy_conn.closed is True


def test_persist_bar_postgres_prunes_old_rows_when_retention_enabled(monkeypatch):
    dummy_conn = _DummyConn()

    def _fake_connect_with_retry(dsn, **kwargs):
        assert dsn == "postgresql://user:pass@localhost:5432/testdb"
        return dummy_conn

    monkeypatch.setattr(
        "app.core.bar_persister.connect_with_retry",
        _fake_connect_with_retry,
    )

    persister = BarPersister(
        postgres_dsn="postgresql://user:pass@localhost:5432/testdb",
        postgres_table="indicator_bars",
        postgres_retention_months=3,
        postgres_retention_prune_interval_seconds=0,
    )
    persister.persist_bar("NIFTY_IDX", 60, _sample_candle(), _sample_indicators())
    persister.close()

    delete_calls = [
        (query, params)
        for query, params in dummy_conn.calls
        if isinstance(query, str) and "DELETE FROM" in query.upper()
    ]
    assert delete_calls, "expected retention pruning DELETE call"
    assert any(params == (3,) for _, params in delete_calls)

    ts_index_calls = [
        query
        for query, _params in dummy_conn.calls
        if isinstance(query, str) and "CREATE INDEX IF NOT EXISTS" in query.upper() and "TS_START" in query.upper()
    ]
    assert ts_index_calls, "expected ts_start retention index creation"


def test_persist_bar_csv_is_idempotent_for_same_bar(tmp_path):
    csv_path = tmp_path / "indicator_bars.csv"
    persister = BarPersister(csv_path=str(csv_path))

    candle = _sample_candle()
    persister.persist_bar("NIFTY_IDX", 60, candle, _sample_indicators(adx=20.0))
    persister.persist_bar("NIFTY_IDX", 60, candle, _sample_indicators(adx=20.0))
    persister.close()

    with csv_path.open("r", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1


def test_persist_bar_sqlite_upserts_same_bar_key():
    persister = BarPersister(sqlite_path=":memory:")

    candle = _sample_candle()
    persister.persist_bar("NIFTY_IDX", 60, candle, _sample_indicators(adx=10.0))
    persister.persist_bar("NIFTY_IDX", 60, candle, _sample_indicators(adx=40.0))
    conn = persister._sqlite_conn
    assert conn is not None
    try:
        count = conn.execute("SELECT COUNT(*) FROM indicator_bars").fetchone()
        latest = conn.execute(
            "SELECT adx, plus_di, minus_di, di_spread FROM indicator_bars WHERE label=? AND timeframe_seconds=?",
            ("NIFTY_IDX", 60),
        ).fetchone()
    finally:
        persister.close()

    assert count is not None
    assert int(count[0]) == 1
    assert latest is not None
    assert float(latest[0]) == pytest.approx(40.0)
    assert float(latest[1]) == pytest.approx(18.5)
    assert float(latest[2]) == pytest.approx(26.5)
    assert float(latest[3]) == pytest.approx(8.0)


def test_persist_bar_bigquery_uses_row_ids():
    class _DummyBQClient:
        def __init__(self):
            self.calls = []

        def insert_rows_json(self, table_ref, rows, row_ids=None, ignore_unknown_values=True):
            self.calls.append((table_ref, rows, row_ids, ignore_unknown_values))
            return []

    persister = BarPersister()
    persister._bq_client = _DummyBQClient()
    persister._bq_table_ref = "proj.ds.indicator_bars"
    persister.persist_bar("NIFTY_IDX", 60, _sample_candle(), _sample_indicators(adx=22.0))
    persister.close()

    assert len(persister._bq_client.calls) == 1
    _table, _rows, row_ids, _ignore = persister._bq_client.calls[0]
    assert row_ids == ["NIFTY_IDX|60|2026-02-01T09:15:00+00:00"]
