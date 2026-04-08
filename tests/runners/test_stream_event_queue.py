from __future__ import annotations

import threading

import pytest

from app.runners.stream_event_queue import LatestPerKeyDispatcher, StreamEventQueue


def test_stream_event_queue_preserves_submission_order():
    event_queue = StreamEventQueue()
    event_queue.start()
    seen: list[int] = []
    try:
        event_queue.submit(lambda: seen.append(1), description="first")
        event_queue.submit(lambda: seen.append(2), description="second")
        event_queue.submit_and_wait(lambda: seen.append(3), description="third")
    finally:
        event_queue.close()

    assert seen == [1, 2, 3]


def test_stream_event_queue_submit_and_wait_surfaces_errors():
    event_queue = StreamEventQueue()
    event_queue.start()
    try:
        with pytest.raises(RuntimeError, match="boom"):
            event_queue.submit_and_wait(
                lambda: (_ for _ in ()).throw(RuntimeError("boom")),
                description="boom-task",
            )
    finally:
        event_queue.close()


def test_latest_per_key_dispatcher_merges_repeated_keys():
    event_queue = StreamEventQueue()
    event_queue.start()
    dispatcher = LatestPerKeyDispatcher(event_queue, overload_threshold=0)
    seen: list[int] = []
    started = threading.Event()
    release = threading.Event()
    try:
        dispatcher.submit(
            "NIFTY",
            lambda: (seen.append(1), started.set(), release.wait(1.0)),
            description="tick",
        )
        assert started.wait(1.0) is True
        assert (
            dispatcher.submit("NIFTY", lambda: seen.append(2), description="tick")
            == "merged"
        )
        assert (
            dispatcher.submit("NIFTY", lambda: seen.append(3), description="tick")
            == "merged"
        )
        release.set()
        event_queue.submit_and_wait(lambda: None, description="flush")
    finally:
        event_queue.close()

    assert seen == [1, 3]


def test_latest_per_key_dispatcher_can_drop_when_key_capacity_is_exhausted():
    event_queue = StreamEventQueue()
    event_queue.start()
    dispatcher = LatestPerKeyDispatcher(
        event_queue,
        overload_threshold=0,
        max_keys=1,
    )
    started = threading.Event()
    release = threading.Event()
    try:
        assert (
            dispatcher.submit(
                "NIFTY",
                lambda: (started.set(), release.wait(1.0)),
                description="tick",
            )
            == "queued"
        )
        assert started.wait(1.0) is True
        assert (
            dispatcher.submit("BANKNIFTY", lambda: None, description="tick")
            == "dropped"
        )
        release.set()
        event_queue.submit_and_wait(lambda: None, description="flush")
    finally:
        event_queue.close()
