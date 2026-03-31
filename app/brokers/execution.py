"""Execution port for broker adapters.

This module defines a narrow execution interface used by order routing and
account runners. It keeps order-placement concerns behind one port while
allowing broker clients to keep their richer read/sync APIs.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Protocol

from app.brokers.base import BrokerClient, OrderRequest, OrderResponse, OrderStatus

logger = logging.getLogger(__name__)
_RETRYABLE_MESSAGE_MARKERS = (
    "timeout",
    "timed out",
    "temporar",
    "temporarily",
    "rate limit",
    "rate_limit",
    "too many requests",
    "retry later",
    "service unavailable",
    "unavailable",
    "connection",
    "network",
    "gateway",
    "blocked",
)


class ExecutionPort(Protocol):
    async def place(self, request: OrderRequest) -> OrderResponse: ...

    async def cancel(
        self, broker_order_id: str, *, symbol: Optional[str] = None
    ) -> OrderResponse: ...

    async def status(self, broker_order_id: str) -> Optional[OrderStatus]: ...


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.getenv(name)
    try:
        parsed = int(str(raw).strip()) if raw is not None else int(default)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(int(minimum), parsed)


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.getenv(name)
    try:
        parsed = float(str(raw).strip()) if raw is not None else float(default)
    except (TypeError, ValueError):
        parsed = float(default)
    return max(float(minimum), parsed)


def _contains_retryable_marker(text: object) -> bool:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return False
    return any(marker in lowered for marker in _RETRYABLE_MESSAGE_MARKERS)


class ExecutionCircuitOpenError(RuntimeError):
    """Raised when broker execution calls are blocked by the circuit breaker."""


class ExecutionRetryableResponseError(RuntimeError):
    """Raised when a broker response indicates a transient, retryable failure."""

    def __init__(self, action: str, response: OrderResponse) -> None:
        self.action = str(action or "")
        self.response = response
        status = str(getattr(response, "status", "") or "").strip() or "UNKNOWN"
        message = str(getattr(response, "message", "") or "").strip() or "-"
        super().__init__(
            f"execution_{self.action}_retryable_response:{status}:{message}"
        )


@dataclass
class BrokerExecutionAdapter(ExecutionPort):
    """Default adapter from BrokerClient to ExecutionPort."""

    broker_client: BrokerClient
    resilience_enabled: bool = field(
        default_factory=lambda: _env_bool("EXECUTION_PORT_RESILIENCE_ENABLED", True)
    )
    retry_max_attempts: int = field(
        default_factory=lambda: _env_int("EXECUTION_PORT_RETRY_MAX_ATTEMPTS", 2)
    )
    retry_base_seconds: float = field(
        default_factory=lambda: _env_float("EXECUTION_PORT_RETRY_BASE_SECONDS", 0.5)
    )
    retry_max_seconds: float = field(
        default_factory=lambda: _env_float("EXECUTION_PORT_RETRY_MAX_SECONDS", 5.0)
    )
    circuit_enabled: bool = field(
        default_factory=lambda: _env_bool("EXECUTION_PORT_CIRCUIT_BREAKER_ENABLED", True)
    )
    circuit_failure_threshold: int = field(
        default_factory=lambda: _env_int(
            "EXECUTION_PORT_CIRCUIT_FAILURE_THRESHOLD",
            3,
        )
    )
    circuit_base_seconds: float = field(
        default_factory=lambda: _env_float(
            "EXECUTION_PORT_CIRCUIT_BASE_SECONDS",
            10.0,
        )
    )
    circuit_max_seconds: float = field(
        default_factory=lambda: _env_float(
            "EXECUTION_PORT_CIRCUIT_MAX_SECONDS",
            120.0,
        )
    )
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep
    now_mono: Callable[[], float] = time.monotonic
    _state_lock: threading.RLock = field(init=False, default_factory=threading.RLock)
    _consecutive_failures: dict[str, int] = field(init=False, default_factory=dict)
    _circuit_open_until: dict[str, float] = field(init=False, default_factory=dict)

    def _retry_delay(self, attempt: int) -> float:
        if attempt <= 0:
            return 0.0
        backoff = self.retry_base_seconds * (2 ** max(0, int(attempt) - 1))
        return min(self.retry_max_seconds, float(backoff))

    def _circuit_remaining(self, action: str) -> float:
        if not (self.resilience_enabled and self.circuit_enabled):
            return 0.0
        now = float(self.now_mono())
        with self._state_lock:
            open_until = float(self._circuit_open_until.get(action, 0.0) or 0.0)
        return max(0.0, open_until - now)

    def _reset_failures(self, action: str) -> None:
        with self._state_lock:
            self._consecutive_failures.pop(action, None)
            self._circuit_open_until.pop(action, None)

    def _record_retryable_failure(self, action: str) -> None:
        if not (self.resilience_enabled and self.circuit_enabled):
            return
        with self._state_lock:
            failures = int(self._consecutive_failures.get(action, 0) or 0) + 1
            self._consecutive_failures[action] = failures
            if failures < self.circuit_failure_threshold:
                return
            extra_failures = max(0, failures - self.circuit_failure_threshold)
            open_seconds = min(
                self.circuit_max_seconds,
                self.circuit_base_seconds * (2 ** extra_failures),
            )
            self._circuit_open_until[action] = float(self.now_mono()) + float(open_seconds)
        logger.warning(
            "Execution circuit opened action=%s failures=%d open_seconds=%.2f",
            action,
            failures,
            open_seconds,
        )

    @staticmethod
    def _is_retryable_exception(exc: Exception) -> bool:
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            return True
        if getattr(exc, "retry_after_seconds", None) is not None:
            return True
        return _contains_retryable_marker(exc)

    @staticmethod
    def _is_retryable_response(action: str, response: OrderResponse) -> bool:
        status = str(getattr(response, "status", "") or "").strip().upper()
        if not status:
            return False
        if not _contains_retryable_marker(getattr(response, "message", "")):
            return False
        if status in {"FAILED", "ERROR"}:
            return True
        if action == "place" and status == "REJECTED":
            return True
        return False

    def _ensure_circuit_closed(self, circuit_key: str) -> None:
        remaining = self._circuit_remaining(circuit_key)
        if remaining <= 0.0:
            return
        raise ExecutionCircuitOpenError(
            f"execution_circuit_open:{circuit_key}:retry_after={remaining:.2f}s"
        )

    async def _run_with_resilience(
        self,
        *,
        action: str,
        operation: Callable[[], Awaitable[Any]],
        circuit_key: Optional[str] = None,
    ) -> Any:
        if not self.resilience_enabled:
            return await operation()
        if circuit_key:
            self._ensure_circuit_closed(circuit_key)
        attempts = max(1, int(self.retry_max_attempts))
        for attempt in range(1, attempts + 1):
            try:
                result = await operation()
            except Exception as exc:
                if not self._is_retryable_exception(exc):
                    if circuit_key:
                        self._reset_failures(circuit_key)
                    raise
                if attempt >= attempts:
                    if circuit_key:
                        self._record_retryable_failure(circuit_key)
                    raise
                await self.sleep_fn(self._retry_delay(attempt))
                continue
            if isinstance(result, OrderResponse) and self._is_retryable_response(
                action,
                result,
            ):
                if attempt >= attempts:
                    if circuit_key:
                        self._record_retryable_failure(circuit_key)
                    raise ExecutionRetryableResponseError(action, result)
                await self.sleep_fn(self._retry_delay(attempt))
                continue
            if circuit_key:
                self._reset_failures(circuit_key)
            return result
        return await operation()

    async def place(self, request: OrderRequest) -> OrderResponse:
        is_exit_order = False
        try:
            is_exit_order = bool(getattr(request, "is_exit_order", False))
        except Exception:
            is_exit_order = False
        result = await self._run_with_resilience(
            action="place_exit" if is_exit_order else "place",
            operation=lambda: self.broker_client.place_order(request),
            circuit_key=None if is_exit_order else "place",
        )
        if not isinstance(result, OrderResponse):
            raise RuntimeError("execution place returned non-OrderResponse")
        return result

    async def cancel(
        self, broker_order_id: str, *, symbol: Optional[str] = None
    ) -> OrderResponse:
        cancel_fn = getattr(self.broker_client, "cancel_order", None)
        if not callable(cancel_fn):
            return OrderResponse(
                broker_order_id=broker_order_id or "",
                status="UNSUPPORTED",
                message="cancel_not_supported_by_broker_adapter",
                filled_quantity=0,
                average_price=None,
            )

        result = await self._run_with_resilience(
            action="cancel",
            operation=lambda: _maybe_await(cancel_fn(broker_order_id, symbol=symbol)),
        )
        if isinstance(result, OrderResponse):
            return result
        return OrderResponse(
            broker_order_id=str(broker_order_id or ""),
            status="UNKNOWN",
            message="cancel_response_unrecognized",
            filled_quantity=0,
            average_price=None,
        )

    async def status(self, broker_order_id: str) -> Optional[OrderStatus]:
        status_fn = getattr(self.broker_client, "get_order_status", None)
        if callable(status_fn):
            result = await self._run_with_resilience(
                action="status",
                operation=lambda: _maybe_await(status_fn(broker_order_id)),
            )
            if isinstance(result, OrderStatus) or result is None:
                return result

        try:
            orders = await self._run_with_resilience(
                action="status",
                operation=self.broker_client.get_orders,
            )
        except Exception:
            return None

        for order in orders or []:
            oid = str(getattr(order, "order_id", "") or "").strip()
            if oid and oid == str(broker_order_id or "").strip():
                return order
        return None


class AngelExecutionAdapter(BrokerExecutionAdapter):
    """Execution adapter for Angel broker client."""


class SimulatedExecutionAdapter(BrokerExecutionAdapter):
    """Execution adapter for simulated broker client."""


class ShadowExecutionAdapter(BrokerExecutionAdapter):
    """Execution adapter for shadow broker client."""


def create_execution_port(
    *, broker_client: BrokerClient, broker_type: Optional[str] = None
) -> ExecutionPort:
    """
    Build an execution adapter based on broker type.

    The selection is configuration-driven from the account's broker_type.
    """
    kind = str(broker_type or "").strip().lower()
    if kind == "angel":
        return AngelExecutionAdapter(broker_client=broker_client)
    if kind in {"simulated", "paper"}:
        return SimulatedExecutionAdapter(broker_client=broker_client)
    if kind in {"shadow"}:
        return ShadowExecutionAdapter(broker_client=broker_client)
    return BrokerExecutionAdapter(broker_client=broker_client)


__all__ = [
    "ExecutionPort",
    "ExecutionCircuitOpenError",
    "ExecutionRetryableResponseError",
    "BrokerExecutionAdapter",
    "AngelExecutionAdapter",
    "SimulatedExecutionAdapter",
    "ShadowExecutionAdapter",
    "create_execution_port",
]
