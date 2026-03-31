"""Logging helpers for the multi-tenant hub."""

from __future__ import annotations

import contextvars
from typing import Any, Optional
import logging

from app.core.identifiers import BrokerAccountId, StrategyId, TenantId

logger = logging.getLogger(__name__)

_correlation_id_ctx: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "phoenix_correlation_id",
    default=None,
)
_request_id_ctx: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "phoenix_request_id",
    default=None,
)


def set_log_context(
    *,
    correlation_id: Optional[str] = None,
    request_id: Optional[str] = None,
) -> tuple[contextvars.Token, contextvars.Token]:
    return (
        _correlation_id_ctx.set(
            str(correlation_id).strip() if correlation_id else None
        ),
        _request_id_ctx.set(str(request_id).strip() if request_id else None),
    )


def reset_log_context(tokens: tuple[contextvars.Token, contextvars.Token]) -> None:
    corr_token, req_token = tokens
    _correlation_id_ctx.reset(corr_token)
    _request_id_ctx.reset(req_token)


def get_log_context() -> tuple[Optional[str], Optional[str]]:
    return _correlation_id_ctx.get(), _request_id_ctx.get()


# Log a structured event with common hub identifiers.
def log_event(
    logger: logging.Logger,
    *,
    event_type: str,
    message: str,
    tenant_id: Optional[TenantId] = None,
    broker_account_id: Optional[BrokerAccountId] = None,
    strategy_id: Optional[StrategyId] = None,
    correlation_id: Optional[str] = None,
    request_id: Optional[str] = None,
    instrument: Optional[str] = None,
    level: int = logging.INFO,
    **extra: Any,
) -> None:
    """
    Log a structured event with standard fields.

    This uses a flat string format for now so Cloud Logging can filter by
    field=value terms (event_type=..., tenant_id=..., correlation_id=...).
    """
    ctx_correlation_id, ctx_request_id = get_log_context()
    resolved_correlation_id = (
        str(correlation_id).strip()
        if correlation_id
        else (str(ctx_correlation_id).strip() if ctx_correlation_id else None)
    )
    resolved_request_id = (
        str(request_id).strip()
        if request_id
        else (str(ctx_request_id).strip() if ctx_request_id else None)
    )
    parts = [f"event_type={event_type}"]
    if tenant_id is not None:
        parts.append(f"tenant_id={tenant_id}")
    if broker_account_id is not None:
        parts.append(f"broker_account_id={broker_account_id}")
    if strategy_id is not None:
        parts.append(f"strategy_id={strategy_id}")
    if resolved_correlation_id:
        parts.append(f"correlation_id={resolved_correlation_id}")
    if resolved_request_id:
        parts.append(f"request_id={resolved_request_id}")
    if instrument:
        parts.append(f"instrument={instrument}")

    for key, value in extra.items():
        parts.append(f"{key}={value}")

    prefix = " ".join(parts)
    logger.log(level, "%s | %s", prefix, message)


__all__ = [
    "get_log_context",
    "log_event",
    "reset_log_context",
    "set_log_context",
]
