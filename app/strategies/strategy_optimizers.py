"""
Strategy-specific parameter optimization for EMA20, Exclusive Nifty CE Buy, and Put Momentum Scalper.
Defines parameter spaces and backtesting logic for each strategy.
"""

from __future__ import annotations

import logging
from typing import List, Dict, Any
import numpy as np

from app.strategies.ml_param_optimizer import (
    ParameterSpace,
    ParameterSet,
    BacktestMetrics,
)

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
    """Parameter optimizer for Exclusive Nifty CE Buy strategy."""

    @staticmethod
    def get_parameter_spaces() -> List[ParameterSpace]:
        """Define Exclusive Nifty CE parameter search space."""
        return [
            # Entry conditions
            ParameterSpace(
                name="rsi_threshold",
                param_type="float",
                min_value=30.0,
                max_value=70.0,
            ),
            ParameterSpace(
                name="vol_threshold",
                param_type="float",
                min_value=0.8,
                max_value=2.0,
            ),
            ParameterSpace(
                name="ema_crossover_threshold",
                param_type="float",
                min_value=0.001,
                max_value=0.010,
            ),

            # Exit/Risk management
            ParameterSpace(
                name="sl_pct",
                param_type="float",
                min_value=0.10,
                max_value=0.50,
            ),
            ParameterSpace(
                name="tp_pct",
                param_type="float",
                min_value=0.20,
                max_value=1.00,
            ),
            ParameterSpace(
                name="trail_pct",
                param_type="float",
                min_value=0.0,
                max_value=0.30,
            ),

            # Position management
            ParameterSpace(
                name="max_position_bars",
                param_type="int",
                min_value=5,
                max_value=30,
            ),
            ParameterSpace(
                name="partial_booking_pct",
                param_type="float",
                min_value=0.0,
                max_value=1.0,
            ),
        ]

    @staticmethod
    def format_params(params: Dict[str, Any]) -> Dict[str, Any]:
        """Format parameters for Exclusive Nifty CE strategy."""
        return {
            "rsi_threshold": float(params.get("rsi_threshold", 50)),
            "vol_threshold": float(params.get("vol_threshold", 1.2)),
            "ema_crossover_threshold": float(params.get("ema_crossover_threshold", 0.005)),
            "sl_pct": float(params["sl_pct"]),
            "tp_pct": float(params["tp_pct"]),
            "trail_pct": float(params.get("trail_pct", 0.0)),
            "max_position_bars": int(params.get("max_position_bars", 15)),
            "partial_booking_pct": float(params.get("partial_booking_pct", 0.5)),
        }


class PutMomentumParameterOptimizer:
    """Parameter optimizer for Put Momentum Scalper strategy."""

    @staticmethod
    def get_parameter_spaces() -> List[ParameterSpace]:
        """Define Put Momentum Scalper parameter search space."""
        return [
            # Entry conditions (RSI range)
            ParameterSpace(
                name="rsi_min",
                param_type="float",
                min_value=15.0,
                max_value=35.0,
            ),
            ParameterSpace(
                name="rsi_max",
                param_type="float",
                min_value=40.0,
                max_value=60.0,
            ),

            # Trend confirmation
            ParameterSpace(
                name="trend_ema_period",
                param_type="int",
                min_value=20,
                max_value=50,
            ),
            ParameterSpace(
                name="min_atr_ratio",
                param_type="float",
                min_value=0.001,
                max_value=0.010,
            ),

            # Exit/Risk management
            ParameterSpace(
                name="sl_pct",
                param_type="float",
                min_value=0.15,
                max_value=0.50,
            ),
            ParameterSpace(
                name="tp_pct",
                param_type="float",
                min_value=0.30,
                max_value=1.00,
            ),
            ParameterSpace(
                name="partial_tp_r",
                param_type="float",
                min_value=0.5,
                max_value=1.5,
            ),
            ParameterSpace(
                name="final_tp_r",
                param_type="float",
                min_value=1.0,
                max_value=3.0,
            ),

            # Position management
            ParameterSpace(
                name="max_bars_in_trade",
                param_type="int",
                min_value=4,
                max_value=16,
            ),
            ParameterSpace(
                name="lookback_breakdown_bars",
                param_type="int",
                min_value=5,
                max_value=20,
            ),
        ]

    @staticmethod
    def format_params(params: Dict[str, Any]) -> Dict[str, Any]:
        """Format parameters for Put Momentum Scalper strategy."""
        return {
            "rsi_min": float(params.get("rsi_min", 25)),
            "rsi_max": float(params.get("rsi_max", 45)),
            "trend_ema_period": int(params.get("trend_ema_period", 20)),
            "min_atr_ratio": float(params.get("min_atr_ratio", 0.0015)),
            "sl_pct": float(params["sl_pct"]),
            "tp_pct": float(params["tp_pct"]),
            "partial_tp_r": float(params.get("partial_tp_r", 1.0)),
            "final_tp_r": float(params.get("final_tp_r", 1.5)),
            "max_bars_in_trade": int(params.get("max_bars_in_trade", 8)),
            "lookback_breakdown_bars": int(params.get("lookback_breakdown_bars", 10)),
        }


class StrategyOptimizationRunner:
    """Orchestrates multi-strategy, multi-underlying optimization."""

    def __init__(self):
        self.strategies = {
            "ema20": {
                "optimizer": Ema20ParameterOptimizer,
                "underlyings": ["NIFTY", "BANKNIFTY", "NATURALGAS"],
            },
            "exclusive_nifty_ce": {
                "optimizer": ExclusiveNiftyCeParameterOptimizer,
                "underlyings": ["NIFTY", "BANKNIFTY"],  # Nifty-specific
            },
            "put_momentum": {
                "optimizer": PutMomentumParameterOptimizer,
                "underlyings": ["NIFTY", "BANKNIFTY", "NATURALGAS"],
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
