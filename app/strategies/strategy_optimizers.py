"""
Strategy-specific parameter optimization for EMA20, Exclusive Nifty CE Buy, and Put Momentum Scalper.
Defines parameter spaces and backtesting logic for each strategy.
"""

from __future__ import annotations

import logging
from typing import List, Dict, Any

from app.strategies.ml_param_optimizer import ParameterSpace

logger = logging.getLogger(__name__)


class Ema20ParameterOptimizer:
    """Parameter optimizer for EMA20 strategy."""

    @staticmethod
    def get_parameter_spaces() -> List[ParameterSpace]:
        """Define EMA20 parameter search space."""
        return [
            # Core trend detection
            ParameterSpace(
                name="ema_period",
                param_type="int",
                min_value=10,
                max_value=50,
            ),
            ParameterSpace(
                name="signal_timeframe",
                param_type="categorical",
                categories=[60, 300, 600],  # 1min, 5min, 10min
            ),

            # Risk/reward
            ParameterSpace(
                name="sl_pct",
                param_type="float",
                min_value=0.15,
                max_value=0.75,
            ),
            ParameterSpace(
                name="tp_pct",
                param_type="float",
                min_value=0.15,
                max_value=0.75,
            ),

            # Filters
            ParameterSpace(
                name="min_atr",
                param_type="float",
                min_value=0.05,
                max_value=0.50,
            ),
            ParameterSpace(
                name="require_rsi_falling",
                param_type="bool",
            ),
            ParameterSpace(
                name="use_adx_filter",
                param_type="bool",
            ),
            ParameterSpace(
                name="min_adx",
                param_type="float",
                min_value=15.0,
                max_value=35.0,
            ),
        ]

    @staticmethod
    def format_params(params: Dict[str, Any]) -> Dict[str, Any]:
        """Format parameters for EMA20 strategy."""
        return {
            "ema_period": int(params["ema_period"]),
            "signal_timeframe": int(params.get("signal_timeframe", 300)),
            "sl_pct": float(params["sl_pct"]),
            "tp_pct": float(params["tp_pct"]),
            "min_atr": float(params["min_atr"]),
            "require_rsi_falling": bool(params.get("require_rsi_falling", True)),
            "use_adx_filter": bool(params.get("use_adx_filter", False)),
            "min_adx": float(params.get("min_adx", 18.0)),
        }


class ExclusiveNiftyCeParameterOptimizer:
    """Parameter optimizer for Exclusive Nifty CE Buy strategy.

    PR #283 codex round-2: parameter spaces match the live config keys
    in ``ExclusiveNiftyCeBuyStrategy`` (sl_atr/tp_atr, rsi_min/rsi_max,
    macd_hist_min, ema_atr_buffer, min_adx, min_di_spread,
    timeframe_seconds, ema_fail_bars) so the resulting
    ``best_parameters`` can be applied to LIVE without translation.
    """

    @staticmethod
    def get_parameter_spaces() -> List[ParameterSpace]:
        """Define Exclusive Nifty CE parameter search space."""
        return [
            # Signal timeframe (live default: 30s).
            ParameterSpace(
                name="timeframe_seconds",
                param_type="categorical",
                categories=[30, 60, 300],
            ),

            # Entry RSI band (live: rsi_min < rsi < rsi_max).
            ParameterSpace(
                name="rsi_min",
                param_type="float",
                min_value=45.0,
                max_value=65.0,
            ),
            ParameterSpace(
                name="rsi_max",
                param_type="float",
                min_value=65.0,
                max_value=80.0,
            ),

            # MACD histogram floor (live: macd_hist >= macd_hist_min).
            ParameterSpace(
                name="macd_hist_min",
                param_type="float",
                min_value=0.0,
                max_value=1.0,
            ),

            # Distance above EMA20 in ATR units (live: close > ema20 + ema_atr_buffer * atr).
            ParameterSpace(
                name="ema_atr_buffer",
                param_type="float",
                min_value=0.0,
                max_value=0.30,
            ),

            # ADX / DI filters (live: adx >= min_adx; |+DI - -DI| >= min_di_spread).
            ParameterSpace(
                name="min_adx",
                param_type="float",
                min_value=12.0,
                max_value=35.0,
            ),
            ParameterSpace(
                name="min_di_spread",
                param_type="float",
                min_value=0.0,
                max_value=15.0,
            ),

            # ATR-scaled exits (live: sl_atr / tp_atr).
            ParameterSpace(
                name="sl_atr",
                param_type="float",
                min_value=0.8,
                max_value=3.5,
            ),
            ParameterSpace(
                name="tp_atr",
                param_type="float",
                min_value=1.0,
                max_value=4.0,
            ),

            # EMA-failure exit (live: close < ema20 - buffer*atr for N bars).
            ParameterSpace(
                name="ema_fail_bars",
                param_type="int",
                min_value=1,
                max_value=8,
            ),
        ]

    @staticmethod
    def format_params(params: Dict[str, Any]) -> Dict[str, Any]:
        """Format parameters for Exclusive Nifty CE strategy."""
        return {
            "timeframe_seconds": int(params.get("timeframe_seconds", 30)),
            "rsi_min": float(params.get("rsi_min", 58.0)),
            "rsi_max": float(params.get("rsi_max", 72.0)),
            "macd_hist_min": float(params.get("macd_hist_min", 0.30)),
            "ema_atr_buffer": float(params.get("ema_atr_buffer", 0.05)),
            "min_adx": float(params.get("min_adx", 20.0)),
            "min_di_spread": float(params.get("min_di_spread", 5.0)),
            "sl_atr": float(params.get("sl_atr", 2.2)),
            "tp_atr": float(params.get("tp_atr", 2.5)),
            "ema_fail_bars": int(params.get("ema_fail_bars", 3)),
        }


