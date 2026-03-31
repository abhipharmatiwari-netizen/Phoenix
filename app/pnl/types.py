"""
PnL-related data types for the multi-tenant hub.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date
from typing import Optional

from app.core.identifiers import BrokerAccountId, StrategyId, TenantId


# Minimal trade event for PnL accounting.
@dataclass(frozen=True)
class TradeEvent:
    """
    Minimal trade event for PnL accounting.
    """

    tenant_id: TenantId
    broker_account_id: BrokerAccountId
    strategy_id: StrategyId

    symbol: str
    qty: int  # signed, >0 for buy, <0 for sell
    price: float
    trade_time: datetime
    fees: float = 0.0


# Key used to identify a unique PnL snapshot.
@dataclass(frozen=True)
class PnLSnapshotKey:
    tenant_id: TenantId
    broker_account_id: BrokerAccountId
    strategy_id: StrategyId


# Snapshot of realized/unrealized PnL for a key.
@dataclass
class PnLSnapshot:
    key: PnLSnapshotKey
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    gross_exposure: float = 0.0
    as_of: Optional[datetime] = None
    session_date: Optional[date] = None
    freshness_updated_at: Optional[datetime] = None
    freshness_source: Optional[str] = None  # "trade", "mark_update", "broker_sync", "manual"


__all__ = ["TradeEvent", "PnLSnapshot", "PnLSnapshotKey"]
