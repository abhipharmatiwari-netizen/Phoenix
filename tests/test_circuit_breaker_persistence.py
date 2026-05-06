from __future__ import annotations

from copy import deepcopy


from app.risk.circuit_breaker import CircuitBreakerConfig, TradingCircuitBreaker


class _MemoryStore:
    def __init__(self) -> None:
        self.state = None
        self.clear_calls = 0

    def load_state(self):
        return deepcopy(self.state)

    def save_state(self, state):
        self.state = deepcopy(state)

    def clear_state(self):
        self.state = None
        self.clear_calls += 1


class _FailingStore:
    def load_state(self):
        raise RuntimeError("load failed")

    def save_state(self, state):
        _ = state
        raise RuntimeError("save failed")

    def clear_state(self):
        raise RuntimeError("clear failed")


def _loss_streak_config() -> CircuitBreakerConfig:
    return CircuitBreakerConfig(
        loss_streak_enabled=True,
        loss_streak_threshold=2,
        loss_streak_cooldown_seconds=600.0,
        reject_rate_enabled=False,
        volatility_enabled=False,
    )


def _reject_rate_config() -> CircuitBreakerConfig:
    return CircuitBreakerConfig(
        loss_streak_enabled=False,
        reject_rate_enabled=True,
        reject_rate_threshold=0.5,
        reject_rate_window_seconds=300.0,
        reject_rate_min_samples=2,
        reject_rate_cooldown_seconds=600.0,
        volatility_enabled=False,
    )


def test_persist_and_hydrate_loss_streak_trip():
    store = _MemoryStore()
    breaker = TradingCircuitBreaker(config=_loss_streak_config(), state_store=store)
    breaker.record_trade_result(is_loss=True)
    breaker.record_trade_result(is_loss=True)

    rehydrated = TradingCircuitBreaker(config=_loss_streak_config(), state_store=store)
    status = rehydrated.get_status()

    assert status.entries_blocked is True
    assert status.state.value == "open"
    assert "loss_streak=2" in str(status.reason or "")


def test_persist_and_hydrate_reject_rate_state():
    store = _MemoryStore()
    breaker = TradingCircuitBreaker(config=_reject_rate_config(), state_store=store)
    breaker.record_order_result(is_reject=True)
    breaker.record_order_result(is_reject=True)

    rehydrated = TradingCircuitBreaker(config=_reject_rate_config(), state_store=store)
    allowed, reason = rehydrated.is_entry_allowed()

    assert allowed is False
    assert "reject_rate" in str(reason or "")
    assert len(rehydrated._order_results) >= 2


def test_reset_clears_persisted_state():
    store = _MemoryStore()
    breaker = TradingCircuitBreaker(config=_loss_streak_config(), state_store=store)
    breaker.record_trade_result(is_loss=True)
    breaker.record_trade_result(is_loss=True)

    breaker.reset()

    assert store.state is None
    assert store.clear_calls == 1
    assert breaker.get_status().entries_blocked is False


def test_store_failures_fall_back_to_in_memory(caplog):
    breaker = TradingCircuitBreaker(
        config=_loss_streak_config(),
        state_store=_FailingStore(),
    )
    caplog.set_level("WARNING")

    breaker.record_trade_result(is_loss=True)
    breaker.record_trade_result(is_loss=True)

    status = breaker.get_status()

    assert status.entries_blocked is True
    assert "circuit_breaker_state" in caplog.text
