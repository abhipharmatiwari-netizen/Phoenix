"""Data Plane trade persistence (§6–§7 BigQuery/tenant isolation), used by hub strategies."""

# Persist trade and execution records to CSV, SQLite, and BigQuery.
import csv
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Set

import logging

from app.data.bq_persister import insert_trade_record
from app.data.trade_records import (
    build_trade_record_from_exec,
    build_trade_record_from_legacy_trade,
    trade_record_to_bq_row,
)

logger = logging.getLogger(__name__)


class TradePersister:
    """

    from __future__ import annotations

        Persists each closed trade to:
          - CSV (always enabled if csv_path given)
          - SQLite (if sqlite_path given)
          - BigQuery (if project/dataset/table given and google-cloud-bigquery installed)
    """

    # Configure persistence targets and decision CSV output.
    def __init__(
        self,
        csv_path: Optional[str] = None,
        sqlite_path: Optional[str] = None,
        bq_project: Optional[str] = None,
        bq_dataset: Optional[str] = None,
        bq_table: Optional[str] = None,
    ) -> None:
        self.csv_path = csv_path
        self.sqlite_path = sqlite_path
        self.bq_project = bq_project
        self.bq_dataset = bq_dataset
        self.bq_table = bq_table

        self._csv_header_written = False
        self._sqlite_conn: Optional[sqlite3.Connection] = None
        self._seen_trade_ids: Set[str] = set()
        self._seen_execution_ids: Set[str] = set()
        self._seen_lock = threading.Lock()
        self._trade_seen_path = self._sidecar_path(self.csv_path, ".seen_trade_ids")
        self._execution_seen_path = self._sidecar_path(self.csv_path, ".seen_execution_ids")
        self._load_seen_ids(self._trade_seen_path, self._seen_trade_ids)
        self._load_seen_ids(self._execution_seen_path, self._seen_execution_ids)

        if self.sqlite_path:
            self._init_sqlite()

    @staticmethod
    def _sidecar_path(base_path: Optional[str], suffix: str) -> Optional[str]:
        if not base_path:
            return None
        return f"{base_path}{suffix}"

    def _load_seen_ids(self, path: Optional[str], target: Set[str]) -> None:
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, mode="r", encoding="utf-8") as f:
                for line in f:
                    key = str(line or "").strip()
                    if key:
                        target.add(key)
        except Exception as exc:
            logger.warning("Failed to load dedupe sidecar %s: %s", path, exc)

    def _claim_seen_id(self, key: Optional[str], target: Set[str], path: Optional[str]) -> bool:
        resolved = str(key or "").strip()
        if not resolved:
            return True
        with self._seen_lock:
            if resolved in target:
                return False
            target.add(resolved)
            if not path:
                return True
            try:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with open(path, mode="a", encoding="utf-8") as f:
                    f.write(resolved)
                    f.write("\n")
            except Exception as exc:
                logger.warning("Failed writing dedupe sidecar %s: %s", path, exc)
            return True

    # ---------- SQLite ----------

    # Create the SQLite trades table if it does not exist.
    def _init_sqlite(self) -> None:
        self._sqlite_conn = sqlite3.connect(self.sqlite_path, check_same_thread=False)
        cur = self._sqlite_conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id TEXT,
                entry_ts TEXT,
                exit_ts TEXT,
                label TEXT,
                underlying TEXT,
                strategy_name TEXT,
                template_name TEXT,
                reason TEXT,
                side TEXT,
                qty INTEGER,
                entry_price REAL,
                exit_price REAL,
                pnl REAL,
                realized_pnl REAL,
                drawdown REAL,
                fees REAL
            )
            """
        )
        try:
            cols = {row[1] for row in cur.execute("PRAGMA table_info(trades)").fetchall()}
            if "trade_id" not in cols:
                cur.execute("ALTER TABLE trades ADD COLUMN trade_id TEXT")
        except Exception as exc:
            logger.warning("SQLite trades migration failed: %s", exc)
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_trade_id_unique ON trades(trade_id)"
        )
        self._sqlite_conn.commit()

    # ---------- CSV ----------

    # Write the trades CSV header once.
    def _ensure_csv_header(self) -> None:
        if not self.csv_path or self._csv_header_written:
            return
        need_header = (
            not os.path.exists(self.csv_path) or os.path.getsize(self.csv_path) == 0
        )
        if need_header:
            with open(self.csv_path, mode="a", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(
                    [
                        "entry_ts",
                        "exit_ts",
                        "label",
                        "underlying",
                        "strategy_name",
                        "template_name",
                        "reason",
                        "side",
                        "qty",
                        "entry_price",
                        "exit_price",
                        "pnl",
                        "realized_pnl",
                        "drawdown",
                        "fees",
                    ]
                )
        self._csv_header_written = True

    # ---------- Public API ----------

    # Persist a completed trade record.
    def record_trade(self, trade: Dict[str, Any]) -> None:
        insert_id = (
            trade.get("trade_id")
            or f"{trade.get('label')}|{trade.get('exit_ts')}|close"
        )
        if not self._claim_seen_id(insert_id, self._seen_trade_ids, self._trade_seen_path):
            logger.debug("Skipping duplicate trade close %s", insert_id)
            return

        trade_row = dict(trade)
        trade_row.setdefault("trade_id", insert_id)
        self._to_csv(trade_row)
        self._to_sqlite(trade_row)

        bq_row = None
        if build_trade_record_from_legacy_trade and trade_record_to_bq_row:
            try:
                trade_record = build_trade_record_from_legacy_trade(
                    trade_row, trade_id=insert_id
                )
                bq_row = trade_record_to_bq_row(trade_record)
            except Exception as exc:
                logger.error(
                    "Failed to build canonical trade record for trade close: %s", exc
                )
        else:
            logger.debug(
                "Skipping BigQuery trade insert (helpers unavailable) for trade_id=%s",
                insert_id,
            )

        if bq_row and insert_trade_record:
            try:
                insert_trade_record(bq_row, insert_id=str(insert_id))
            except Exception as exc:
                logger.error("Failed to persist trade close to BigQuery: %s", exc)

    # Persist a single execution/fill to BigQuery with de-duplication.
    def record_execution(self, execution: Dict[str, Any]) -> None:
        """
        Persist a single execution/fill (entry or exit) to BigQuery.
        Uses an in-memory set to avoid duplicate inserts per session.
        """
        ts = execution.get("timestamp") or datetime.now(timezone.utc).isoformat()
        execution = dict(execution)
        execution.setdefault("timestamp", ts)
        trade_id = execution.get("trade_id") or "|".join(
            [
                str(execution.get("order_id") or execution.get("label") or "unknown"),
                str(execution.get("side") or "NA"),
                ts,
            ]
        )
        if not self._claim_seen_id(
            str(trade_id), self._seen_execution_ids, self._execution_seen_path
        ):
            logger.debug("Skipping duplicate execution %s", trade_id)
            return

        trade_record = None
        if build_trade_record_from_exec:
            try:
                execution.setdefault("trade_id", trade_id)
                trade_record = build_trade_record_from_exec(execution)
            except Exception as exc:
                logger.error(
                    "Failed to build canonical trade record for execution %s: %s",
                    trade_id,
                    exc,
                )
        else:
            logger.debug(
                "Skipping BigQuery execution insert (helpers unavailable) for trade_id=%s",
                trade_id,
            )

        if trade_record and trade_record_to_bq_row and insert_trade_record:
            try:
                insert_trade_record(
                    trade_record_to_bq_row(trade_record),
                    insert_id=str(trade_id),
                )
            except Exception as exc:
                logger.error(
                    "Failed to persist execution %s to BigQuery: %s", trade_id, exc
                )

    # Append a trade record to CSV storage.
    def _to_csv(self, row: Dict[str, Any]) -> None:
        if not self.csv_path:
            return
        try:
            self._ensure_csv_header()
            with open(self.csv_path, mode="a", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(
                    [
                        row.get("entry_ts"),
                        row.get("exit_ts"),
                        row.get("label"),
                        row.get("underlying"),
                        row.get("strategy_name"),
                        row.get("template_name"),
                        row.get("reason"),
                        row.get("side"),
                        row.get("qty"),
                        row.get("entry_price"),
                        row.get("exit_price"),
                        row.get("pnl"),
                        row.get("realized_pnl"),
                        row.get("drawdown"),
                        row.get("fees"),
                    ]
                )
        except Exception as e:
            logger.error("Failed to write trades CSV: %s", e)

    # Insert a trade record into SQLite storage.
    def _to_sqlite(self, row: Dict[str, Any]) -> None:
        if not self._sqlite_conn:
            return
        try:
            cur = self._sqlite_conn.cursor()
            cur.execute(
                """
                INSERT OR IGNORE INTO trades (
                    trade_id, entry_ts, exit_ts, label, underlying, strategy_name, template_name, reason,
                    side, qty, entry_price, exit_price, pnl, realized_pnl, drawdown, fees
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.get("trade_id"),
                    row.get("entry_ts"),
                    row.get("exit_ts"),
                    row.get("label"),
                    row.get("underlying"),
                    row.get("strategy_name"),
                    row.get("template_name"),
                    row.get("reason"),
                    row.get("side"),
                    row.get("qty"),
                    row.get("entry_price"),
                    row.get("exit_price"),
                    row.get("pnl"),
                    row.get("realized_pnl"),
                    row.get("drawdown"),
                    row.get("fees"),
                ),
            )
            self._sqlite_conn.commit()
        except Exception as e:
            logger.error("Failed to write SQLite trades: %s", e)

    # Close any open SQLite connection.
    def close(self) -> None:
        if self._sqlite_conn:
            self._sqlite_conn.close()
            self._sqlite_conn = None
