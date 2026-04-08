"""Sprint 5 RISK-5.4: Circuit breaker tests."""

from __future__ import annotations

import time

import pytest

from app.risk.circuit_breaker import (
    BreakerState,
    BreakerType,
    CircuitBreakerConfig,
    TradingCircuitBreaker,
)


class TestLossStreakBreaker:
    def test_trips_after_threshold(self):
        cb = TradingCircuitBreaker(
            CircuitBreakerConfig(loss_streak_threshold=3, loss_streak_cooldown_seconds=60)
        )
        for _ in range(3):
            cb.record_trade_result(is_loss=True)

        allowed, reason = cb.is_entry_allowed()
        assert not allowed
        assert "loss_streak" in reason

    def test_resets_on_win(self):
        cb = TradingCircuitBreaker(
            CircuitBreakerConfig(loss_streak_threshold=3)
        )
        cb.record_trade_result(is_loss=True)
        cb.record_trade_result(is_loss=True)
        cb.record_trade_result(is_loss=False)  # Reset
        cb.record_trade_result(is_loss=True)

        allowed, _ = cb.is_entry_allowed()
        assert allowed

    def test_does_not_trip_below_threshold(self):
        cb = TradingCircuitBreaker(
            CircuitBreakerConfig(loss_streak_threshold=5)
        )
        for _ in range(4):
            cb.record_trade_result(is_loss=True)

        allowed, _ = cb.is_entry_allowed()
        assert allowed


class TestRejectRateBreaker:
    def test_trips_on_high_reject_rate(self):
        cb = TradingCircuitBreaker(
            CircuitBreakerConfig(
                loss_streak_enabled=False,
                reject_rate_threshold=0.5,
                reject_rate_min_samples=4,
                reject_rate_window_seconds=300,
            )
        )
        cb.record_order_result(is_reject=True)
        cb.record_order_result(is_reject=True)
        cb.record_order_result(is_reject=True)
        cb.record_order_result(is_reject=False)

        allowed, reason = cb.is_entry_allowed()
        assert not allowed
        assert "reject_rate" in reason

    def test_allows_below_threshold(self):
        cb = TradingCircuitBreaker(
            CircuitBreakerConfig(
                loss_streak_enabled=False,
                reject_rate_threshold=0.5,
                reject_rate_min_samples=4,
            )
        )
        cb.record_order_result(is_reject=True)
        cb.record_order_result(is_reject=False)
        cb.record_order_result(is_reject=False)
        cb.record_order_result(is_reject=False)

        allowed, _ = cb.is_entry_allowed()
        assert allowed

    def test_below_min_samples_allows(self):
        cb = TradingCircuitBreaker(
            CircuitBreakerConfig(
                loss_streak_enabled=False,
                reject_rate_min_samples=10,
            )
        )
        for _ in range(5):
            cb.record_order_result(is_reject=True)

        allowed, _ = cb.is_entry_allowed()
        assert allowed  # Not enough samples


class TestVolatilityBreaker:
    def test_trips_on_high_volatility(self):
        cb = TradingCircuitBreaker(
            CircuitBreakerConfig(
                loss_streak_enabled=False,
                reject_rate_enabled=False,
                volatility_enabled=True,
                volatility_threshold=25.0,
            )
        )
        cb.update_volatility(30.0)

        allowed, reason = cb.is_entry_allowed()
        assert not allowed
        assert "volatility" in reason

    def test_allows_below_threshold(self):
        cb = TradingCircuitBreaker(
            CircuitBreakerConfig(
                loss_streak_enabled=False,
                reject_rate_enabled=False,
                volatility_enabled=True,
                volatility_threshold=25.0,
            )
        )
        cb.update_volatility(20.0)

        allowed, _ = cb.is_entry_allowed()
        assert allowed


class TestAutoRecovery:
    def test_breaker_recovers_after_cooldown(self):
        cb = TradingCircuitBreaker(
            CircuitBreakerConfig(
                loss_streak_threshold=1,
                loss_streak_cooldown_seconds=0.01,  # Very short for testing
            )
        )
        cb.record_trade_result(is_loss=True)
        assert not cb.is_entry_allowed()[0]

        time.sleep(0.02)  # Wait for cooldown
        assert cb.is_entry_allowed()[0]

    def test_manual_reset(self):
        cb = TradingCircuitBreaker(
            CircuitBreakerConfig(loss_streak_threshold=1, loss_streak_cooldown_seconds=3600)
        )
        cb.record_trade_result(is_loss=True)
        assert not cb.is_entry_allowed()[0]

        cb.reset()
        assert cb.is_entry_allowed()[0]


class TestStatus:
    def test_closed_state_when_no_trips(self):
        cb = TradingCircuitBreaker()
        status = cb.get_status()
        assert status.state == BreakerState.CLOSED
        assert not status.entries_blocked

    def test_open_state_when_tripped(self):
        cb = TradingCircuitBreaker(
            CircuitBreakerConfig(loss_streak_threshold=1, loss_streak_cooldown_seconds=60)
        )
        cb.record_trade_result(is_loss=True)
        status = cb.get_status()
        assert status.state == BreakerState.OPEN
        assert status.entries_blocked
        assert len(status.active_trips) == 1
        assert status.active_trips[0].breaker_type == BreakerType.LOSS_STREAK


class TestDailyReset:
    def test_reset_daily_clears_all(self):
        cb = TradingCircuitBreaker(
            CircuitBreakerConfig(loss_streak_threshold=1, loss_streak_cooldown_seconds=3600)
        )
        cb.record_trade_result(is_loss=True)
        assert not cb.is_entry_allowed()[0]

        cb.reset_daily()
        assert cb.is_entry_allowed()[0]
