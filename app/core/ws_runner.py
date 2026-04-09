"""
Run Angel SmartWebSocketV2 with batching, reconnection, and status tracking.
Wraps the broker websocket client to manage token subscriptions safely.
"""

from __future__ import annotations

import logging
import os
import time
import threading
from typing import Callable, Optional

try:  # pragma: no cover - optional in lightweight test environments
    from SmartApi.smartWebSocketV2 import SmartWebSocketV2  # type: ignore
except Exception:  # pragma: no cover - optional in lightweight test environments
    SmartWebSocketV2 = None
from app.core.signal_metrics import get_labeled_metrics

try:  # websocket-client import path
    from websocket._exceptions import WebSocketConnectionClosedException
except Exception:  # pragma: no cover - fallback if path changes
    WebSocketConnectionClosedException = Exception  # type: ignore

logger = logging.getLogger(__name__)

# Optimal batch size for Angel SmartAPI subscription (25 tokens per batch)
# Reduces subscription time from 2-3s to ~200ms by parallel API processing
SUBSCRIPTION_BATCH_SIZE = 25


def _is_network_outage_error(message: str) -> bool:
    text = str(message).lower()
    markers = (
        "getaddrinfo failed",
        "name or service not known",
        "temporary failure in name resolution",
        "connection timeout",
        "timed out",
        "connection closed",
        "forcibly closed",
        "network is unreachable",
        "connection reset",
        "connection aborted",
    )
    return any(marker in text for marker in markers)


class _RepeatThrottleFilter(logging.Filter):
    def __init__(self, throttle_seconds: float) -> None:
        super().__init__()
        self._throttle_seconds = max(0.0, float(throttle_seconds))
        self._last_seen: dict[tuple[str, str], float] = {}
        self._suppressed: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()

    def filter(self, record: logging.LogRecord) -> bool:
        if self._throttle_seconds <= 0:
            return True
        message = record.getMessage()
        key = (record.name, message)
        now = time.monotonic()
        with self._lock:
            last_ts = self._last_seen.get(key)
            if last_ts is not None and (now - last_ts) < self._throttle_seconds:
                self._suppressed[key] = self._suppressed.get(key, 0) + 1
                return False
            suppressed = self._suppressed.pop(key, 0)
            self._last_seen[key] = now
        if suppressed:
            record.msg = f"{message} (suppressed {suppressed} repeats)"
            record.args = ()
        return True


_websocket_logger = logging.getLogger("websocket")
if not any(isinstance(f, _RepeatThrottleFilter) for f in _websocket_logger.filters):
    try:
        _ws_lib_log_throttle = float(os.getenv("WS_LIBRARY_LOG_THROTTLE_SECONDS", "15"))
    except (TypeError, ValueError):
        _ws_lib_log_throttle = 15.0
    _websocket_logger.addFilter(
        _RepeatThrottleFilter(_ws_lib_log_throttle)
    )


class WebSocketState:
    """
    Lightweight in-memory websocket status tracker for health/reporting.
    """

    # Initialize connection status and last error fields.
    def __init__(self) -> None:
        self.status: str = "disconnected"
        self.last_error: Optional[str] = None

    # Update status and optionally record the last error.
    def set_status(self, status: str, error: Optional[str] = None) -> None:
        self.status = status
        if error is not None:
            self.last_error = error


