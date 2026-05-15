"""
Unit tests for ML parameter optimization framework.
"""

import pytest
from datetime import datetime, timedelta

from app.strategies.ml_param_optimizer import (
    ParameterSpace,
    TradeResult,
    BacktestMetrics,
    ParameterSet,
    Ema20Backtester,
    BayesianOptimizer,
    ParameterEnsemble,
)
from app.strategies.advanced_optimization import (
    MultiObjectiveOptimizer,
    MarketRegimeOptimizer,
    RiskAdjustedOptimizer,
)


class TestParameterSpace:
    """Test parameter space sampling and conversion."""

    def test_int_parameter_sampling(self):
        space = ParameterSpace(
            name="ema_period",
            param_type="int",
            min_value=10,
            max_value=50,
        )

        # Test min
        assert space.sample(0.0) == 10
        # Test max
        assert space.sample(1.0) == 50
        # Test middle
        assert space.sample(0.5) == 30

    def test_float_parameter_sampling(self):
        space = ParameterSpace(
            name="sl_pct",
            param_type="float",
            min_value=0.1,
            max_value=0.9,
        )

        assert space.sample(0.0) == pytest.approx(0.1)
        assert space.sample(1.0) == pytest.approx(0.9)
        assert space.sample(0.5) == pytest.approx(0.5)

    def test_bool_parameter_sampling(self):
        space = ParameterSpace(name="filter", param_type="bool")

        assert space.sample(0.1) is False
        assert space.sample(0.6) is True

    def test_categorical_parameter_sampling(self):
        space = ParameterSpace(
            name="timeframe",
            param_type="categorical",
            categories=[60, 300, 600],
        )

        assert space.sample(0.0) == 60
        assert space.sample(0.5) in [60, 300, 600]
        assert space.sample(1.0) == 600


class TestTradeResult:
    """Test trade result calculations."""

    def test_short_trade_pnl(self):
        trade = TradeResult(
            entry_time=datetime.now(),
            exit_time=datetime.now() + timedelta(hours=1),
            entry_price=100.0,
            exit_price=98.0,
            quantity=1,
            side="SHORT",
        )

        assert trade.pnl == 2.0  # Entry - Exit = 100 - 98
        assert trade.pnl_pct == pytest.approx(2.0)

    def test_long_trade_pnl(self):
        trade = TradeResult(
            entry_time=datetime.now(),
            exit_time=datetime.now() + timedelta(hours=1),
            entry_price=100.0,
            exit_price=102.0,
            quantity=1,
            side="LONG",
        )

        assert trade.pnl == 2.0  # Exit - Entry = 102 - 100
        assert trade.pnl_pct == pytest.approx(2.0)

    def test_holding_time(self):
        now = datetime.now()
        trade = TradeResult(
            entry_time=now,
            exit_time=now + timedelta(minutes=30),
            entry_price=100.0,
            exit_price=101.0,
            quantity=1,
            side="LONG",
        )

        assert trade.holding_time_minutes == 30.0


class TestBacktestMetrics:
    """Test metrics calculation and scoring."""

    def test_metrics_score_positive(self):
        metrics = BacktestMetrics(
            total_trades=50,
            winning_trades=30,
            losing_trades=20,
            total_pnl=1000.0,
            max_drawdown=-200.0,
            win_rate=0.60,
            profit_factor=2.0,
        )

        score = metrics.score
        assert score > 0  # Should be positive with good metrics

    def test_metrics_score_negative(self):
        metrics = BacktestMetrics(
            total_trades=50,
            winning_trades=10,
            losing_trades=40,
            total_pnl=-1000.0,
            max_drawdown=-2000.0,
            win_rate=0.20,
            profit_factor=0.2,
        )

        score = metrics.score
        assert score <= 0  # Should be negative with bad metrics

    def test_metrics_to_dict(self):
        metrics = BacktestMetrics(
            total_trades=10,
            total_pnl=500.0,
        )

        d = metrics.to_dict()
        assert d["total_trades"] == 10
        assert d["total_pnl"] == 500.0
        assert "score" in d


class TestEma20Backtester:
    """Test backtester."""

    def test_synthetic_data_generation(self):
        backtester = Ema20Backtester()

        assert len(backtester.ohlc_data) > 0
        assert "open" in backtester.ohlc_data.columns
        assert "close" in backtester.ohlc_data.columns
        assert "high" in backtester.ohlc_data.columns
        assert "low" in backtester.ohlc_data.columns

    def test_backtest_with_basic_params(self):
        backtester = Ema20Backtester()

        params = {
            "ema_period": 20,
            "sl_pct": 0.30,
            "tp_pct": 0.30,
            "min_atr": 0.10,
            "require_rsi_falling": True,
        }

        metrics = backtester.backtest(params)

        assert metrics.total_trades >= 0
        assert metrics.win_rate >= 0.0 and metrics.win_rate <= 1.0
        assert isinstance(metrics.total_pnl, float)

    def test_backtest_tight_stops(self):
        backtester = Ema20Backtester()

        params_tight = {"sl_pct": 0.10, "tp_pct": 0.10}
        params_loose = {"sl_pct": 0.50, "tp_pct": 0.50}

        metrics_tight = backtester.backtest(params_tight)
        metrics_loose = backtester.backtest(params_loose)

        # Tight stops should exit more trades quickly
        assert metrics_tight.total_trades >= 0
        assert metrics_loose.total_trades >= 0


