"""
Authoritative strategy registry — single source of truth for all strategy metadata.

All strategy information (canonical name, class, display label, runtime ID) is 
defined here. All other modules should read from this registry to eliminate 
ambiguity and ensure consistency across config, runtime, persistence, and APIs.
"""

from dataclasses import dataclass
from typing import Any, Dict, Type

from app.strategies.base import BaseStrategy


@dataclass(frozen=True)
class StrategyMetadata:
    """Immutable metadata for a single strategy."""
    canonical_name: str  # e.g., "ema20_strategy"
    strategy_class: Type[BaseStrategy]
    display_name: str  # e.g., "EMA 20 Strategy"
    description: str
    underlying_agnostic: bool  # True if strategy applies across multiple underlyings


# Placeholder for strategy class imports (to avoid circular imports, these are populated at runtime)
_STRATEGY_CLASSES: Dict[str, Type[BaseStrategy]] = {}


def register_strategy_class(canonical_name: str, cls: Type[BaseStrategy]) -> None:
    """Register a strategy class with the registry. Called during app initialization."""
    _STRATEGY_CLASSES[canonical_name] = cls


# Main strategy registry: authoritative source of truth
AUTHORIZED_STRATEGIES: Dict[str, StrategyMetadata] = {
    # Options-style strategies
    "option_strategy": StrategyMetadata(
        canonical_name="option_strategy",
        strategy_class=None,  # Type set at runtime via register_strategy_class
        display_name="Option Strategy",
        description="Multi-underlying options trading strategy",
        underlying_agnostic=True,
    ),
    # Delta strategies
    "delta_strangle": StrategyMetadata(
        canonical_name="delta_strangle",
        strategy_class=None,
        display_name="Delta Strangle",
        description="Delta-neutral strangle strategy",
        underlying_agnostic=True,
    ),
    # EMA-based strategies
    "ema20_strategy": StrategyMetadata(
        canonical_name="ema20_strategy",
        strategy_class=None,
        display_name="EMA 20 Strategy",
        description="Exponential Moving Average (20-period) trend following",
        underlying_agnostic=True,
    ),
    # Call Entry strategies
    "ce_orb": StrategyMetadata(
        canonical_name="ce_orb",
        strategy_class=None,
        display_name="CE ORB",
        description="Call Entry - Open Range Breakout",
        underlying_agnostic=False,
    ),
    "ce_pdh_rb": StrategyMetadata(
        canonical_name="ce_pdh_rb",
        strategy_class=None,
        display_name="CE PDH RB",
        description="Call Entry - Previous Day High Range Breakout",
        underlying_agnostic=False,
    ),
    # Put strategies
    "exclusive_nifty_ce_buy": StrategyMetadata(
        canonical_name="exclusive_nifty_ce_buy",
        strategy_class=None,
        display_name="Exclusive Nifty CE Buy",
        description="NIFTY-exclusive call entry buy strategy",
        underlying_agnostic=False,
    ),
    "put_momentum_scalper": StrategyMetadata(
        canonical_name="put_momentum_scalper",
        strategy_class=None,
        display_name="Put Momentum Scalper",
        description="Put option momentum scalping strategy",
        underlying_agnostic=True,
    ),
    "put_reversion_writer": StrategyMetadata(
        canonical_name="put_reversion_writer",
        strategy_class=None,
        display_name="Put Reversion Writer",
        description="Put reversion and writing strategy",
        underlying_agnostic=True,
    ),
    "put_buy": StrategyMetadata(
        canonical_name="put_buy",
        strategy_class=None,
        display_name="Put Buy",
        description="Put option buying strategy",
        underlying_agnostic=True,
    ),
    "nifty_weekly_credit_spreads": StrategyMetadata(
        canonical_name="nifty_weekly_credit_spreads",
        strategy_class=None,
        display_name="Nifty Weekly Credit Spreads",
        description="Weekly credit spread strategy on NIFTY",
        underlying_agnostic=False,
    ),
}

# Frozen set of all authorized canonical strategy names
CANONICAL_STRATEGY_NAMES: frozenset[str] = frozenset(AUTHORIZED_STRATEGIES.keys())


def get_strategy_metadata(canonical_name: str) -> StrategyMetadata:
    """
    Retrieve metadata for a canonical strategy name.
    
    Raises:
        ValueError: If canonical_name is not in AUTHORIZED_STRATEGIES.
    """
    if canonical_name not in AUTHORIZED_STRATEGIES:
        raise ValueError(
            f"Unknown strategy '{canonical_name}'. "
            f"Authorized strategies: {sorted(CANONICAL_STRATEGY_NAMES)}"
        )
    return AUTHORIZED_STRATEGIES[canonical_name]


def get_strategy_class(canonical_name: str) -> Type[BaseStrategy]:
    """Get the strategy class for a canonical name."""
    metadata = get_strategy_metadata(canonical_name)
    if canonical_name not in _STRATEGY_CLASSES:
        raise RuntimeError(
            f"Strategy class '{canonical_name}' not registered. "
            "Did you call register_strategy_class during initialization?"
        )
    return _STRATEGY_CLASSES[canonical_name]


def list_all_strategies() -> Dict[str, StrategyMetadata]:
    """Return a copy of all authorized strategies."""
    return AUTHORIZED_STRATEGIES.copy()


def validate_canonical_name(name: str) -> bool:
    """Check if a name is a valid canonical strategy name."""
    return name in CANONICAL_STRATEGY_NAMES


def validate_underlying_agnostic(canonical_name: str) -> bool:
    """Check if a strategy is underlying-agnostic."""
    metadata = get_strategy_metadata(canonical_name)
    return metadata.underlying_agnostic
