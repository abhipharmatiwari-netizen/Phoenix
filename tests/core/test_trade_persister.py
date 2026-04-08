from __future__ import annotations

import csv
import importlib


def test_record_trade_is_idempotent_and_uses_insert_id(tmp_path, monkeypatch):
    mod = importlib.import_module("app.core.trade_persister")

    bq_calls: list[tuple[dict, str | None]] = []
    monkeypatch.setattr(
        mod, "insert_trade_record", lambda row, insert_id=None: bq_calls.append((dict(row), insert_id))
    )
    monkeypatch.setattr(
        mod,
        "build_trade_record_from_legacy_trade",
        lambda trade, trade_id=None: {"trade_id": str(trade_id or trade.get("trade_id"))},
    )
    monkeypatch.setattr(mod, "trade_record_to_bq_row", lambda rec: {"trade_id": rec["trade_id"]})

    csv_path = tmp_path / "trades.csv"
    decision_path = tmp_path / "trade_decisions.csv"

    p1 = mod.TradePersister(csv_path=str(csv_path), decision_csv_path=str(decision_path))
    trade = {
        "trade_id": "T-100",
        "entry_ts": "2026-02-26T09:15:00+00:00",
        "exit_ts": "2026-02-26T09:30:00+00:00",
        "label": "NIFTY_IDX",
        "side": "BUY",
        "qty": 50,
    }
    p1.record_trade(trade)
    p1.record_trade(trade)
    p1.close()

    p2 = mod.TradePersister(csv_path=str(csv_path), decision_csv_path=str(decision_path))
    p2.record_trade(trade)
    p2.close()

    assert len(bq_calls) == 1
    assert bq_calls[0][1] == "T-100"

    with csv_path.open("r", newline="") as f:
        rows = list(csv.reader(f))
    assert len(rows) == 2  # header + one trade


def test_record_execution_and_decision_are_idempotent(tmp_path, monkeypatch):
    mod = importlib.import_module("app.core.trade_persister")

    bq_calls: list[tuple[dict, str | None]] = []
    monkeypatch.setattr(
        mod, "insert_trade_record", lambda row, insert_id=None: bq_calls.append((dict(row), insert_id))
    )
    monkeypatch.setattr(
        mod,
        "build_trade_record_from_exec",
        lambda payload: {"trade_id": str(payload.get("trade_id"))},
    )
    monkeypatch.setattr(mod, "trade_record_to_bq_row", lambda rec: {"trade_id": rec["trade_id"]})

    decision_path = tmp_path / "trade_decisions.csv"
    persister = mod.TradePersister(decision_csv_path=str(decision_path))
    exec_row = {
        "trade_id": "EXEC-1",
        "timestamp": "2026-02-26T10:00:00+00:00",
        "side": "SELL",
        "qty": 10,
    }
    persister.record_execution(exec_row)
    persister.record_execution(exec_row)

    decision_row = {
        "timestamp": "2026-02-26T10:01:00+00:00",
        "decision": "NO_TRADE",
        "no_trade_reason": "ml_gate",
        "strategy_name": "ema20_strategy",
        "underlying": "NIFTY_IDX",
        "label": "NIFTY_IDX",
        "template_name": "NA",
        "side": "BUY",
        "qty": 1,
        "ml_p_up": 0.1,
        "ml_p_down": 0.9,
        "ml_gate_reason": "below_threshold",
        "ml_gate_meta_json": {"threshold": 0.7},
    }
    persister.persist_decision(decision_row)
    persister.persist_decision(decision_row)
    persister.close()

    assert len(bq_calls) == 1
    assert bq_calls[0][1] == "EXEC-1"

    with decision_path.open("r", newline="") as f:
        rows = list(csv.reader(f))
    assert len(rows) == 2  # header + one decision
