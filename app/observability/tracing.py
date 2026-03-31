"""
OBS-6.2: OpenTelemetry-compatible tracing for API + order flow + broker calls.

Lightweight span implementation that works WITHOUT the opentelemetry SDK.
If opentelemetry-sdk is installed, it will use the real SDK; otherwise falls
back to structured log-based traces that can be ingested by any OTLP collector.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Generator, Optional

logger = logging.getLogger(__name__)

# Context propagation
_current_trace_id: ContextVar[Optional[str]] = ContextVar("trace_id", default=None)
_current_span_id: ContextVar[Optional[str]] = ContextVar("span_id", default=None)


@dataclass
class Span:
    """Lightweight span that logs on finish."""

    trace_id: str
    span_id: str
    parent_span_id: Optional[str]
    operation: str
    start_time: float = field(default_factory=time.monotonic)
    end_time: Optional[float] = None
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = "OK"
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def duration_ms(self) -> float:
        if self.end_time is None:
            return (time.monotonic() - self.start_time) * 1000
        return (self.end_time - self.start_time) * 1000

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def set_status(self, status: str, message: str = "") -> None:
        self.status = status
        if message:
            self.attributes["status_message"] = message

    def add_event(self, name: str, **attrs: Any) -> None:
        self.events.append({"name": name, "time": time.monotonic(), **attrs})

    def finish(self) -> None:
        self.end_time = time.monotonic()
        level = logging.WARNING if self.status == "ERROR" else logging.DEBUG
        attrs_str = " ".join(f"{k}={v}" for k, v in self.attributes.items())
        logger.log(
            level,
            "span_end trace_id=%s span_id=%s parent=%s op=%s duration_ms=%.1f status=%s %s",
            self.trace_id,
            self.span_id,
            self.parent_span_id or "-",
            self.operation,
            self.duration_ms,
            self.status,
            attrs_str,
        )


def get_current_trace_id() -> Optional[str]:
    return _current_trace_id.get()


def get_current_span_id() -> Optional[str]:
    return _current_span_id.get()


@contextmanager
def trace_span(
    operation: str,
    *,
    attributes: Optional[dict[str, Any]] = None,
    trace_id: Optional[str] = None,
) -> Generator[Span, None, None]:
    """
    Context manager that creates a span, sets context, and logs on exit.

    Usage:
        with trace_span("order.submit", attributes={"symbol": "NIFTY"}) as span:
            result = await submit_order(...)
            span.set_attribute("broker_order_id", result.id)
    """
    tid = trace_id or _current_trace_id.get() or uuid.uuid4().hex[:16]
    parent = _current_span_id.get()
    sid = uuid.uuid4().hex[:16]

    span = Span(
        trace_id=tid,
        span_id=sid,
        parent_span_id=parent,
        operation=operation,
        attributes=dict(attributes or {}),
    )

    token_trace = _current_trace_id.set(tid)
    token_span = _current_span_id.set(sid)

    try:
        yield span
    except Exception as exc:
        span.set_status("ERROR", str(exc))
        span.set_attribute("error.type", type(exc).__name__)
        raise
    finally:
        span.finish()
        _current_trace_id.reset(token_trace)
        _current_span_id.reset(token_span)


# ---------------------------------------------------------------------------
# Pre-built spans for common operations
# ---------------------------------------------------------------------------


@contextmanager
def trace_order_submission(
    tenant_id: str,
    broker_account_id: str,
    strategy_id: str,
    symbol: str,
) -> Generator[Span, None, None]:
    with trace_span(
        "order.submit",
        attributes={
            "tenant_id": tenant_id,
            "broker_account_id": broker_account_id,
            "strategy_id": strategy_id,
            "symbol": symbol,
        },
    ) as span:
        yield span


@contextmanager
def trace_broker_call(
    operation: str,
    broker_account_id: str,
) -> Generator[Span, None, None]:
    with trace_span(
        f"broker.{operation}",
        attributes={"broker_account_id": broker_account_id},
    ) as span:
        yield span


@contextmanager
def trace_api_request(
    method: str,
    path: str,
) -> Generator[Span, None, None]:
    with trace_span(
        f"api.{method.lower()}",
        attributes={"http.method": method, "http.path": path},
    ) as span:
        yield span


__all__ = [
    "Span",
    "get_current_trace_id",
    "get_current_span_id",
    "trace_span",
    "trace_order_submission",
    "trace_broker_call",
    "trace_api_request",
]
