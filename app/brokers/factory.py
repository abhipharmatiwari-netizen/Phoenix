"""
BrokerClient factory.

Given a BrokerAccountModel and runtime_mode (DISABLED/PAPER/LIVE/SHADOW),
return an appropriate BrokerClient instance.

For this step we only support:
- Angel (real) via AngelBrokerClient
- Simulated broker for other or unknown broker_type.
"""

from __future__ import annotations

import logging

from app.brokers.angel_client import AngelBrokerClient
from app.brokers.base import BrokerClient
from app.brokers.secret_loader import resolve_angel_secrets
from app.brokers.shadow_client import ShadowBrokerClient
from app.brokers.simulated_client import SimulatedBrokerClient
from app.tenants.models import BrokerAccountModel

logger = logging.getLogger(__name__)


def create_broker_client(
    account: BrokerAccountModel,
    runtime_mode: str,
) -> BrokerClient:
    """
    Build a BrokerClient for a given account and runtime mode.

    - If runtime_mode == "SHADOW" -> ShadowBrokerClient (never calls Angel APIs).
    - If broker_type == "angel" -> AngelBrokerClient with per-account secrets.
    - Fallback to SimulatedBrokerClient for unknown broker_type.
    """
    broker_type = (account.broker_type or "").lower()
    mode = runtime_mode.upper()

    if mode == "SHADOW":
        logger.info(
            "Using ShadowBrokerClient for broker_account_id=%s broker_type=%s mode=%s execution_mode=SHADOW broker_call_blocked=true",
            account.broker_account_id,
            account.broker_type,
            mode,
        )
        return ShadowBrokerClient(account)

    if broker_type == "angel":
        secrets = resolve_angel_secrets(account)
        return AngelBrokerClient(account=account, secrets=secrets)

    logger.warning(
        "Using SimulatedBrokerClient for broker_account_id=%s broker_type=%s mode=%s",
        account.broker_account_id,
        account.broker_type,
        mode,
    )
    return SimulatedBrokerClient(account)


__all__ = ["create_broker_client"]
