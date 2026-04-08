"""Sprint 5: Tests for new interceptors (ExposureLimit, CircuitBreaker)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Optional

import pytest

from app.brokers.base import (
    OrderRequest,
    OrderResponse,
    OrderSide,
    OrderType,
    ProductType,
    TimeInForce,
)
from app.orders.interceptors import (
    CircuitBreakerInterceptor,
    ExposureLimitInterceptor,
    OrderInterceptionContext,
)
from app.risk.circuit_breaker import CircuitBreakerConfig, TradingCircuitBreaker
from app.risk.exposure_limiter import ExposureLimits


def _make_ctx(
    *,
    is_exit: bool = False,
    positions=None,
    exposure_limits: Optional[ExposureLimits] = None,
    circuit_breaker: Optional[TradingCircuitBreaker] = None,
    instrument_meta: Optional[dict] = None,
    reference_price: float = 100.0,
    quantity: int = 1,
) -> OrderInterceptionContext:
    return OrderInterceptionContext(
        tenant_id="t1",
        broker_account_id="a1",
        strategy_id="s1",
        correlation_id=None,
        request_id=None,
        order_req=OrderRequest(
            symbol="NIFTY25000CE",
            quantity=quantity,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
            time_in_force=TimeInForce.DAY,
        ),
        settings=SimpleNamespace(),
        is_exit_order=is_exit,
        lot_size=1,
        instrument_meta=instrument_meta or {"underlying": "NIFTY", "instrument_type": ""},
        reference_price=reference_price,
        balance=None,
        positions=positions or [],
        capital_engine=None,
        risk_engine=None,
        profit_engine=None,
        exposure_limits=exposure_limits,
        circuit_breaker=circuit_breaker,
    )


class TestExposureLimitInterceptor:
    def test_allows_within_limits(self):
        interceptor = ExposureLimitInterceptor()
        ctx = _make_ctx(exposure_limits=ExposureLimits(max_open_positions=20))
        result = interceptor.evaluate(ctx)
        assert result is None  # No rejection

    def test_blocks_at_max_positions(self):
        positions = [SimpleNamespace(quantity=1, underlying="NIFTY", ltp=100.0, product_type="", side="BUY", instrument_type="") for _ in range(20)]
        interceptor = ExposureLimitInterceptor()
        ctx = _make_ctx(
            exposure_limits=ExposureLimits(max_open_positions=20),
            positions=positions,
        )
        result = interceptor.evaluate(ctx)
        assert result is not None
        assert result.status == "REJECTED"
        assert "Exposure limit" in result.message

    def test_exits_always_pass(self):
        positions = [SimpleNamespace(quantity=1, underlying="NIFTY", ltp=100.0, product_type="", side="BUY", instrument_type="") for _ in range(20)]
        interceptor = ExposureLimitInterceptor()
        ctx = _make_ctx(
            is_exit=True,
            exposure_limits=ExposureLimits(max_open_positions=20),
            positions=positions,
        )
        result = interceptor.evaluate(ctx)
        assert result is None  # Exit passes

    def test_no_limits_configured_allows(self):
        interceptor = ExposureLimitInterceptor()
        ctx = _make_ctx(exposure_limits=None)
        result = interceptor.evaluate(ctx)
        assert result is None


class TestCircuitBreakerInterceptor:
    def test_allows_when_no_breaker(self):
        interceptor = CircuitBreakerInterceptor()
        ctx = _make_ctx(circuit_breaker=None)
        result = interceptor.evaluate(ctx)
        assert result is None

    def test_blocks_when_tripped(self):
        cb = TradingCircuitBreaker(
            CircuitBreakerConfig(loss_streak_threshold=1, loss_streak_cooldown_seconds=60)
        )
        cb.record_trade_result(is_loss=True)

        interceptor = CircuitBreakerInterceptor()
        ctx = _make_ctx(circuit_breaker=cb)
        result = interceptor.evaluate(ctx)
        assert result is not None
        assert result.status == "REJECTED"
        assert "Circuit breaker" in result.message

    def test_exits_pass_when_tripped(self):
        cb = TradingCircuitBreaker(
            CircuitBreakerConfig(loss_streak_threshold=1, loss_streak_cooldown_seconds=60)
        )
        cb.record_trade_result(is_loss=True)

        interceptor = CircuitBreakerInterceptor()
        ctx = _make_ctx(is_exit=True, circuit_breaker=cb)
        result = interceptor.evaluate(ctx)
        assert result is None  # Exit passes

    def test_allows_when_not_tripped(self):
        cb = TradingCircuitBreaker()
        interceptor = CircuitBreakerInterceptor()
        ctx = _make_ctx(circuit_breaker=cb)
        result = interceptor.evaluate(ctx)
        assert result is None