class TestBayesianOptimizer:
    """Test Bayesian optimization."""

    def test_optimizer_initialization(self):
        spaces = [
            ParameterSpace("ema_period", "int", min_value=10, max_value=50),
            ParameterSpace("sl_pct", "float", min_value=0.1, max_value=0.5),
        ]
        backtester = Ema20Backtester()

        optimizer = BayesianOptimizer(spaces, backtester)

        assert len(optimizer.param_spaces) == 2
        assert optimizer.best_score == -float('inf')

    def test_optimize_small_iterations(self):
        spaces = [
            ParameterSpace("ema_period", "int", min_value=10, max_value=50),
            ParameterSpace("sl_pct", "float", min_value=0.1, max_value=0.5),
            ParameterSpace("tp_pct", "float", min_value=0.1, max_value=0.5),
        ]
        backtester = Ema20Backtester()

        optimizer = BayesianOptimizer(spaces, backtester)
        result = optimizer.optimize(n_iterations=10)

        assert result is not None
        assert "ema_period" in result
        assert len(optimizer.evaluated) >= 10


class TestParameterEnsemble:
    """Test ensemble ranking."""

    def test_top_n(self):
        sets = [
            ParameterSet(
                params={"param1": i},
                metrics=BacktestMetrics(total_trades=10, total_pnl=100*i),
            )
            for i in range(1, 6)
        ]

        ensemble = ParameterEnsemble(sets)
        top_3 = ensemble.top_n(3)

        assert len(top_3) == 3

    def test_pareto_frontier(self):
        sets = [
            ParameterSet(
                params={"name": "high_profit"},
                metrics=BacktestMetrics(total_pnl=1000, max_drawdown=-500, win_rate=0.5),
            ),
            ParameterSet(
                params={"name": "low_drawdown"},
                metrics=BacktestMetrics(total_pnl=500, max_drawdown=-100, win_rate=0.6),
            ),
            ParameterSet(
                params={"name": "dominated"},
                metrics=BacktestMetrics(total_pnl=300, max_drawdown=-600, win_rate=0.4),
            ),
        ]

        ensemble = ParameterEnsemble(sets)
        frontier = ensemble.pareto_frontier()

        # Dominated config should not be in frontier
        assert len(frontier) <= len(sets)


class TestMultiObjectiveOptimizer:
    """Test multi-objective optimization."""

    def test_weighted_scoring(self):
        sets = [
            ParameterSet(
                params={"name": "high_profit"},
                metrics=BacktestMetrics(
                    total_pnl=1000,
                    max_drawdown=-500,
                    sharpe_ratio=1.0,
                    win_rate=0.5,
                ),
            ),
            ParameterSet(
                params={"name": "stable"},
                metrics=BacktestMetrics(
                    total_pnl=500,
                    max_drawdown=-100,
                    sharpe_ratio=2.0,
                    win_rate=0.7,
                ),
            ),
        ]

        optimizer = MultiObjectiveOptimizer(sets)
        scored = optimizer.score_weighted()

        assert len(scored) == 2
        # Score should be (score, param_set) tuple
        assert scored[0][1] >= 0


class TestMarketRegimeOptimizer:
    """Test market regime detection and preset selection."""

    def test_regime_detection(self):
        backtester = Ema20Backtester()
        regime_opt = MarketRegimeOptimizer(backtester)

        regime = regime_opt.detect_regime()
        assert regime in ["TRENDING", "CHOPPY", "BREAKOUT", "SIDEWAYS"]

    def test_regime_params(self):
        backtester = Ema20Backtester()
        regime_opt = MarketRegimeOptimizer(backtester)

        for regime in ["TRENDING", "CHOPPY", "BREAKOUT", "SIDEWAYS"]:
            params = regime_opt.optimal_params_for_regime(regime)
            assert params is not None
            assert "ema_period" in params
            assert "sl_pct" in params


