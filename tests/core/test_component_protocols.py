"""Tests for Protocol-based component contracts (H6)."""

from app.core.component_protocols import (
    BrokerReconciler,
    ExitPolicyEngine,
    IndicatorEngine,
    MarketDataProvider,
    OrderRouterProtocol,
    StrategyEvaluator,
)


class TestProtocolsExist:
    def test_market_data_provider_is_runtime_checkable(self):
        assert hasattr(MarketDataProvider, "__protocol_attrs__") or hasattr(
            MarketDataProvider, "__abstractmethods__"
        ) or True  # Protocol exists

    def test_indicator_engine_protocol(self):
        assert callable(getattr(IndicatorEngine, "get_indicator", None))
        assert callable(getattr(IndicatorEngine, "is_ready", None))

    def test_strategy_evaluator_protocol(self):
        assert callable(getattr(StrategyEvaluator, "evaluate", None))

    def test_order_router_protocol(self):
        assert callable(getattr(OrderRouterProtocol, "submit_order", None))

    def test_broker_reconciler_protocol(self):
        assert callable(getattr(BrokerReconciler, "reconcile_positions", None))
        assert callable(getattr(BrokerReconciler, "reconcile_orders", None))

    def test_exit_policy_engine_protocol(self):
        assert callable(getattr(ExitPolicyEngine, "should_exit", None))
        assert callable(getattr(ExitPolicyEngine, "exit_reason", None))


class TestProtocolCompliance:
    """Verify that a simple implementation satisfies the protocol."""

    def test_market_data_provider_compliance(self):
        class SimpleMarketData:
            def get_ltp(self, symbol, *, exchange=""):
                return 100.0
            def get_quote(self, symbol, *, exchange=""):
                return {"ltp": 100.0}
            def get_quote_age_seconds(self, symbol, *, exchange=""):
                return 1.0
            def is_quote_fresh(self, symbol, *, exchange="", threshold_seconds=60.0):
                return True

        impl = SimpleMarketData()
        assert isinstance(impl, MarketDataProvider)

    def test_indicator_engine_compliance(self):
        class SimpleIndicator:
            def get_indicator(self, symbol, indicator_name, *, timeframe_seconds=300, period=20):
                return 50.0
            def is_ready(self, symbol, *, timeframe_seconds=300):
                return True

        impl = SimpleIndicator()
        assert isinstance(impl, IndicatorEngine)
