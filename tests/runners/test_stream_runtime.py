from __future__ import annotations

import threading

from app.runners import stream_runtime


def test_run_stream_lifecycle_teardown_is_safe_and_idempotent(monkeypatch):
    stop_event = threading.Event()
    refresh_stop = threading.Event()
    shutdown_calls = []

    class _Runner:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    class _Persister:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    class _HealthServer:
        def __init__(self):
            self.shutdown_calls = 0
            self.server_close_calls = 0

        def shutdown(self):
            self.shutdown_calls += 1

        def server_close(self):
            self.server_close_calls += 1

    health_server = _HealthServer()
    monkeypatch.setenv("SKIP_HEALTH_SERVER", "0")

    def _refresh_loop():
        while not refresh_stop.wait(0.01):
            pass

    def _position_loop():
        return

    def _run_socket():
        stop_event.set()

    runner = _Runner()
    persister = _Persister()

    stream_runtime.run_stream_lifecycle(
        stop_event=stop_event,
        refresh_stop=refresh_stop,
        start_health_server_enabled=True,
        refresh_atm_loop=_refresh_loop,
        position_sync_loop=_position_loop,
        position_sync_interval=0,
        run_socket=_run_socket,
        runner=runner,
        persister=persister,
        health_server_factory=lambda _port: health_server,
        shutdown_callback=lambda: shutdown_calls.append("done"),
    )

    assert runner.close_calls == 1
    assert persister.close_calls == 1
    assert health_server.shutdown_calls == 1
    assert health_server.server_close_calls == 1
    assert shutdown_calls == ["done"]
