"""Subscription runtime helpers (A2 Control Plane subscription enforcement)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from app.tenants.models import BrokerAccountModel, SubscriptionModel


# Return True if a subscription is active at the given time.
def is_subscription_active(
    subscription: SubscriptionModel, now: Optional[datetime] = None
) -> bool:
    """
    Returns True when now is within [start_at, end_at] (UTC-aware).
    """
    now = now or datetime.now(timezone.utc)
    if subscription.start_at.tzinfo is None:
        start = subscription.start_at.replace(tzinfo=timezone.utc)
    else:
        start = subscription.start_at
    if subscription.end_at.tzinfo is None:
        end = subscription.end_at.replace(tzinfo=timezone.utc)
    else:
        end = subscription.end_at
    return start <= now <= end


# Decide the runtime mode based on account and subscription state.
def compute_account_runtime_mode(
    broker_account: BrokerAccountModel, active_subscription: Optional[SubscriptionModel]
) -> str:
    """
    Decide runtime mode for an account:
    - "DISABLED": no active subscription or broker account disabled.
    - "PAPER": active subscription in PAPER mode.
    - "LIVE": active subscription in LIVE mode.
    - "SHADOW": active subscription in SHADOW mode.
    """
    if not broker_account.enabled:
        return "DISABLED"
    if not active_subscription or not is_subscription_active(active_subscription):
        return "DISABLED"
    sub_mode = (active_subscription.mode or "").upper()
    if sub_mode in {"LIVE", "PAPER", "SHADOW"}:
        return sub_mode
    return "DISABLED"


__all__ = ["is_subscription_active", "compute_account_runtime_mode"]
