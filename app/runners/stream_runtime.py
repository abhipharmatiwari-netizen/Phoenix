"""Runtime orchestration helpers for the legacy stream runner."""

from __future__ import annotations

import http.server
import logging
import os
import socketserver
import threading
import time
from typing import Any, Callable

from app.core.dashboard_bus import dashboard_bus

logger = logging.getLogger(__name__)


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    """Minimal HTTP handler so Cloud Run health checks succeed."""

    def do_GET(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format, *args):  # noqa: A003 - BaseHTTPRequestHandler API
        return


def start_health_server(port: int):
    server = socketserver.ThreadingTCPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Health server listening on 0.0.0.0:%s", port)
    return server


def run_stream_lifecycle(
    *,
    stop_event: threading.Event,
    refresh_stop: threading.Event,
    start_health_server_enabled: bool,
    refresh_atm_loop: Callable[[], None],
    position_sync_loop: Callable[[], None],
    position_sync_interval: int,
    run_socket: Callable[[], None],
    runner: Any,
    persister: Any,
    loop_body: Callable[[], None] | None = None,
    health_server_factory: Callable[[int], Any] = start_health_server,
    shutdown_callback: Callable[[], None] | None = None,
) -> None:
    health_server = None
    refresh_thread = None
    runner_thread = None
    position_sync_thread = None
    dashboard_bus.set_live(True)
    try:
        if start_health_server_enabled and os.getenv("SKIP_HEALTH_SERVER", "0").lower() not in {
            "1",
            "true",
        }:
            try:
                health_port = int(os.getenv("PORT", "8080"))
                health_server = health_server_factory(health_port)
            except Exception as exc:
                logger.warning("Health server failed to start: %s", exc)

        refresh_thread = threading.Thread(
            target=refresh_atm_loop,
            name="atm-refresh",
            daemon=True,
        )
        refresh_thread.start()

        if position_sync_interval > 0:
            position_sync_thread = threading.Thread(
                target=position_sync_loop,
                name="position-sync",
                daemon=True,
            )
            position_sync_thread.start()

        runner_thread = threading.Thread(target=run_socket, name="ws-runner", daemon=True)
        runner_thread.start()

        while not stop_event.is_set():
            if loop_body is not None:
                loop_body()
            time.sleep(0.5)
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt (Ctrl+C). Shutting down gracefully...")
    finally:
        refresh_stop.set()
        stop_event.set()
        runner.close()
        if runner_thread:
            runner_thread.join(timeout=10)
        if refresh_thread:
            refresh_thread.join(timeout=10)
        if position_sync_thread:
            position_sync_thread.join(timeout=10)
        persister.close()
        if shutdown_callback is not None:
            try:
                shutdown_callback()
            except Exception as exc:
                logger.warning("Shutdown callback failed: %s", exc)
        if health_server:
            health_server.shutdown()
            health_server.server_close()
        dashboard_bus.set_live(False)
        logger.info("Multi-instrument stream terminated.")


__all__ = [
    "run_stream_lifecycle",
    "start_health_server",
]
