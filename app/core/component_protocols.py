"""Protocol-based component contracts for runtime decomposition (Architecture S19.1).

Defines formal Protocol interfaces so that components interact only through
defined methods, preventing cross-boundary state access.  These protocols
support static type checking via mypy/pyright.

Components may continue to co-reside initially, but their contracts must be
explicit and shared mutable state must be minimized.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from app.brokers.base import OrderRequest
from app.core.identifiers import BrokerAccountId, StrategyId, TenantId


# ---------------------------------------------------------------------------
# MarketDataProvider
# ---------------------------------------------------------------------------

@runtime_checkable
class MarketDataProvider(Protocol):
    """Provides market data (quotes, LTP, depth) to downstream consumers."""

    def get_ltp(self, symbol: str, *, exchange: str = "") -> Optional[float]:
        """Return the last traded price for a symbol, or None if unavailable."""
        ...

    def get_quote(self, symbol: str, *, exchange: str = "") -> Optional[Dict[str, Any]]:
        """Return the full quote dict for a symbol."""
        ...

    def get_quote_age_seconds(self, symbol: str, *, exchange: str = "") -> Optional[float]:
        """Return how many seconds since the last quote update."""
        ...

    def is_quote_fresh(
        self, symbol: str, *, exchange: str = "", threshold_seconds: float = 60.0
    ) -> bool:
        """Return True if the quote is within the freshness threshold."""
        ...


# ---------------------------------------------------------------------------
# IndicatorEngine
# ---------------------------------------------------------------------------

@runtime_checkable
class IndicatorEngine(Protocol):
    """Computes technical indicators from market data."""

    def get_indicator(
        self,
        symbol: str,
        indicator_name: str,
        *,
        timeframe_seconds: int = 300,
        period: int = 20,
    ) -> Optional[float]:
        """Return the current value of an indicator, or None if not ready."""
        ...

    def is_ready(self, symbol: str, *, timeframe_seconds: int = 300) -> bool:
        """Return True if enough data has been accumulated for indicators."""
        ...


# ---------------------------------------------------------------------------
# StrategyEvaluator
# ---------------------------------------------------------------------------

@runtime_checkable
class StrategyEvaluator(Protocol):
    """Evaluates strategy signals and produces order intents."""

    def evaluate(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        market_data: MarketDataProvider,
    ) -> List[Dict[str, Any]]:
        """Evaluate the strategy and return a list of order intent dicts."""
        ...

    @property
    def strategy_name(self) -> str:
        """Return the canonical strategy name."""
        ...


# ---------------------------------------------------------------------------
# OrderRouter
# ---------------------------------------------------------------------------

@runtime_checkable
class OrderRouterProtocol(Protocol):
    """Routes order requests to broker execution."""

    async def submit_order(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        order_req: OrderRequest,
    ) -> tuple[str, Any]:
        """Submit an order and return (hub_order_id, response)."""
        ...


# ---------------------------------------------------------------------------
# BrokerReconciler
# ---------------------------------------------------------------------------

@runtime_checkable
class BrokerReconciler(Protocol):
    """Reconciles internal state against broker position/order snapshots."""

    def reconcile_positions(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        broker_positions: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Reconcile broker positions against internal state. Returns reconciliation result."""
        ...

    def reconcile_orders(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        broker_orders: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Reconcile broker orders against internal state. Returns reconciliation result."""
        ...


# ---------------------------------------------------------------------------
# ExitPolicyEngine
# ---------------------------------------------------------------------------

@runtime_checkable
class ExitPolicyEngine(Protocol):
    """Evaluates exit policies (profit sweep, EOD, stop-loss)."""

    def should_exit(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        position: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Return exit order intent dict if an exit is warranted, else None."""
        ...

    def exit_reason(self) -> str:
        """Return the reason for the most recent exit decision."""
        ...


__all__ = [
    "BrokerReconciler",
    "ExitPolicyEngine",
    "IndicatorEngine",
    "MarketDataProvider",
    "OrderRouterProtocol",
    "StrategyEvaluator",
]