class PutMomentumParameterOptimizer:
    """Parameter optimizer for Put Momentum Scalper strategy.

    PR #283 codex round-2: parameter spaces match
    ``PutMomentumScalperConfig`` keys exactly so the optimizer never
    emits a parameter the live strategy will not read (and the
    formatted output cannot drift from live config semantics).
    """

    @staticmethod
    def get_parameter_spaces() -> List[ParameterSpace]:
        """Define Put Momentum Scalper parameter search space."""
        return [
            # Entry RSI band (live: rsi_min <= rsi <= rsi_max).
            ParameterSpace(
                name="rsi_min",
                param_type="float",
                min_value=15.0,
                max_value=35.0,
            ),
            ParameterSpace(
                name="rsi_max",
                param_type="float",
                min_value=35.0,
                max_value=55.0,
            ),

            # Volatility floor (live: atr/close >= min_atr_ratio).
            ParameterSpace(
                name="min_atr_ratio",
                param_type="float",
                min_value=0.0005,
                max_value=0.0050,
            ),

            # Option-level exits (live: option_sl_pct, partial_tp_r, final_tp_r).
            ParameterSpace(
                name="option_sl_pct",
                param_type="float",
                min_value=0.15,
                max_value=0.40,
            ),
            ParameterSpace(
                name="partial_tp_r",
                param_type="float",
                min_value=0.5,
                max_value=2.0,
            ),
            ParameterSpace(
                name="final_tp_r",
                param_type="float",
                min_value=1.0,
                max_value=3.0,
            ),

            # Signal-quality gates (live keys).
            ParameterSpace(
                name="rsi_falling_bars_required",
                param_type="int",
                min_value=1,
                max_value=4,
            ),
            ParameterSpace(
                name="lookback_breakdown_bars",
                param_type="int",
                min_value=5,
                max_value=20,
            ),
            ParameterSpace(
                name="max_bars_in_trade",
                param_type="int",
                min_value=4,
                max_value=16,
            ),
        ]

    @staticmethod
    def format_params(params: Dict[str, Any]) -> Dict[str, Any]:
        """Format parameters for Put Momentum Scalper strategy.

        Keys match ``PutMomentumScalperConfig`` field names so that
        approving a candidate writes parameters the live strategy will
        actually read.
        """
        return {
            "rsi_min": float(params.get("rsi_min", 25.0)),
            "rsi_max": float(params.get("rsi_max", 45.0)),
            "min_atr_ratio": float(params.get("min_atr_ratio", 0.0015)),
            "option_sl_pct": float(params.get("option_sl_pct", 0.25)),
            "partial_tp_r": float(params.get("partial_tp_r", 1.0)),
            "final_tp_r": float(params.get("final_tp_r", 1.5)),
            "rsi_falling_bars_required": int(params.get("rsi_falling_bars_required", 2)),
            "lookback_breakdown_bars": int(params.get("lookback_breakdown_bars", 10)),
            "max_bars_in_trade": int(params.get("max_bars_in_trade", 8)),
        }


class StrategyOptimizationRunner:
    """Orchestrates multi-strategy, multi-underlying optimization."""

    def __init__(self):
        # PR #283 codex round-2 P1: ``indicator_bars.label`` uses the
        # ``*_IDX`` suffix for NSE indexes (see
        # app/strategies/exclusive_indicator_store.py:100) and ``NG_FUT``
        # for the natural-gas future. The previous bare ``NIFTY`` /
        # ``BANKNIFTY`` / ``NATURALGAS`` defaults returned empty
        # DataFrames and the optimizer silently emitted zero-trade
        # results.
        self.strategies = {
            "ema20": {
                "optimizer": Ema20ParameterOptimizer,
                "underlyings": ["NIFTY_IDX", "BANKNIFTY_IDX", "NG_FUT"],
            },
            "exclusive_nifty_ce": {
                "optimizer": ExclusiveNiftyCeParameterOptimizer,
                "underlyings": ["NIFTY_IDX"],  # Nifty-only (per strategy name)
            },
            "put_momentum": {
                "optimizer": PutMomentumParameterOptimizer,
                "underlyings": ["NIFTY_IDX", "BANKNIFTY_IDX", "NG_FUT"],
            },
        }

    def get_strategies(self) -> Dict[str, Any]:
        """Get all strategies and their configuration."""
        return self.strategies

    def get_parameter_spaces(self, strategy_name: str) -> List[ParameterSpace]:
        """Get parameter spaces for a strategy."""
        if strategy_name not in self.strategies:
            raise ValueError(f"Unknown strategy: {strategy_name}")

        optimizer_class = self.strategies[strategy_name]["optimizer"]
        return optimizer_class.get_parameter_spaces()

    def get_underlyings_for_strategy(self, strategy_name: str) -> List[str]:
        """Get underlyings where a strategy can be optimized."""
        if strategy_name not in self.strategies:
            raise ValueError(f"Unknown strategy: {strategy_name}")

        return self.strategies[strategy_name]["underlyings"]

    def format_parameters(self, strategy_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Format raw parameters for a strategy."""
        if strategy_name not in self.strategies:
            raise ValueError(f"Unknown strategy: {strategy_name}")

        optimizer_class = self.strategies[strategy_name]["optimizer"]
        return optimizer_class.format_params(params)
