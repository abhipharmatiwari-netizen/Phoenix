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


# ---- bar_regime sidecar persistence (issue #215) ----


def test_persist_regime_postgres_inserts_with_idempotent_conflict_clause(monkeypatch):
    """`persist_regime` writes a row into the bar_regime sidecar with the
    composite-key UPSERT shape required by issue #215 AC: idempotent on
    (bar_id, classifier_version)."""
    dummy_conn = _DummyConn()

    monkeypatch.setattr(
        "app.core.bar_persister.connect_with_retry",
        lambda dsn, **kwargs: dummy_conn,
    )

    persister = BarPersister(
        postgres_dsn="postgresql://user:pass@localhost:5432/testdb",
        postgres_table="indicator_bars",
    )
    candle = _sample_candle()
    persister.persist_regime(
        "NIFTY_IDX",
        60,
        candle,
        regime="TRENDING_UP",
        classifier_version="dynamic_policy_v2",
        signals={"adx14": 27.5, "di_spread": 12.0},
    )
    persister.close()

    regime_inserts = [
        (query, params)
        for query, params in dummy_conn.calls
        if isinstance(query, str)
        and "INSERT INTO" in query.upper()
        and "BAR_REGIME" in query.upper()
    ]
    assert regime_inserts, "expected an INSERT into the bar_regime sidecar"
    query, params = regime_inserts[-1]
    # AC: idempotent on the composite key.
    assert "ON CONFLICT (bar_id, classifier_version)" in query
    # AC: insert references the source indicator_bars row for FK integrity.
    assert "FROM \"indicator_bars\"" in query
    assert "WHERE label = %s" in query
    assert "AND timeframe_seconds = %s" in query
    assert "AND ts_start = %s" in query
    # AC: regime + classifier_version travel as positional params.
    assert params is not None
    assert params[0] == "TRENDING_UP"
    assert params[1] == "dynamic_policy_v2"
    # signals is serialized to JSON for the JSONB column.
    assert "\"adx14\": 27.5" in params[2]
    # The bar lookup uses the candle's tz-aware ts_start.
    assert params[3] == "NIFTY_IDX"
    assert params[4] == 60
    assert params[5] == candle.start_ts


def test_persist_regime_is_fire_and_forget_on_postgres_error(monkeypatch):
    """AC: regime persistence is fire-and-forget. A Postgres failure must
    never propagate out of `persist_regime` and block the hot path."""
    dummy_conn = _DummyConn()

    class _BrokenCursor(_DummyCursor):
        def execute(self, query, params=None):
            text = str(query).upper()
            if "INSERT INTO" in text and "BAR_REGIME" in text:
                raise RuntimeError("simulated bar_regime write failure")
            return super().execute(query, params)

    monkeypatch.setattr(dummy_conn, "cursor", lambda: _BrokenCursor(dummy_conn))
    monkeypatch.setattr(
        "app.core.bar_persister.connect_with_retry",
        lambda dsn, **kwargs: dummy_conn,
    )

    persister = BarPersister(
        postgres_dsn="postgresql://user:pass@localhost:5432/testdb",
        postgres_table="indicator_bars",
    )

    # Must not raise even though the cursor.execute path raises.
    persister.persist_regime(
        "NIFTY_IDX",
        60,
        _sample_candle(),
        regime="CHOPPY",
        classifier_version="dynamic_policy_v2",
        signals=None,
    )
    persister.close()


def test_persist_regime_noop_when_postgres_disabled():
    """Without a Postgres backend the call must silently no-op (no crash,
    no attribute access). Replay / CSV-only setups rely on this."""
    persister = BarPersister(csv_path=None)
    # Should return cleanly even with no connection initialized.
    persister.persist_regime(
        "NIFTY_IDX",
        60,
        _sample_candle(),
        regime="NORMAL",
        classifier_version="dynamic_policy_v2",
    )
    persister.close()


def test_persist_regime_scopes_sidecar_for_custom_indicator_bars(monkeypatch):
    """Non-default indicator_bars table names get a derived sidecar table
    name (`<table>_bar_regime`) so multi-tenant setups do not cross-write."""
    dummy_conn = _DummyConn()
    monkeypatch.setattr(
        "app.core.bar_persister.connect_with_retry",
        lambda dsn, **kwargs: dummy_conn,
    )

    persister = BarPersister(
        postgres_dsn="postgresql://user:pass@localhost:5432/testdb",
        postgres_table="tenant_alpha_indicator_bars",
    )
    persister.persist_regime(
        "NIFTY_IDX",
        60,
        _sample_candle(),
        regime="HIGH_VOL",
        classifier_version="dynamic_policy_v2",
    )
    persister.close()

    regime_inserts = [
        (query, params)
        for query, params in dummy_conn.calls
        if isinstance(query, str)
        and "INSERT INTO" in query.upper()
        and "BAR_REGIME" in query.upper()
    ]
    assert regime_inserts, "expected a sidecar INSERT for the tenant table"
    query, _params = regime_inserts[-1]
    # Sidecar name is derived from the source table, not the global default.
    assert "tenant_alpha_indicator_bars_bar_regime" in query
    assert "FROM \"tenant_alpha_indicator_bars\"" in query
