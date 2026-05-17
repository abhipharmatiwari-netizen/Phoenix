"""
Canonical strategy identifiers for runtime, hub, PnL, and risk systems.

Migrated from underlying-specific kebab-case IDs (e.g., "ema20-trend") to
canonical snake_case names (e.g., "ema20_strategy"). Underlying is now
stored as separate metadata in StrategyConfig, not part of strategy_id.

These identifiers are kept stable across deployments and become keys in
BigQuery, PnL state, routing tables, and distributed tracing.
"""

from __future__ import annotations

from app.core.identifiers import StrategyId

# Canonical strategy identifiers (all snake_case, stable across deployments)
OPTION_STRATEGY_ID: StrategyId = "option_strategy"
DELTA_STRANGLE_ID: StrategyId = "delta_strangle"
EMA20_STRATEGY_ID: StrategyId = "ema20_strategy"
CE_ORB_ID: StrategyId = "ce_orb"
CE_PDH_RB_ID: StrategyId = "ce_pdh_rb"
PUT_MOMENTUM_SCALPER_ID: StrategyId = "put_momentum_scalper"
PUT_REVERSION_WRITER_ID: StrategyId = "put_reversion_writer"
NIFTY_WEEKLY_CREDIT_SPREADS_ID: StrategyId = "nifty_weekly_credit_spreads"
OI_ML_CE_SELLER_ID: StrategyId = "oi_ml_ce_seller"
EXCLUSIVE_NIFTY_CE_BUY_ID: StrategyId = "exclusive_nifty_ce_buy"
PUT_BUY_ID: StrategyId = "put_buy"

# Deprecated: legacy kebab-case identifiers (for backward compatibility in logs only)
# Do not use these for new code. Use canonical names above.
_LEGACY_NG_OPTIONS_STRATEGY_ID: StrategyId = "ng-options"  # → option_strategy
_LEGACY_NIFTY_OPTIONS_STRATEGY_ID: StrategyId = "nifty-options"  # → option_strategy
_LEGACY_BANKNIFTY_OPTIONS_STRATEGY_ID: StrategyId = "banknifty-options"  # → option_strategy
_LEGACY_NIFTY_DELTA_STRANGLE_STRATEGY_ID: StrategyId = "nifty-delta-strangle"  # → delta_strangle
_LEGACY_BANKNIFTY_DELTA_STRANGLE_STRATEGY_ID: StrategyId = "banknifty-delta-strangle"  # → delta_strangle
_LEGACY_EMA20_TREND_STRATEGY_ID: StrategyId = "ema20-trend"  # → ema20_strategy
_LEGACY_CE_ORB_STRATEGY_ID: StrategyId = "ce-orb"  # → ce_orb
_LEGACY_CE_PDH_RB_STRATEGY_ID: StrategyId = "ce-pdh-rb"  # → ce_pdh_rb
_LEGACY_PUT_MOMENTUM_SCALPER_ID: StrategyId = "put-momentum-scalper"  # → put_momentum_scalper
_LEGACY_PUT_REVERSION_WRITER_ID: StrategyId = "put-reversion-writer"  # → put_reversion_writer
_LEGACY_NIFTY_WEEKLY_CREDIT_SPREADS_ID: StrategyId = "nifty-weekly-credit-spreads"  # → nifty_weekly_credit_spreads
_LEGACY_EXCLUSIVE_NIFTY_CE_BUY_STRATEGY_ID: StrategyId = "exclusive-nifty-ce-buy"  # → exclusive_nifty_ce_buy
_LEGACY_PUT_BUY_STRATEGY_ID: StrategyId = "put-buy"  # → put_buy

__all__ = [
    "OPTION_STRATEGY_ID",
    "DELTA_STRANGLE_ID",
    "EMA20_STRATEGY_ID",
    "CE_ORB_ID",
    "CE_PDH_RB_ID",
    "PUT_MOMENTUM_SCALPER_ID",
    "PUT_REVERSION_WRITER_ID",
    "NIFTY_WEEKLY_CREDIT_SPREADS_ID",
    "OI_ML_CE_SELLER_ID",
    "EXCLUSIVE_NIFTY_CE_BUY_ID",
    "PUT_BUY_ID",
]
