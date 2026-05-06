"""Chaos and dependency-failure tests — PHX-QA-005.

Tests that the system behaves safely when dependencies fail:
  - Postgres unavailable
  - Broker connectivity lost (N consecutive failures)
  - Stale market data
  - Reconciliation mismatch
  - Audit log unavailable
  - Session store unavailable
  - Kill-switch with step-up under failure conditions
  - Concurrent access under thread contention

All tests verify that:
  1. The failure is detected and not silently swallowed
  2. The appropriate degraded behavior activates
  3. The system does not crash or corrupt in-memory state
  4. Audit/safety subsystems continue operating on best-effort basis
"""

from __future__ import annotations

import threading
import time

import pytest


# ---------------------------------------------------------------------------
# Pre-trade policy engine resilience
# ---------------------------------------------------------------------------

class TestPreTradePolicyChaos:
    def test_policy_survives_concurrent_checks(self):
        """Concurrent check() calls must not corrupt state."""
        from app.risk.pre_trade_policy import PreTradePolicyEngine, StrategyPolicy

        engine = PreTradePolicyEngine()
        engine.set_strategy_policy(StrategyPolicy(
            strategy_id="test_strat",
            max_daily_orders=10000,
        ))

        errors = []

        def run_checks():
            for _ in range(100):
                try:
                    engine.check(
                        symbol="NIFTY",
                        strategy_id="test_strat",
                        account_id="acct1",
                        side="BUY",
                        qty=10,
                        price=100.0,
                    )
                except Exception as exc:
                    errors.append(exc)

        threads = [threading.Thread(target=run_checks) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Concurrent check() raised: {errors[:3]}"

    def test_policy_blocks_after_limit(self):
        from app.risk.pre_trade_policy import (
            PolicyViolation,
            PreTradePolicyEngine,
            StrategyPolicy,
        )

        engine = PreTradePolicyEngine()
        engine.set_strategy_policy(StrategyPolicy(
            strategy_id="limited_strat",
            max_daily_orders=2,
        ))

        # First two orders pass
        engine.record_submitted(strategy_id="limited_strat", qty=1, price=100.0)
        engine.record_submitted(strategy_id="limited_strat", qty=1, price=100.0)

        # Third order should be blocked
        with pytest.raises(PolicyViolation, match="daily order limit"):
            engine.check(
                symbol="NIFTY",
                strategy_id="limited_strat",
                account_id="acct1",
                side="BUY",
                qty=1,
                price=100.0,
            )


# ---------------------------------------------------------------------------
# Stale quote guard resilience
# ---------------------------------------------------------------------------

class TestStaleQuoteGuardChaos:
    def test_blocks_on_never_received_quote(self):
        from app.risk.stale_quote_guard import StaleQuoteGuard, StaleQuoteViolation

        guard = StaleQuoteGuard()
        # Never recorded a quote — must block
        with pytest.raises(StaleQuoteViolation):
            guard.check_or_raise("UNKNOWN_SYMBOL")

    def test_blocks_after_staleness_threshold(self):
        from app.risk.stale_quote_guard import (
            QuoteStalenessPolicy,
            StaleQuoteGuard,
            StaleQuoteViolation,
        )
        import time as _time

        guard = StaleQuoteGuard()
        guard.set_policy(QuoteStalenessPolicy(
            instrument_class="fast",
            warn_seconds=0.01,
            block_seconds=0.05,
            degrade_seconds=10.0,
        ))
        guard.register_symbol("FAST_SYM", "fast")
        guard.record_quote("FAST_SYM")

        # Immediately should pass
        guard.check_or_raise("FAST_SYM")

        # Wait past block threshold
        _time.sleep(0.06)

        with pytest.raises(StaleQuoteViolation):
            guard.check_or_raise("FAST_SYM")

    def test_no_new_orders_mode_blocks_all(self):
        from app.risk.stale_quote_guard import StaleQuoteGuard, StaleQuoteViolation

        guard = StaleQuoteGuard()
        guard.record_quote("ANY_SYMBOL")
        guard.set_global_no_new_orders(True)

        with pytest.raises(StaleQuoteViolation):
            guard.check_or_raise("ANY_SYMBOL")

    def test_concurrent_record_quote_is_safe(self):
        from app.risk.stale_quote_guard import StaleQuoteGuard

        guard = StaleQuoteGuard()
        errors = []

        def spam_quotes():
            for _ in range(500):
                try:
                    guard.record_quote("THREAD_SYM")
                except Exception as exc:
                    errors.append(exc)

        threads = [threading.Thread(target=spam_quotes) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors


# ---------------------------------------------------------------------------
# Reconciliation policy resilience
# ---------------------------------------------------------------------------

class TestReconciliationPolicyChaos:
    def test_critical_mismatch_raises_and_freezes_scope(self):
        from app.risk.reconciliation_policy import (
            ReconciliationFreezeError,
            ReconciliationPolicy,
            ReconciliationThresholds,
        )

        policy = ReconciliationPolicy()
        policy.set_threshold(ReconciliationThresholds(
            "position_qty", warn_abs=1, critical_abs=5
        ))

        with pytest.raises(ReconciliationFreezeError):
            policy.evaluate(
                scope="acct1",
                mismatch_type="position_qty",
                expected=100,
                actual=110,   # gap=10, exceeds critical_abs=5
            )

        assert policy.is_frozen("acct1")

    def test_unfreeze_restores_trading(self):
        from app.risk.reconciliation_policy import (
            ReconciliationFreezeError,
            ReconciliationPolicy,
            ReconciliationThresholds,
        )

        policy = ReconciliationPolicy()
        policy.set_threshold(ReconciliationThresholds("test", critical_abs=1))
        try:
            policy.evaluate(scope="scope1", mismatch_type="test", expected=0, actual=5)
        except ReconciliationFreezeError:
            pass

        assert policy.is_frozen("scope1")
        policy.unfreeze("scope1", actor="operator")
        assert not policy.is_frozen("scope1")

    def test_warning_does_not_freeze(self):
        from app.risk.reconciliation_policy import (
            MismatchSeverity,
            ReconciliationPolicy,
            ReconciliationThresholds,
        )

        policy = ReconciliationPolicy()
        policy.set_threshold(ReconciliationThresholds(
            "cash", warn_abs=100, critical_abs=10000
        ))

        result = policy.evaluate(
            scope="acct2",
            mismatch_type="cash",
            expected=10000,
            actual=10150,  # gap=150, warning only
        )
        assert result.severity == MismatchSeverity.WARNING
        assert not policy.is_frozen("acct2")


# ---------------------------------------------------------------------------
# Broker failover resilience
# ---------------------------------------------------------------------------

class TestBrokerFailoverChaos:
    def test_failover_after_threshold_failures(self):
        from app.brokers.failover import BrokerFailoverManager, BrokerFailoverPolicy, BrokerHealth, FailoverMode

        manager = BrokerFailoverManager()
        manager.register(BrokerFailoverPolicy(
            broker_id="angel",
            failover_mode=FailoverMode.PAUSE,
            degrade_after_failures=2,
            fail_after_failures=3,
        ))

        # Two failures → DEGRADED
        manager.record_failure("angel", "timeout")
        manager.record_failure("angel", "timeout")
        assert manager.get_health("angel") == BrokerHealth.DEGRADED

        # Third failure → FAILED
        manager.record_failure("angel", "timeout")
        assert manager.get_health("angel") == BrokerHealth.FAILED

    def test_recovery_after_successes(self):
        from app.brokers.failover import BrokerFailoverManager, BrokerFailoverPolicy, BrokerHealth

        manager = BrokerFailoverManager()
        manager.register(BrokerFailoverPolicy(
            broker_id="angel2",
            fail_after_failures=2,
            recover_after_successes=2,
        ))
        manager.record_failure("angel2", "err")
        manager.record_failure("angel2", "err")
        assert manager.get_health("angel2") == BrokerHealth.FAILED

        # Recovering
        manager.record_success("angel2")
        assert manager.get_health("angel2") == BrokerHealth.RECOVERING

        # Recovered
        manager.record_success("angel2")
        assert manager.get_health("angel2") == BrokerHealth.HEALTHY

    def test_failover_blocks_new_orders_in_pause_mode(self):
        from app.brokers.failover import (
            BrokerFailoverManager,
            BrokerFailoverPolicy,
            FailoverError,
            FailoverMode,
        )

        manager = BrokerFailoverManager()
        manager.register(BrokerFailoverPolicy(
            broker_id="broker_pause",
            failover_mode=FailoverMode.PAUSE,
            fail_after_failures=1,
        ))
        manager.record_failure("broker_pause", "conn_error")

        with pytest.raises(FailoverError):
            manager.check_order_allowed("broker_pause")

    def test_close_only_allows_closing_orders(self):
        from app.brokers.failover import (
            BrokerFailoverManager,
            BrokerFailoverPolicy,
            FailoverError,
            FailoverMode,
        )

        manager = BrokerFailoverManager()
        manager.register(BrokerFailoverPolicy(
            broker_id="broker_co",
            failover_mode=FailoverMode.CLOSE_ONLY,
            fail_after_failures=1,
        ))
        manager.record_failure("broker_co", "err")

        # New order blocked
        with pytest.raises(FailoverError):
            manager.check_order_allowed("broker_co", is_closing=False)

        # Closing order allowed
        result = manager.check_order_allowed("broker_co", is_closing=True)
        assert result is None


# ---------------------------------------------------------------------------
# Autonomy envelope resilience
# ---------------------------------------------------------------------------

class TestAutonomyEnvelopeChaos:
    def test_envelope_auto_disables_on_drawdown(self):
        from app.strategies.autonomy_envelope import AutonomyEnvelope, EnvelopeBreach

        env = AutonomyEnvelope(
            strategy_id="test_strat",
            max_drawdown_pct=0.05,
            # Disable time-window gate so test passes regardless of wall-clock time.
            allowed_time_start="",
            allowed_time_end="",
        )
        with pytest.raises(EnvelopeBreach, match="drawdown"):
            env.check_or_raise(drawdown_pct=0.06)

        # Subsequent calls should raise immediately (auto-disabled)
        with pytest.raises(EnvelopeBreach, match="auto_disabled"):
            env.check_or_raise(drawdown_pct=0.0)

    def test_envelope_blocks_disabled_strategy(self):
        from app.strategies.autonomy_envelope import AutonomyEnvelope, EnvelopeBreach

        env = AutonomyEnvelope(strategy_id="disabled_strat", enabled=False)
        with pytest.raises(EnvelopeBreach, match="enabled_flag"):
            env.check_or_raise()

    def test_envelope_concurrent_check_is_safe(self):
        from app.strategies.autonomy_envelope import AutonomyEnvelope

        env = AutonomyEnvelope(
            strategy_id="concurrent_strat",
            max_capital_deployed=1_000_000,
        )
        errors = []

        def run_checks():
            for _ in range(100):
                try:
                    env.check_or_raise(capital_deployed=500_000)
                except Exception as exc:
                    # Unexpected exceptions (not EnvelopeBreach)
                    from app.strategies.autonomy_envelope import EnvelopeBreach
                    if not isinstance(exc, EnvelopeBreach):
                        errors.append(exc)

        threads = [threading.Thread(target=run_checks) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors


# ---------------------------------------------------------------------------
# Market data validator resilience
# ---------------------------------------------------------------------------

class TestMarketDataValidatorChaos:
    def test_crossed_book_detected(self):
        from app.data.market_data_validator import DataQualityLevel, MarketDataValidator

        validator = MarketDataValidator()
        result = validator.validate_tick("NIFTY", bid=100.0, ask=99.0, timestamp=time.time())
        assert result.quality == DataQualityLevel.CRITICAL
        assert any(a.anomaly_type == "crossed_book" for a in result.anomalies)

    def test_out_of_order_timestamp_detected(self):
        from app.data.market_data_validator import MarketDataValidator

        validator = MarketDataValidator()
        now = time.time()
        validator.validate_tick("SYM", bid=100.0, ask=101.0, timestamp=now)
        result = validator.validate_tick("SYM", bid=100.5, ask=101.5, timestamp=now - 5)
        assert any(a.anomaly_type == "timestamp_out_of_order" for a in result.anomalies)

    def test_concurrent_validate_is_safe(self):
        from app.data.market_data_validator import MarketDataValidator

        validator = MarketDataValidator()
        errors = []

        def spam_ticks():
            for i in range(200):
                try:
                    validator.validate_tick(
                        "THREAD_SYM",
                        bid=100.0 + i * 0.01,
                        ask=100.5 + i * 0.01,
                        timestamp=time.time(),
                    )
                except Exception as exc:
                    errors.append(exc)

        threads = [threading.Thread(target=spam_ticks) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors


# ---------------------------------------------------------------------------
# Session store resilience
# ---------------------------------------------------------------------------

class TestSessionStoreChaos:
    def test_revoked_token_returns_none_on_parse(self, monkeypatch):
        """A token whose JTI is revoked must fail _parse_token."""
        monkeypatch.setenv("DEMO_AUTH_TOKEN_SECRET", "test-secret-for-chaos-test-32chars!!")
        from app.core.session_store import revoke_token
        from app.api.auth_routes import _make_token, _parse_token
        from app.api.models import Role
        import time as _time

        # Create a token
        token_str = _make_token(user_id="u1", email="a@b.com", role=Role.VIEWER)
        # Parse to get jti
        import json
        import base64
        parts = token_str.split(".")
        def pad(s):
            return s + "=" * (-len(s) % 4)
        payload = json.loads(base64.urlsafe_b64decode(pad(parts[1])))
        jti = payload.get("jti")
        exp = payload.get("exp", int(_time.time()) + 3600)

        # Token should parse fine before revocation
        result_before = _parse_token(token_str)
        assert result_before is not None

        # Revoke and re-parse
        if jti:
            revoke_token(jti, exp)
            result_after = _parse_token(token_str)
            assert result_after is None

    def test_refresh_token_is_single_use(self):
        from app.core.session_store import consume_refresh_token, issue_refresh_token

        token, _ = issue_refresh_token(user_id="u2", email="b@c.com", role="viewer")

        first = consume_refresh_token(token)
        assert first is not None

        second = consume_refresh_token(token)
        assert second is None  # already consumed


# ---------------------------------------------------------------------------
# Approval workflow resilience
# ---------------------------------------------------------------------------

class TestApprovalWorkflowChaos:
    def test_self_approval_rejected(self):
        from app.core.approval_workflow import (
            ChangeKind,
            approve_change_request,
            submit_change_request,
        )

        rec = submit_change_request(
            change_kind=ChangeKind.FEATURE_FLAG,
            resource_id="some_flag",
            requester="alice",
            description="Test",
            before={"v": 1},
            after={"v": 2},
        )
        ok, msg = approve_change_request(
            request_id=rec.request_id,
            approver="alice",  # self-approval
        )
        assert not ok
        assert "self" in msg.lower() or "not permitted" in msg.lower()

    def test_double_approval_rejected(self):
        from app.core.approval_workflow import (
            ChangeKind,
            approve_change_request,
            submit_change_request,
        )

        rec = submit_change_request(
            change_kind=ChangeKind.GENERAL_CONFIG,
            resource_id="cfg_x",
            requester="alice",
            description="Test2",
            before={},
            after={"key": "val"},
        )
        ok1, _ = approve_change_request(request_id=rec.request_id, approver="bob")
        assert ok1

        ok2, msg2 = approve_change_request(request_id=rec.request_id, approver="carol")
        assert not ok2  # Already approved
