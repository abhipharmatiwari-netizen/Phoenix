"""Single-threaded execution queue for stream events."""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class _QueueTask:
    callback: Callable[[], None]
    description: str
    done_event: Optional[threading.Event] = None
    error: Optional[BaseException] = None


class StreamEventQueue:
    def __init__(self, *, name: str = "stream-event-queue", max_pending: int = 0) -> None:
        self._name = str(name)
        self._queue: queue.Queue[_QueueTask | None] = queue.Queue(maxsize=max(0, int(max_pending)))
        self._thread: threading.Thread | None = None
        self._closed = False
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._run,
                name=self._name,
                daemon=True,
            )
            self._thread.start()

    def submit(self, callback: Callable[[], None], *, description: str = "task") -> None:
        self._ensure_open()
        self._queue.put(_QueueTask(callback=callback, description=description))

    def submit_and_wait(
        self,
        callback: Callable[[], None],
        *,
        description: str = "task",
        timeout: float | None = None,
    ) -> None:
        self._ensure_open()
        done_event = threading.Event()
        task = _QueueTask(
            callback=callback,
            description=description,
            done_event=done_event,
        )
        self._queue.put(task)
        if not done_event.wait(timeout):
            raise TimeoutError(f"Timed out waiting for queued task: {description}")
        if task.error is not None:
            raise task.error

    def close(self, *, timeout: float = 10.0) -> None:
        with self._lock:
            if self._closed:
                thread = self._thread
            else:
                self._closed = True
                thread = self._thread
                self._queue.put(None)
        if thread is not None:
            thread.join(timeout=timeout)

    def pending_count(self) -> int:
        return self._queue.qsize()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError(f"{self._name} is closed")

    def _run(self) -> None:
        while True:
            task = self._queue.get()
            try:
                if task is None:
                    return
                try:
                    task.callback()
                except BaseException as exc:
                    task.error = exc
                    logger.exception(
                        "Stream event queue task failed: %s",
                        task.description,
                    )
                finally:
                    if task.done_event is not None:
                        task.done_event.set()
            finally:
                self._queue.task_done()

class LatestPerKeyDispatcher:
    def __init__(
        self,
        event_queue: StreamEventQueue,
        *,
        overload_threshold: int = 200,
        max_keys: int = 0,
    ) -> None:
        self._event_queue = event_queue
        self._overload_threshold = max(0, int(overload_threshold))
        self._max_keys = max(0, int(max_keys))
        self._lock = threading.Lock()
        self._slots: dict[str, dict[str, Any]] = {}

    def submit(
        self,
        key: str,
        callback: Callable[[], None],
        *,
        description: str = "task",
    ) -> str:
        normalized_key = str(key or "").strip() or "__default__"
        pending_count = self._event_queue.pending_count()
        with self._lock:
            slot = self._slots.get(normalized_key)
            overload_active = (
                slot is not None
                or self._overload_threshold == 0
                or pending_count >= self._overload_threshold
            )
            if not overload_active:
                self._event_queue.submit(callback, description=description)
                return "queued"
            if slot is None:
                if self._max_keys > 0 and len(self._slots) >= self._max_keys:
                    return "dropped"
                self._slots[normalized_key] = {
                    "callback": callback,
                    "version": 0,
                    "description": description,
                }
                should_schedule = True
                action = "queued"
            else:
                slot["callback"] = callback
                slot["version"] = int(slot.get("version", 0)) + 1
                slot["description"] = description
                should_schedule = False
                action = "merged"
        if should_schedule:
            self._event_queue.submit(
                lambda normalized_key=normalized_key: self._drain_key(normalized_key),
                description=f"{description}:coalesced",
            )
        return action

    def active_key_count(self) -> int:
        with self._lock:
            return len(self._slots)

    def is_throttling(self) -> bool:
        return self.active_key_count() > 0

    def _drain_key(self, key: str) -> None:
        while True:
            with self._lock:
                slot = self._slots.get(key)
                if slot is None:
                    return
                callback = slot["callback"]
                version = int(slot.get("version", 0))
            callback()
            with self._lock:
                latest = self._slots.get(key)
                if latest is None:
                    return
                if int(latest.get("version", 0)) == version:
                    self._slots.pop(key, None)
                    return


__all__ = ["LatestPerKeyDispatcher", "StreamEventQueue"]
