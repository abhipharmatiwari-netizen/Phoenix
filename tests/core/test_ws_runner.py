from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from app.core import ws_runner
from app.core.signal_metrics import get_labeled_metrics


class _FakeSmartWebSocketV2:
    def __init__(self, *_args, **_kwargs) -> None:
        self.subscribe_calls: list[tuple[str, int, list]] = []
        self.unsubscribe_calls: list[tuple[str, int, list]] = []
        self.state = "OPEN"
        self.sock = object()
        self._active_subscribe = 0
        self.concurrent_subscribe_detected = False
        self._lock = threading.Lock()

    def subscribe(self, name: str, mode: int, token_list: list) -> None:
        with self._lock:
            self._active_subscribe += 1
            if self._active_subscribe > 1:
                self.concurrent_subscribe_detected = True
        time.sleep(0.01)
        self.subscribe_calls.append((name, mode, list(token_list)))
        with self._lock:
            self._active_subscribe -= 1

    def unsubscribe(self, name: str, mode: int, token_list: list) -> None:
        self.unsubscribe_calls.append((name, mode, list(token_list)))

    def connect(self) -> None:
        return

    def close_connection(self) -> None:
        self.state = "CLOSED"
        self.sock = None

    def on_close(self, *_args, **_kwargs) -> None:
        return


@pytest.fixture
def ws_module(monkeypatch):
    monkeypatch.setattr(ws_runner, "SmartWebSocketV2", _FakeSmartWebSocketV2)
    return ws_runner


def _runner(module) -> ws_runner.SmartWebSocketRunner:
    return module.SmartWebSocketRunner(
        auth_header_value="Bearer test",
        api_key="api",
        client_code="client",
        feed_token="feed",
        token_list=[{"exchangeType": 1, "tokens": ["101", "102"]}],
    )


def test_resubscribe_queues_pending_when_disconnected(ws_module):
    runner = _runner(ws_module)
    runner.sws.state = "CLOSED"
    runner.sws.sock = None

    ok = runner.resubscribe([{"exchangeType": 1, "tokens": ["201"]}])
    assert ok is False
    assert runner._pending_resubscribe is not None


def test_swap_subscriptions_unsubscribes_removed_tokens(ws_module):
    runner = _runner(ws_module)
    runner.token_list = [
        {"exchangeType": 1, "tokens": ["101", "102", "103"]},
        {"exchangeType": 2, "tokens": ["201"]},
    ]
    new_tokens = [
        {"exchangeType": 1, "tokens": ["101", "103"]},
        {"exchangeType": 2, "tokens": ["201"]},
    ]

    removed_payload, unsub_ok = runner.swap_subscriptions(new_tokens)
    assert unsub_ok is True
    assert removed_payload == [{"exchangeType": 1, "tokens": ["102"]}]
    assert runner.sws.unsubscribe_calls
    assert runner.sws.subscribe_calls


def test_resubscribe_serializes_parallel_mutations(ws_module):
    runner = _runner(ws_module)
    token_list_a = [{"exchangeType": 1, "tokens": ["301"]}]
    token_list_b = [{"exchangeType": 1, "tokens": ["302"]}]

    with ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(runner.resubscribe, token_list_a)
        future_b = pool.submit(runner.resubscribe, token_list_b)
        assert future_a.result() is True
        assert future_b.result() is True

    assert runner.sws.concurrent_subscribe_detected is False
    assert runner.token_list in (token_list_a, token_list_b)


def test_set_handlers_wraps_on_data_and_emits_received_metric(ws_module):
    metrics = get_labeled_metrics()
    metrics.reset_for_tests()
    runner = _runner(ws_module)
    seen = []

    runner.set_handlers(on_data=lambda _wsapp, message: seen.append(dict(message)))
    runner.sws.on_data(None, {"token": "101"})

    assert seen == [{"token": "101"}]
    assert metrics.counter_value(
        "stream_ws_ticks_received_total",
        labels={"source": "smartapi"},
    ) == 1.0
