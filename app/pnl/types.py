"""
PnL-related data types for the multi-tenant hub.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date
from typing import Any, Optional

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
    # Control PnL fields: track short-option legs separately from cash ledger
    control_realized_pnl: float = 0.0      # realized from economic closes only
    control_open_pnl: float = 0.0          # MTM on open positions
    control_open_premium: float = 0.0      # sum of entry premiums (open legs)
    control_open_qty: int = 0              # open short-option leg units
    # Position-tracking fields (ephemeral — not persisted to DB, restored from
    # internal_position_records on startup).  Used to compute display_realized_pnl
    # which corrects the cash-flow realized_pnl for open positions: the cash-flow
    # ledger books SELL proceeds as positive even before a short is covered, so
    # display_realized = realized_pnl + net_open_qty * open_avg_price cancels the
    # open contribution and shows only locked-in P&L from fully closed trades.
    net_open_qty: int = 0                  # signed net open qty (+long, -short)
    open_avg_price: float = 0.0            # weighted average entry price of open position


@dataclass(frozen=True)
class TradeOpenEvent:
    """Signals that a short-option leg has been opened."""
    tenant_id: Any  # TenantId
    broker_account_id: Any  # BrokerAccountId
    strategy_id: Any  # StrategyId
    symbol: str
    qty: int           # positive, units sold short
    entry_price: float
    lot_size: int = 1
    trade_time: Any = None  # datetime


@dataclass(frozen=True)
class TradeCloseEvent:
    """Signals that a short-option leg has been economically closed."""
    tenant_id: Any
    broker_account_id: Any
    strategy_id: Any
    symbol: str
    qty: int           # positive, units bought to close
    exit_price: float
    lot_size: int = 1
    trade_time: Any = None  # datetime


__all__ = [
    "TradeEvent",
    "PnLSnapshot",
    "PnLSnapshotKey",
    "TradeOpenEvent",
    "TradeCloseEvent",
]