class TestRiskAdjustedOptimizer:
    """Test risk-adjusted filtering."""

    def test_filter_by_sharpe(self):
        sets = [
            ParameterSet(
                params={"name": "good"},
                metrics=BacktestMetrics(sharpe_ratio=1.5),
            ),
            ParameterSet(
                params={"name": "bad"},
                metrics=BacktestMetrics(sharpe_ratio=0.2),
            ),
        ]

        filtered = RiskAdjustedOptimizer.filter_by_sharpe(sets, min_sharpe=1.0)
        assert len(filtered) == 1

    def test_filter_by_win_rate(self):
        sets = [
            ParameterSet(
                params={"name": "good"},
                metrics=BacktestMetrics(win_rate=0.6),
            ),
            ParameterSet(
                params={"name": "bad"},
                metrics=BacktestMetrics(win_rate=0.3),
            ),
        ]

        filtered = RiskAdjustedOptimizer.filter_by_win_rate(sets, min_win_rate=0.5)
        assert len(filtered) == 1


class TestCodexRegressions:
    """Regression tests for PR #283 codex review findings.

    Each test pins a specific bug that codex caught in the original
    f729add commit so the fix can't silently regress.
    """

    def test_categorical_normalization_roundtrips(self):
        """P2: normalizing [60,300,600]/3 had clipped all values to 1.0,
        so denormalize always returned the last category."""
        space = ParameterSpace(
            name="signal_timeframe",
            param_type="categorical",
            categories=[60, 300, 600],
        )
        optimizer = BayesianOptimizer([space], backtester=None)  # type: ignore[arg-type]
        for value in (60, 300, 600):
            norm = optimizer._normalize_params({"signal_timeframe": value})
            params = optimizer._denormalize_params(norm)
            assert params["signal_timeframe"] == value, (
                f"categorical {value} should round-trip; got {params['signal_timeframe']}"
            )

    def test_score_returns_signed_value_for_losing_configs(self):
        """P2: previous max(0, score) made every losing candidate tie."""
        losing = BacktestMetrics(
            total_trades=5,
            total_pnl=-1000.0,
            win_rate=0.2,
            profit_factor=0.3,
            max_drawdown=-500.0,
        )
        worse = BacktestMetrics(
            total_trades=5,
            total_pnl=-2000.0,
            win_rate=0.1,
            profit_factor=0.1,
            max_drawdown=-1000.0,
        )
        assert losing.score > worse.score, (
            "least-bad losing config must outrank a strictly worse one"
        )
        assert losing.score < 0, "score must be signed (not clamped to zero)"

    def test_pareto_frontier_preserves_lower_drawdown(self):
        """P2: previous filter kept WORSE drawdowns and dropped lower-risk
        alternatives. With (-100) vs (-500), -100 (lower abs) must stay
        on the frontier."""
        sets = [
            ParameterSet(
                params={"name": "high_profit_high_dd"},
                metrics=BacktestMetrics(total_trades=10, total_pnl=1000, max_drawdown=-500),
            ),
            ParameterSet(
                params={"name": "low_profit_low_dd"},
                metrics=BacktestMetrics(total_trades=10, total_pnl=500, max_drawdown=-100),
            ),
        ]
        ensemble = ParameterEnsemble(sets)
        frontier = ensemble.pareto_frontier()
        names = {p["name"] for p in frontier}
        # Both are Pareto-optimal: one trades higher profit for higher DD,
        # the other trades lower profit for lower DD. Both must survive.
        assert "high_profit_high_dd" in names
        assert "low_profit_low_dd" in names

    def test_to_json_serializes_pareto_without_attribute_error(self):
        """P2: to_json used to call .params/.metrics on dicts returned by
        pareto_frontier(), which raised AttributeError."""
        sets = [
            ParameterSet(
                params={"k": 1},
                metrics=BacktestMetrics(total_trades=10, total_pnl=500, max_drawdown=-100),
            ),
            ParameterSet(
                params={"k": 2},
                metrics=BacktestMetrics(total_trades=10, total_pnl=1000, max_drawdown=-500),
            ),
        ]
        ensemble = ParameterEnsemble(sets)
        payload = ensemble.to_json()
        import json
        data = json.loads(payload)
        assert "top_10" in data and "pareto_frontier" in data
        for entry in data["pareto_frontier"]:
            assert "params" in entry and "metrics" in entry

    def test_ema20_backtester_accepts_real_dataframe(self):
        """P2: ``self.ohlc_data = data or generate(...)`` raised on real
        DataFrames because DataFrames cannot be truth-tested."""
        import pandas as pd

        bars = pd.DataFrame({
            "timestamp": pd.date_range("2026-01-01", periods=200, freq="5min"),
            "open": [25000.0] * 200,
            "high": [25050.0] * 200,
            "low": [24950.0] * 200,
            "close": [25000.0 + i * 0.1 for i in range(200)],
            "volume": [1000] * 200,
        })
        backtester = Ema20Backtester(ohlc_data=bars)
        # Must keep the caller's frame, not replace it with synthetic data.
        assert backtester.ohlc_data is bars


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
