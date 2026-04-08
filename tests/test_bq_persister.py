from __future__ import annotations

import csv
from datetime import datetime, timezone

from app.data import bq_persister
from app.data.bq_async_writer import stop_global_writer
from app.core.identifiers import BrokerAccountId, TenantId


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


def test_insert_trade_record_uses_explicit_insert_id(monkeypatch):
    stop_global_writer()
    monkeypatch.setenv("BQ_ASYNC_ENABLED", "false")

    client = _DummyBQClient()
    monkeypatch.setattr("app.data.bq_persister.get_bq_client", lambda: client)
    monkeypatch.setattr("app.data.bq_persister._trades_table_id", lambda: "proj.ds.trades")

    bq_persister.insert_trade_record(
        {"trade_id": "OID1", "qty": 1},
        insert_id="ba1:OID1",
    )

    assert len(client.calls) == 1
    table_id, rows, row_ids = client.calls[0]
    assert table_id == "proj.ds.trades"
    assert rows[0]["trade_id"] == "OID1"
    assert row_ids == ["ba1:OID1"]


def test_insert_trade_record_defaults_insert_id_from_trade_id(monkeypatch):
    stop_global_writer()
    monkeypatch.setenv("BQ_ASYNC_ENABLED", "false")

    client = _DummyBQClient()
    monkeypatch.setattr("app.data.bq_persister.get_bq_client", lambda: client)
    monkeypatch.setattr("app.data.bq_persister._trades_table_id", lambda: "proj.ds.trades")

    bq_persister.insert_trade_record({"trade_id": "OID2", "qty": 1})

    assert len(client.calls) == 1
    table_id, rows, row_ids = client.calls[0]
    assert table_id == "proj.ds.trades"
    assert rows[0]["trade_id"] == "OID2"
    assert row_ids == ["OID2"]


def test_insert_order_record_uses_explicit_insert_id(monkeypatch):
    stop_global_writer()
    monkeypatch.setenv("BQ_ASYNC_ENABLED", "false")

    client = _DummyBQClient()
    monkeypatch.setattr("app.data.bq_persister.get_bq_client", lambda: client)
    monkeypatch.setattr("app.data.bq_persister._orders_table_id", lambda: "proj.ds.orders")

    bq_persister.insert_order_record(
        {"hub_order_id": "H1", "status": "ACK"},
        insert_id="tenant1:order1",
    )

    assert len(client.calls) == 1
    table_id, rows, row_ids = client.calls[0]
    assert table_id == "proj.ds.orders"
    assert rows[0]["hub_order_id"] == "H1"
    assert row_ids == ["tenant1:order1"]


def test_insert_order_record_defaults_insert_id_from_hub_order_id(monkeypatch):
    stop_global_writer()
    monkeypatch.setenv("BQ_ASYNC_ENABLED", "false")

    client = _DummyBQClient()
    monkeypatch.setattr("app.data.bq_persister.get_bq_client", lambda: client)
    monkeypatch.setattr("app.data.bq_persister._orders_table_id", lambda: "proj.ds.orders")

    bq_persister.insert_order_record({"hub_order_id": "H2", "status": "ACK"})

    assert len(client.calls) == 1
    table_id, rows, row_ids = client.calls[0]
    assert table_id == "proj.ds.orders"
    assert rows[0]["hub_order_id"] == "H2"
    assert row_ids == ["H2"]


def test_fetch_trades_for_tenant_falls_back_to_csv_when_bq_unconfigured(
    monkeypatch, tmp_path
):
    base_dir = tmp_path / "logs"
    day_dir = base_dir / "2026-03-26"
    day_dir.mkdir(parents=True)
    csv_path = day_dir / "trades.csv"

    with open(csv_path, mode="w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "trade_id",
                "tenant_id",
                "broker_account_id",
                "strategy_id",
                "symbol",
                "side",
                "qty",
                "price",
                "trade_time",
                "realized_pnl",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "trade_id": "T-2",
                "tenant_id": "tenant-1",
                "broker_account_id": "A1",
                "strategy_id": "ema20-trend",
                "symbol": "BANKNIFTY30MAR2653700CE",
                "side": "BUY",
                "qty": "2",
                "price": "110.0",
                "trade_time": "2026-03-26T10:05:00+00:00",
                "realized_pnl": "25.5",
            }
        )
        writer.writerow(
            {
                "trade_id": "T-1",
                "tenant_id": "tenant-1",
                "broker_account_id": "A1",
                "strategy_id": "ema20-trend",
                "symbol": "BANKNIFTY30MAR2653700CE",
                "side": "SELL",
                "qty": "1",
                "price": "100.0",
                "trade_time": "2026-03-26T10:00:00+00:00",
                "realized_pnl": "10.0",
            }
        )

    monkeypatch.setenv("TRADE_CSV_PATH", str(base_dir / "trades.csv"))
    monkeypatch.setattr("app.data.bq_persister._trades_table_id", lambda: None)

    rows = bq_persister.fetch_trades_for_tenant(
        tenant_id=TenantId("tenant-1"),
        broker_account_id=BrokerAccountId("A1"),
        start_time=datetime(2026, 3, 26, 9, 59, tzinfo=timezone.utc),
        end_time=datetime(2026, 3, 26, 10, 6, tzinfo=timezone.utc),
        limit=10,
    )

    assert [row["trade_id"] for row in rows] == ["T-2", "T-1"]
    assert rows[0]["qty"] == 2
    assert rows[0]["price"] == 110.0
    assert rows[0]["realized_pnl"] == 25.5
