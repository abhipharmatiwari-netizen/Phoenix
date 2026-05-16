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
                # PR #283 codex round-18/19 P2: sample EMA periods
                # that match LIVE strategy values. The categorical
                # restriction (vs. continuous int) prevents the
                # optimizer from emitting a period the simulator
                # would have to compute against arbitrary in-memory
                # data that diverges from what live persists.
                # Includes:
                #   - 8: NG_FUT yaml-deployed value (computed
                #     in-memory by live since no persistent column
                #     exists, so the simulator does the same).
                #   - 20 / 30 / 50: persisted columns in
                #     ``indicator_bars`` consumed by live for
                #     NIFTY / BANKNIFTY strategies.
                name="ema_period",
                param_type="categorical",
                categories=[8, 20, 30, 50],
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
            # PR #283 codex round-12 P2: live ``_passes_adx_filter``
            # also enforces ``min_di_spread`` when ADX is on. Sample
            # it independently so the optimizer can find the right
            # DI-spread threshold for each underlying instead of
            # leaving it at a single hard-coded default.
            ParameterSpace(
                name="min_di_spread",
                param_type="float",
                min_value=0.0,
                max_value=15.0,
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
            "min_di_spread": float(params.get("min_di_spread", 0.0)),
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

            # EMA-failure exit. PR #283 codex round-6 P2: live ECN uses
            # a SEPARATE ``ema_fail_buffer_atr`` config field for the
            # exit threshold (``close < ema20 - ema_fail_buffer_atr * atr``
            # for N bars), distinct from the entry ``ema_atr_buffer``.
            # Sampling and exporting them independently lets candidates
            # tune entry and exit buffers separately, matching live.
            ParameterSpace(
                name="ema_fail_buffer_atr",
                param_type="float",
                min_value=0.0,
                max_value=0.50,
            ),
            ParameterSpace(
                name="ema_fail_bars",
                param_type="int",
                min_value=1,
                max_value=8,
            ),
        ]

    @staticmethod
    def format_params(params: Dict[str, Any]) -> Dict[str, Any]:
        """Format parameters for Exclusive Nifty CE strategy.

        PR #283 codex round-18 P2: defaults now mirror the deployed
        yaml (``app/config/strategy_env.yaml``) instead of the class-
        fallback values. Without this, partial caller params silently
        fell back to thresholds that differ from production (e.g.
        ``rsi_min: 52`` deployed vs ``58`` class fallback,
        ``min_adx: 14`` deployed vs ``20`` class fallback,
        ``macd_hist_min: 0.0`` deployed vs ``0.30`` class fallback,
        ``sl_atr: 2.0`` deployed vs ``2.2`` class fallback). Caller
        params still take precedence.
        """
        return {
            "timeframe_seconds": int(params.get("timeframe_seconds", 30)),
            "rsi_min": float(params.get("rsi_min", 52.0)),
            "rsi_max": float(params.get("rsi_max", 72.0)),
            "macd_hist_min": float(params.get("macd_hist_min", 0.0)),
            "allow_near_macd": bool(params.get("allow_near_macd", True)),
            "macd_near": float(params.get("macd_near", 0.0)),
            "ema_atr_buffer": float(params.get("ema_atr_buffer", 0.05)),
            "min_adx": float(params.get("min_adx", 14.0)),
            "min_di_spread": float(params.get("min_di_spread", 0.0)),
            "sl_atr": float(params.get("sl_atr", 2.0)),
            "tp_atr": float(params.get("tp_atr", 2.5)),
            "ema_fail_buffer_atr": float(params.get("ema_fail_buffer_atr", 0.10)),
            "ema_fail_bars": int(params.get("ema_fail_bars", 3)),
            # Session and limits — deployed-yaml values so partial
            # formatter calls don't fall back to the class defaults.
            # PR #283 codex round-18: emit the LIVE key
            # ``squareoff_time`` (one word, what
            # ``ExclusiveNiftyCeBuyStrategy.__init__`` reads at line
            # 157) — NOT the yaml's ``square_off_time`` (two words),
            # which is unused by the live strategy class.
            "session_start": str(params.get("session_start", "10:15")),
            "last_entry_time": str(params.get("last_entry_time", "14:45")),
            "squareoff_time": str(
                params.get("squareoff_time", params.get("square_off_time", "15:15"))
            ),
            "late_start": str(params.get("late_start", "14:45")),
            "max_trades_per_day": int(params.get("max_trades_per_day", 1)),
            "cooldown_bars": int(params.get("cooldown_bars", 2)),
            "late_tp_cap_atr": float(params.get("late_tp_cap_atr", 2.6)),
            "trail_active_atr": float(params.get("trail_active_atr", 0.8)),
            "trail_cushion_atr": float(params.get("trail_cushion_atr", 0.16)),
            "late_trail_active_atr": float(params.get("late_trail_active_atr", 0.6)),
            "late_trail_cushion": float(params.get("late_trail_cushion", 0.08)),
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

            # Option-level exits (live: option_sl_pct, final_tp_r).
            #
            # PR #283 codex round-5 P2: ``partial_tp_r`` was dropped
            # from this param space because the simulator (and live
            # ``on_tick``) does NOT exit on the partial-target level —
            # only stop / final / EOD. Sampling and exporting
            # ``partial_tp_r`` here would produce arbitrary noise with
            # no effect on the backtest score and could mislead
            # operators copying the "best" partial value into live
            # config. The live ``PutMomentumScalperConfig`` still
            # accepts the field (as carryover state in
            # ``OptionPosition.partial_tp``); set it directly in
            # config if/when the live exit path starts honouring it.
            ParameterSpace(
                name="option_sl_pct",
                param_type="float",
                min_value=0.15,
                max_value=0.40,
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

        PR #283 codex round-13 P2: ``entry_start`` / ``entry_end`` are
        threaded through with the deployed yaml defaults
        (NIFTY/BANKNIFTY: 09:20 / 14:45 — see
        ``app/config/strategy_env.yaml``). Without these, the
        simulator's ``_within_entry_window`` falls back to the
        morning/afternoon split path even though the deployed live
        config uses the single-window path.

        PR #283 codex round-18 P2: defaults now mirror the yaml
        OPTIMIZED values instead of the class-fallback values. Many
        deployed params materially differ from the class defaults
        (``rsi_min: 20`` deployed vs ``25`` class, ``rsi_max: 45``
        same, ``rsi_falling_bars_required: 1`` deployed vs ``2``
        class, ``lookback_breakdown_bars: 8`` deployed vs ``10``
        class, ``max_bars_in_trade: 14`` deployed vs ``8`` class,
        ``min_atr_ratio: 0.0008`` deployed vs ``0.0015`` class).
        Caller params still take precedence.
        """
        return {
            "rsi_min": float(params.get("rsi_min", 20.0)),
            "rsi_max": float(params.get("rsi_max", 45.0)),
            "min_atr_ratio": float(params.get("min_atr_ratio", 0.0008)),
            "option_sl_pct": float(params.get("option_sl_pct", 0.25)),
            # PR #283 codex round-5 P2: ``partial_tp_r`` not emitted —
            # the live exit path doesn't honour it (only stop / final /
            # EOD), so a tuned value is noise. See get_parameter_spaces.
            "final_tp_r": float(params.get("final_tp_r", 1.5)),
            "rsi_falling_bars_required": int(params.get("rsi_falling_bars_required", 1)),
            "lookback_breakdown_bars": int(params.get("lookback_breakdown_bars", 8)),
            "max_bars_in_trade": int(params.get("max_bars_in_trade", 14)),
            # PR #283 codex round-13 P2: deployed PM single-window
            # defaults so the simulator's ``_within_entry_window`` uses
            # the live yaml path, not the fallback morning/afternoon
            # split. Sample-time overrides via ``params`` (e.g. an
            # optimizer that sweeps the window) take precedence.
            "entry_start": str(params.get("entry_start", "09:20")),
            "entry_end": str(params.get("entry_end", "14:45")),
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
                # PR #283 codex round-6 P2: ``put_momentum_scalper`` is
                # configured for index PE instruments (NIFTY / BANKNIFTY).
                # ``NG_FUT`` is the natural-gas futures stream used by
                # EMA20; the live PM strategy is not deployed for it.
                # Leaving it in the allowlist would have surfaced PM
                # recommendations on natgas bars that no live route can
                # consume.
                "underlyings": ["NIFTY_IDX", "BANKNIFTY_IDX"],
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
