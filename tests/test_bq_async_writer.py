from __future__ import annotations

import threading

from app.data import bq_persister
from app.data.bq_async_writer import get_global_writer, start_global_writer, stop_global_writer


class _DummyBQClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict], list[str] | None]] = []

    def insert_rows_json(self, table_id, rows, row_ids=None):
        normalized_row_ids = None
        if row_ids is not None:
            normalized_row_ids = [None if rid is None else str(rid) for rid in row_ids]
        self.calls.append(
            (str(table_id), [dict(row) for row in rows], normalized_row_ids)
        )
        return []


def test_bq_async_writer_batches_trade_records(monkeypatch):
    stop_global_writer()
    monkeypatch.setenv("BQ_ASYNC_ENABLED", "true")
    monkeypatch.setenv("BQ_ASYNC_FLUSH_SECONDS", "60")
    monkeypatch.setenv("BQ_ASYNC_BATCH_SIZE", "200")

    client = _DummyBQClient()
    monkeypatch.setattr("app.data.bq_persister.get_bq_client", lambda: client)
    monkeypatch.setattr("app.data.bq_persister._trades_table_id", lambda: "proj.ds.trades")

    start_global_writer(client_getter=bq_persister.get_bq_client)
    try:
        bq_persister.insert_trade_record({"trade_id": "T1", "qty": 1})
        bq_persister.insert_trade_record({"trade_id": "T2", "qty": 2})
        bq_persister.insert_trade_record({"trade_id": "T3", "qty": 3})

        writer = get_global_writer()
        assert writer is not None
        writer.flush_once()

        assert len(client.calls) == 1
        table_id, rows, row_ids = client.calls[0]
        assert table_id == "proj.ds.trades"
        assert [row["trade_id"] for row in rows] == ["T1", "T2", "T3"]
        assert row_ids == ["T1", "T2", "T3"]
    finally:
        stop_global_writer()


def test_bq_async_writer_flush_once_returns_when_client_unavailable():
    from app.data.bq_async_writer import BQAsyncWriter

    ready = threading.Event()
    client = _DummyBQClient()

    def _client_getter():
        if ready.is_set():
            return client
        return None

    writer = BQAsyncWriter(
        client_getter=_client_getter,
        flush_seconds=60.0,
        batch_size=10,
    )
    writer.enqueue("proj.ds.trades", {"trade_id": "T-NONE"}, insert_id="T-NONE")

    thread = threading.Thread(target=writer.flush_once, daemon=True)
    thread.start()
    thread.join(timeout=0.25)
    finished_without_client = not thread.is_alive()

    # Cleanup path for old/broken implementations that block here.
    if thread.is_alive():
        ready.set()
        thread.join(timeout=1.0)

    assert finished_without_client
    assert writer.queue_size() == 1


def test_bq_async_writer_stop_returns_when_client_unavailable():
    from app.data.bq_async_writer import BQAsyncWriter

    ready = threading.Event()
    client = _DummyBQClient()

    def _client_getter():
        if ready.is_set():
            return client
        return None

    writer = BQAsyncWriter(
        client_getter=_client_getter,
        flush_seconds=60.0,
        batch_size=10,
    )
    writer.enqueue("proj.ds.trades", {"trade_id": "T-STOP-NONE"}, insert_id="T-STOP-NONE")

    thread = threading.Thread(target=writer.stop, daemon=True)
    thread.start()
    thread.join(timeout=0.25)
    finished_without_client = not thread.is_alive()

    # Cleanup path for old/broken implementations that block in stop()->flush_once().
    if thread.is_alive():
        ready.set()
        thread.join(timeout=1.0)

    assert finished_without_client
    assert writer.queue_size() == 1
