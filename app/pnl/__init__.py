"""
PnL tracking for the multi-tenant hub.

PnLEngine consumes trade events and maintains per-(tenant, account, strategy)
PnL snapshots for use by dashboards and risk logic.
"""

from app.pnl.pnl_engine import PnLEngine
from app.pnl.profit_engine import ProfitConfig, ProfitDecision, ProfitEngine
from app.pnl.state_store import (
    PnLStateStore,
    InMemoryPnLStateStore,
    FirestorePnLStateStore,
    PostgresPnLStateStore,
    RedisPnLStateStore,
    build_pnl_state_store,
)

__all__ = [
    "PnLEngine",
    "ProfitEngine",
    "ProfitDecision",
    "ProfitConfig",
    "PnLStateStore",
    "InMemoryPnLStateStore",
    "FirestorePnLStateStore",
    "PostgresPnLStateStore",
    "RedisPnLStateStore",
    "build_pnl_state_store",
]