class SmartWebSocketRunner:
    # Configure the SmartWebSocketV2 client and subscription options.
    def __init__(
        self,
        auth_header_value: str,
        api_key: str,
        client_code: str,
        feed_token: str,
        token_list: list,
        *,
        subscribe_name: str = "multiuniv1",
        subscribe_mode: int = 2,
        max_retry_attempt: int = 3,
        retry_strategy: int = 0,
        retry_delay: int = 10,
        retry_duration: int = 60,
    ) -> None:
        if SmartWebSocketV2 is None:
            raise RuntimeError("SmartApi websocket client is required for live websocket runner")
        self.sws = SmartWebSocketV2(
            auth_header_value,
            api_key,
            client_code,
            feed_token,
            max_retry_attempt=max_retry_attempt,
            retry_strategy=retry_strategy,
            retry_delay=retry_delay,
            retry_duration=retry_duration,
        )
        self.token_list = token_list
        self.subscribe_name = subscribe_name
        self.subscribe_mode = subscribe_mode
        self._closed = False
        self._user_on_open: Optional[Callable] = None
        self._user_on_error: Optional[Callable] = None
        self._user_on_close: Optional[Callable] = None
        self.state = WebSocketState()
        self._min_stable_seconds = 10
        self._subscription_lock = threading.RLock()
        self._pending_resubscribe: Optional[tuple] = None  # Queue for deferred resubscription
        self._last_error_message: Optional[str] = None
        self._last_error_log_ts: float = 0.0
        self._suppressed_error_logs: int = 0
        try:
            self._error_log_throttle_seconds = max(
                1.0, float(os.getenv("WS_ERROR_LOG_THROTTLE_SECONDS", "15"))
            )
        except (TypeError, ValueError):
            self._error_log_throttle_seconds = 15.0
        try:
            self._circuit_trigger_failures = max(
                1, int(os.getenv("WS_CIRCUIT_BREAKER_FAILURES", "4"))
            )
        except (TypeError, ValueError):
            self._circuit_trigger_failures = 4
        try:
            self._circuit_base_seconds = max(
                1.0, float(os.getenv("WS_CIRCUIT_BREAKER_BASE_SECONDS", "20"))
            )
        except (TypeError, ValueError):
            self._circuit_base_seconds = 20.0
        try:
            self._circuit_max_seconds = max(
                self._circuit_base_seconds,
                float(os.getenv("WS_CIRCUIT_BREAKER_MAX_SECONDS", "300")),
            )
        except (TypeError, ValueError):
            self._circuit_max_seconds = max(300.0, self._circuit_base_seconds)
        self._circuit_open_until_mono: float = 0.0
        self._consecutive_network_failures: int = 0

        self._patch_close_signature()
        self.sws.on_open = self._on_open

    def _log_error_throttled(self, message: str, *, level: int = logging.WARNING) -> None:
        now = time.monotonic()
        if (
            self._last_error_message == message
            and (now - self._last_error_log_ts) < self._error_log_throttle_seconds
        ):
            self._suppressed_error_logs += 1
            return
        suffix = (
            f" (suppressed {self._suppressed_error_logs} repeats)"
            if self._suppressed_error_logs
            else ""
        )
        logger.log(level, "%s%s", message, suffix)
        self._suppressed_error_logs = 0
        self._last_error_message = message
        self._last_error_log_ts = now

    def _register_network_failure(self, error_text: str) -> None:
        if not _is_network_outage_error(error_text):
            return
        self._consecutive_network_failures += 1
        if self._consecutive_network_failures < self._circuit_trigger_failures:
            return
        extra_failures = (
            self._consecutive_network_failures - self._circuit_trigger_failures
        )
        open_seconds = min(
            self._circuit_max_seconds,
            self._circuit_base_seconds * (2 ** extra_failures),
        )
        circuit_until = time.monotonic() + open_seconds
        if circuit_until <= self._circuit_open_until_mono:
            return
        self._circuit_open_until_mono = circuit_until
        self._log_error_throttled(
            (
                "WebSocket circuit breaker open for %.1fs after repeated network "
                "failures (count=%d)"
            )
            % (open_seconds, self._consecutive_network_failures),
            level=logging.ERROR,
        )

    # Patch on_close to tolerate different websocket-client signatures.
    def _patch_close_signature(self) -> None:
        """
        SmartAPI's bundled on_close handler only accepts ``wsapp`` whereas newer
        websocket-client versions pass ``(wsapp, status_code, msg)``. Patch the
        instance method to tolerate both so the callback does not crash.
        """
        sws = self.sws

        def _safe_close(wsapp, close_status_code=None, close_msg=None):
            logger.info(
                "WebSocket closed (code=%s, reason=%s)",
                close_status_code,
                close_msg,
            )
            try:
                sws.on_close(wsapp, close_status_code, close_msg)
            except TypeError:
                sws.on_close(wsapp)
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.error("on_close handler failed: %s", exc)

        sws._on_close = _safe_close

    # Subscribe on open, handling batching and pending resubscribe.
    def _on_open(self, wsapp) -> None:
        if self._closed:
            logger.warning("WebSocket opened after close request; skipping subscribe.")
            return
        with self._subscription_lock:
            self.state.set_status("connected")
            logger.info(
                "WebSocket opened, subscribing to %s (mode=%s) with %d tokens...",
                self.subscribe_name,
                self.subscribe_mode,
                len(self.token_list),
            )

            # Subscribe in batches to optimize API performance
            # Angel SmartAPI processes batches in parallel, avoiding timeout issues
            batch_count = (len(self.token_list) + SUBSCRIPTION_BATCH_SIZE - 1) // SUBSCRIPTION_BATCH_SIZE
            for i in range(0, len(self.token_list), SUBSCRIPTION_BATCH_SIZE):
                batch = self.token_list[i:i+SUBSCRIPTION_BATCH_SIZE]
                batch_num = (i // SUBSCRIPTION_BATCH_SIZE) + 1
                self.sws.subscribe(self.subscribe_name, self.subscribe_mode, batch)
                logger.debug(
                    "Subscribed batch %d/%d: %d tokens (total progress: %d/%d)",
                    batch_num,
                    batch_count,
                    len(batch),
                    min(i + SUBSCRIPTION_BATCH_SIZE, len(self.token_list)),
                    len(self.token_list)
                )

            logger.info(
                "WebSocket subscription complete: %d tokens across %d batches",
                len(self.token_list),
                batch_count
            )

            # Retry any pending resubscription that was deferred
            if self._pending_resubscribe is not None:
                pending_tokens, attempt, max_attempts = self._pending_resubscribe
                logger.info(
                    "Retrying pending resubscription (attempt %d/%d) after connection restored...",
                    attempt + 1,
                    max_attempts
                )
                self._pending_resubscribe = None  # Clear before retry to avoid infinite loops
                self.resubscribe(pending_tokens, attempt=attempt, max_attempts=max_attempts)
        
        if self._user_on_open:
            self._user_on_open(wsapp)

    # Replace subscriptions with retries and backoff.
    def resubscribe(self, token_list: list, *, attempt: int = 0, max_attempts: int = 3) -> bool:
        """
        Resubscribe to an updated token list with exponential backoff.
        
        Does NOT force-reconnect if WebSocket is disconnected. Instead, queues
        the resubscription to be retried when connection is restored.
        
        Args:
            token_list: List of tokens to subscribe to
            attempt: Current retry attempt (0-indexed)
            max_attempts: Maximum number of retry attempts
        
        Returns:
            True if resubscribe was successful, False otherwise
        """
        with self._subscription_lock:
            # Check connection state - queue if closed or not connected
            if self._closed or not self._is_connected():
                logger.warning(
                    "Cannot resubscribe: WebSocket not connected. "
                    "Queueing resubscription for when connection is restored..."
                )
                self._pending_resubscribe = (token_list, attempt, max_attempts)
                return False

            try:
                self.token_list = token_list

                # Subscribe in batches to optimize API performance
                batch_count = (len(token_list) + SUBSCRIPTION_BATCH_SIZE - 1) // SUBSCRIPTION_BATCH_SIZE
                logger.info(
                    "Starting resubscription attempt %d/%d with %d tokens (%d batches)...",
                    attempt + 1,
                    max_attempts,
                    len(token_list) if token_list else 0,
                    batch_count
                )

                for i in range(0, len(token_list), SUBSCRIPTION_BATCH_SIZE):
                    batch = token_list[i:i+SUBSCRIPTION_BATCH_SIZE]
                    batch_num = (i // SUBSCRIPTION_BATCH_SIZE) + 1
                    self.sws.subscribe(self.subscribe_name, self.subscribe_mode, batch)
                    logger.debug(
                        "Resubscribe batch %d/%d: %d tokens",
                        batch_num,
                        batch_count,
                        len(batch)
                    )

                logger.info(
                    "Resubscribe successful on attempt %d (now subscribed to %d tokens across %d batches)",
                    attempt + 1,
                    len(token_list) if token_list else 0,
                    batch_count
                )
                self._pending_resubscribe = None  # Clear any pending resubscribe
                return True
            except Exception as exc:
            # Check for "Connection is already closed" - this needs immediate reconnect
                exc_str = str(exc).lower()
                if "connection is already closed" in exc_str or "forcibly closed" in exc_str:
                    logger.error(
                        "Resubscribe failed: Connection closed by remote host. "
                        "Forcing full reconnect...",
                    )
                    self.request_reconnect()
                    self._pending_resubscribe = (token_list, 0, max_attempts)
                    return False

                if attempt >= max_attempts - 1:
                    logger.error(
                        "Resubscribe failed after %d attempts: %s. "
                        "Will retry on next reconnection attempt.",
                        max_attempts,
                        exc
                    )
                    # Queue for retry instead of forcing reconnect
                    self._pending_resubscribe = (token_list, 0, max_attempts)
                    return False

                # Exponential backoff: 1s, 2s, 4s
                wait_seconds = 2 ** attempt
                logger.warning(
                    "Resubscribe attempt %d/%d failed: %s. Retrying in %ds...",
                    attempt + 1,
                    max_attempts,
                    exc,
                    wait_seconds
                )

                # Schedule retry using a background timer (thread-safe)
                def _retry() -> None:
                    self.resubscribe(
                        token_list, attempt=attempt + 1, max_attempts=max_attempts
                    )

                timer = threading.Timer(wait_seconds, _retry)
                timer.daemon = True
                timer.start()
                return False

    # Subscribe to additional tokens without replacing the current list.
    def subscribe_tokens(self, token_list: list) -> bool:
        """
        Subscribe to an additional token list without replacing the current universe.
        Checks connection state before attempting subscribe.
        """
        with self._subscription_lock:
            # Check connection state first
            if not self._is_connected():
                logger.warning("Cannot subscribe: WebSocket not connected. Requesting reconnect.")
                self.request_reconnect()
                return False

            try:
                self.sws.subscribe(self.subscribe_name, self.subscribe_mode, token_list)
                logger.debug("Token subscribe successful for %d tokens", len(token_list))
                return True
            except Exception as exc:  # pragma: no cover
                logger.warning("Token subscribe failed: %s. Requesting reconnect.", exc)
                self.request_reconnect()
                return False
    
    # Check whether the websocket is connected and usable.
    def _is_connected(self) -> bool:
        """
        Check if WebSocket is currently connected.
        
        Returns:
            True if WebSocket is connected and ready, False otherwise
        """
        try:
            # Check if socket exists and is not closed
            if self._closed:
                return False
            # Check WebSocket state - it may be OPEN, OPENING, or CONNECTING
            # Don't rely solely on sock being None since it might be in transition
            if hasattr(self.sws, 'state'):
                state = getattr(self.sws, 'state', None)
                if state in ['OPEN', 'CONNECTING', 'OPENING']:
                    return True
                if state == 'CLOSED':
                    return False
            # Fallback: check if sock exists
            if hasattr(self.sws, 'sock') and self.sws.sock is not None:
                return True
            # If status indicates connected, consider it connected
            if self.state.status == 'connected':
                return True
            return False
        except Exception as e:
            logger.debug(f"Connection check failed: {e}")
            return False

    # Force a reconnect by closing the underlying socket.
    def request_reconnect(self) -> None:
        """
        Force a websocket reconnect by closing the underlying connection.
        """
        if self._closed:
            return
        try:
            logger.warning("WebSocket reconnect requested.")
            self.sws.close_connection()
        except Exception as exc:  # pragma: no cover
            logger.error("WebSocket reconnect request failed: %s", exc)

    # Flatten a token list payload into a unique string set.
    @staticmethod
    def _flatten_tokens(token_list: list) -> set[str]:
        flattened: set[str] = set()
        for entry in token_list or []:
            try:
                tokens = entry.get("tokens", [])
                flattened.update(str(t) for t in tokens)
            except Exception:
                continue
        return flattened

    def swap_subscriptions(
        self, new_token_list: list, *, old_token_list: Optional[list] = None
    ) -> tuple[list, bool]:
        """
        Unsubscribe tokens that are no longer needed, then subscribe to the new list.
        Returns (unsubscribe_payload, unsubscribe_succeeded).
        """
        with self._subscription_lock:
            old_list = old_token_list if old_token_list is not None else self.token_list
            removed_by_exchange: dict[int, list[str]] = {}

            new_tokens_flat = self._flatten_tokens(new_token_list)
            for entry in old_list or []:
                ex_type = entry.get("exchangeType")
                if ex_type is None:
                    continue
                old_tokens = [str(t) for t in entry.get("tokens", [])]
                removed = [t for t in old_tokens if t not in new_tokens_flat]
                if removed:
                    removed_by_exchange.setdefault(int(ex_type), []).extend(removed)

            removed_payload = [
                {"exchangeType": ex_type, "tokens": tokens}
                for ex_type, tokens in removed_by_exchange.items()
            ]

            # Try unsubscribe first so the socket stops sending old tokens
            unsub_ok = False
            if removed_payload and hasattr(self.sws, "unsubscribe"):
                try:
                    self.sws.unsubscribe(
                        self.subscribe_name, self.subscribe_mode, removed_payload
                    )
                    unsub_ok = True
                    logger.info(
                        "Unsubscribed %d token groups before resubscribe",
                        len(removed_payload),
                    )
                except Exception as exc:  # pragma: no cover - defensive log only
                    logger.warning("Unsubscribe failed, keeping old labels alive: %s", exc)

            self.resubscribe(new_token_list)
            return removed_payload, unsub_ok

    # Attach user-provided callbacks and wrap error/close handlers.
    def set_handlers(
        self,
        *,
        on_data: Callable,
        on_control: Optional[Callable] = None,
        on_error: Optional[Callable] = None,
        on_close: Optional[Callable] = None,
        on_open: Optional[Callable] = None,
    ) -> None:
        labeled_metrics = get_labeled_metrics()

        def _wrapped_data(wsapp, message):
            labeled_metrics.inc_counter(
                "stream_ws_ticks_received_total",
                labels={"source": "smartapi"},
            )
            on_data(wsapp, message)

        self.sws.on_data = _wrapped_data
        if on_control:
            self.sws.on_control_message = on_control
        self._user_on_error = on_error
        self._user_on_close = on_close

        def _wrapped_error(wsapp, error):
            self.state.set_status("error", error=str(error))
            self._log_error_throttled(f"WebSocket error: {error}", level=logging.WARNING)
            if self._user_on_error:
                self._user_on_error(wsapp, error)

        def _wrapped_close(wsapp, *args, **kwargs):
            self.state.set_status("disconnected")
            if self._user_on_close:
                self._user_on_close(wsapp, *args, **kwargs)

        self.sws.on_error = _wrapped_error
        self.sws.on_close = _wrapped_close
        self._user_on_open = on_open

    # Run the websocket connection loop with reconnect backoff.
    def start(self) -> None:
        """
        Run the websocket client with reconnect + backoff.

        This is expected to be executed in a background thread so it never
        blocks the main FastAPI/uvicorn event loop.
        """
        backoff = 1.0
        max_backoff = 30.0
        while not self._closed:
            now_mono = time.monotonic()
            if now_mono < self._circuit_open_until_mono:
                sleep_seconds = self._circuit_open_until_mono - now_mono
                self.state.set_status("circuit_open")
                self._log_error_throttled(
                    "WebSocket circuit breaker delaying reconnect for %.1fs"
                    % sleep_seconds,
                    level=logging.WARNING,
                )
                time.sleep(sleep_seconds)
                continue
            self.state.set_status("connecting")
            start_ts = time.monotonic()
            connection_error = False
            try:
                logger.info("Connecting WebSocket 2.0 for multi-instrument universe...")
                self.sws.connect()
                self.state.set_status("connected")
                # If we got here, connection was successful - keep it alive, don't close immediately
                # The connection will be closed when we hit an exception or self._closed is True
            except WebSocketConnectionClosedException as exc:
                connection_error = True
                self.state.set_status("error", error=str(exc))
                self._log_error_throttled(
                    f"WebSocket connection closed unexpectedly: {exc}",
                    level=logging.WARNING,
                )
                logger.debug("WebSocket closed exception details", exc_info=True)
                self._register_network_failure(str(exc))
            except Exception as exc:  # pragma: no cover - defensive logging
                connection_error = True
                self.state.set_status("error", error=str(exc))
                self._log_error_throttled(
                    f"WebSocket runner encountered an error: {exc}",
                    level=logging.WARNING,
                )
                logger.debug("WebSocket runner exception details", exc_info=True)
                self._register_network_failure(str(exc))
            finally:
                # Only close connection if there was an error
                # If connection succeeded, let it stay open - don't force close in finally!
                if connection_error or self._closed:
                    try:
                        self.sws.close_connection()
                    except Exception:
                        logger.debug(
                            "close_connection failed during cleanup", exc_info=True
                        )
                    self.state.set_status("disconnected")

            if self._closed:
                break

            elapsed = time.monotonic() - start_ts
            if elapsed >= self._min_stable_seconds:
                backoff = 1.0  # reset after a stable run
                self._consecutive_network_failures = 0
                self._circuit_open_until_mono = 0.0
            else:
                backoff = min(max_backoff, backoff * 2 or 1.0)

            logger.info(
                "WebSocket reconnecting after %.1fs (backoff=%.1fs)",
                elapsed,
                backoff,
            )
            time.sleep(backoff)

    # Close the websocket connection and mark it closed.
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            logger.info("Closing WebSocket connection...")
            self.sws.close_connection()
        except Exception as exc:  # pragma: no cover - best effort close
            logger.error("Error while closing WebSocket: %s", exc)
        finally:
            self.state.set_status("disconnected")
