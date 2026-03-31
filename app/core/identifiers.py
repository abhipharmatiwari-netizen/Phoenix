"""
Canonical identifiers shared across the hub (Firestore docs, BigQuery rows, logs).

Naming conventions aligned with the project plan §1.3:
- tenant_id: stable string such as "tenant-default" or "tenant_<shortname>_<seq>".
- broker_account_id: stable per broker account, e.g. "ba_<tenant>_<broker>_<seq>".
- strategy_id: canonical snake_case strategy name like "delta_strangle".

Use these NewTypes everywhere to keep tenant/broker isolation explicit, and rely on
the helper below to build composite hub order identifiers instead of ad-hoc string
concatenation.
"""

from __future__ import annotations

from typing import NamedTuple, NewType

TenantId = NewType("TenantId", str)
BrokerAccountId = NewType("BrokerAccountId", str)
StrategyId = NewType("StrategyId", str)
HubOrderId = NewType("HubOrderId", str)


class HubOrderKey(NamedTuple):
    tenant_id: TenantId
    broker_account_id: BrokerAccountId
    broker_order_id: str


# Build the canonical hub order id from tenant, broker, and broker order ids.
def make_hub_order_id(key: HubOrderKey) -> HubOrderId:
    """
    Build the canonical composite hub order id.

    All hub order identifiers should be constructed through this helper so we log,
    persist, and reconcile orders consistently across tenants and broker accounts.
    """
    return HubOrderId(f"{key.tenant_id}:{key.broker_account_id}:{key.broker_order_id}")


__all__ = [
    "TenantId",
    "BrokerAccountId",
    "StrategyId",
    "HubOrderId",
    "HubOrderKey",
    "make_hub_order_id",
]
