"""
Optional async BigQuery writer using a background flush loop.

When enabled, records are enqueued and flushed in batches so callers avoid
network-bound writes in the hot path.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


def _truthy_env(name: str, default: str = "false") -> bool:
    raw = str(os.getenv(name, default)).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        return max(0.1, float(raw))
    except (TypeError, ValueError):
        return float(default)


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return int(default)


@dataclass(frozen=True)
class _QueuedRecord:
    table_id: str
    row: Dict[str, Any]
    insert_id: Optional[str]
    enqueued_at: str


class BQAsyncWriter:
    def __init__(
        self,
        *,
        client_getter: Callable[[], Any],
        flush_seconds: float,
        batch_size: int,
    ) -> None:
        self._client_getter = client_getter
        self._flush_seconds = float(flush_seconds)
        self._batch_size = int(batch_size)
        self._queue: Deque[_QueuedRecord] = deque()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="bq-async-writer",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=5.0)
        # Best-effort final flush on shutdown.
        self.flush_once()

    def enqueue(
        self,
        table_id: str,
        row: Dict[str, Any],
        *,
        insert_id: Optional[str] = None,
    ) -> None:
        item = _QueuedRecord(
            table_id=str(table_id),
            row=dict(row),
            insert_id=(str(insert_id).strip() if insert_id else None),
            enqueued_at=datetime.now(timezone.utc).isoformat(),
        )
        with self._lock:
            self._queue.append(item)

    def queue_size(self) -> int:
        with self._lock:
            return len(self._queue)

    def flush_once(self) -> None:
        items = self._drain_batch(self._batch_size)
        while items:
            should_continue = self._flush_items(items)
            if not should_continue:
                break
            items = self._drain_batch(self._batch_size)

    def _drain_batch(self, max_items: int) -> list[_QueuedRecord]:
        with self._lock:
            size = min(len(self._queue), max_items)
            return [self._queue.popleft() for _ in range(size)]

    def _run_loop(self) -> None:
        while not self._stop_event.wait(self._flush_seconds):
            try:
                self.flush_once()
            except Exception:
                logger.exception("BQAsyncWriter flush loop failed")

    def _flush_items(self, items: list[_QueuedRecord]) -> bool:
        if not items:
            return True
        client = self._client_getter()
        if client is None:
            # No client currently available; re-queue for retry.
            with self._lock:
                for item in reversed(items):
                    self._queue.appendleft(item)
            return False

        grouped_rows: Dict[str, list[Dict[str, Any]]] = defaultdict(list)
        grouped_row_ids: Dict[str, list[Optional[str]]] = defaultdict(list)
        for item in items:
            grouped_rows[item.table_id].append(item.row)
            grouped_row_ids[item.table_id].append(item.insert_id)

        for table_id, rows in grouped_rows.items():
            row_ids = grouped_row_ids.get(table_id, [])
            try:
                if any(rid is not None for rid in row_ids):
                    errors = client.insert_rows_json(table_id, rows, row_ids=row_ids)
                else:
                    errors = client.insert_rows_json(table_id, rows)
            except TypeError:
                # Compatibility for client stubs/mocks that don't accept row_ids.
                errors = client.insert_rows_json(table_id, rows)
            except Exception as exc:
                self._write_deadletter(
                    table_id=table_id,
                    rows=rows,
                    error=f"exception:{exc}",
                )
                logger.error(
                    "BQAsyncWriter insert exception for %s rows=%d err=%s",
                    table_id,
                    len(rows),
                    exc,
                )
                continue
            if errors:
                self._write_deadletter(
                    table_id=table_id,
                    rows=rows,
                    error=f"errors:{errors}",
                )
                logger.error(
                    "BQAsyncWriter insert errors for %s rows=%d errors=%s",
                    table_id,
                    len(rows),
                    errors,
                )
        return True

    @staticmethod
    def _write_deadletter(*, table_id: str, rows: list[Dict[str, Any]], error: str) -> None:
        day = datetime.now(timezone.utc).date().isoformat()
        path = Path("logs") / day / "bq_deadletter.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for row in rows:
                payload = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "table_id": table_id,
                    "error": error,
                    "row": row,
                }
                handle.write(json.dumps(payload, ensure_ascii=True) + "\n")


_global_lock = threading.Lock()
_global_writer: Optional[BQAsyncWriter] = None


def is_bq_async_enabled() -> bool:
    return _truthy_env("BQ_ASYNC_ENABLED", "false")


def start_global_writer(*, client_getter: Callable[[], Any]) -> Optional[BQAsyncWriter]:
    if not is_bq_async_enabled():
        return None
    flush_seconds = _float_env("BQ_ASYNC_FLUSH_SECONDS", 2.0)
    batch_size = _int_env("BQ_ASYNC_BATCH_SIZE", 200)
    global _global_writer
    with _global_lock:
        if _global_writer is None:
            _global_writer = BQAsyncWriter(
                client_getter=client_getter,
                flush_seconds=flush_seconds,
                batch_size=batch_size,
            )
        _global_writer.start()
        return _global_writer


def stop_global_writer() -> None:
    global _global_writer
    with _global_lock:
        writer = _global_writer
        _global_writer = None
    if writer is not None:
        writer.stop()


def get_global_writer() -> Optional[BQAsyncWriter]:
    with _global_lock:
        return _global_writer


def enqueue_record(
    *,
    table_id: str,
    row: Dict[str, Any],
    insert_id: Optional[str] = None,
) -> bool:
    writer = get_global_writer()
    if writer is None:
        return False
    writer.enqueue(table_id, row, insert_id=insert_id)
    return True


__all__ = [
    "BQAsyncWriter",
    "is_bq_async_enabled",
    "start_global_writer",
    "stop_global_writer",
    "get_global_writer",
    "enqueue_record",
]
