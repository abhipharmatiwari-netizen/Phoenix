"""
Strategy validation and enforcement utilities.

This module provides validators and model mixins to ensure canonical strategy
names are used throughout the application at config loading, API input, and
persistence layers.
"""

import logging
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, field_validator, model_validator

from app.strategies.naming import canonicalize_strategy_name, validate_canonical_name_strict
from app.strategies.registry import get_strategy_metadata, AUTHORIZED_STRATEGIES

logger = logging.getLogger(__name__)


class StrategyNameValidator(BaseModel):
    """Mixin to validate strategy names in Pydantic models."""
    
    strategy_id: str
    
    @field_validator("strategy_id", mode="before")
    @classmethod
    def validate_strategy_id_canonical(cls, v: Any) -> str:
        """Ensure strategy_id is canonical."""
        v_str = str(v or "").strip()
        canonical = canonicalize_strategy_name(
            v_str,
            source=f"{cls.__name__}.strategy_id",
            warn_alias=False,
        )
        if not canonical:
            raise ValueError(f"Invalid strategy_id '{v_str}'")
        return canonical


class StrategyConfigValidator(BaseModel):
    """Validates strategy configuration models."""
    
    strategy_id: str
    tenant_id: str
    broker_account_id: str
    enabled: bool = True
    params: Dict[str, Any] = {}
    underlying: Optional[str] = None
    
    @field_validator("strategy_id")
    @classmethod
    def validate_strategy_id(cls, v: str) -> str:
        """Ensure strategy_id is canonical."""
        canonical = canonicalize_strategy_name(
            v,
            source=f"{cls.__name__}.strategy_id",
            warn_alias=False,
        )
        if not canonical:
            raise ValueError(f"Invalid strategy_id '{v}'")
        return canonical
    
    @field_validator("tenant_id")
    @classmethod
    def validate_tenant_id(cls, v: str) -> str:
        """Ensure tenant_id is not empty."""
        if not v or not str(v).strip():
            raise ValueError("tenant_id cannot be empty")
        return str(v).strip()
    
    @field_validator("broker_account_id")
    @classmethod
    def validate_broker_account_id(cls, v: str) -> str:
        """Ensure broker_account_id is not empty."""
        if not v or not str(v).strip():
            raise ValueError("broker_account_id cannot be empty")
        return str(v).strip()
    
    @model_validator(mode="after")
    def validate_underlying_required_if_not_agnostic(self) -> "StrategyConfigValidator":
        """For non-agnostic strategies, underlying must be specified."""
        metadata = get_strategy_metadata(self.strategy_id)
        if not metadata.underlying_agnostic and not self.underlying:
            raise ValueError(
                f"Strategy '{self.strategy_id}' is not underlying-agnostic. "
                "underlying field is required."
            )
        return self


def validate_strategy_list(
    strategy_names: Union[List[str], List[Dict[str, Any]]],
    context: str = "config",
) -> List[str]:
    """
    Validate a list of strategy names or configs.
    
    Args:
        strategy_names: List of strategy names or config dicts with 'strategy_id'.
        context: Where validation is happening (for error reporting).
        
    Returns:
        List of validated canonical strategy names.
        
    Raises:
        ValueError: If any strategy name is invalid.
    """
    validated: List[str] = []
    seen: set[str] = set()
    
    for name in strategy_names or []:
        if isinstance(name, dict):
            strategy_id = name.get("strategy_id", "")
        else:
            strategy_id = str(name or "").strip()
        
        if not strategy_id:
            raise ValueError(f"Empty strategy name in {context}")
        
        if strategy_id in seen:
            continue  # Deduplicate
        
        canonical = canonicalize_strategy_name(
            strategy_id,
            source=context,
            warn_alias=False,
        )
        if not canonical:
            raise ValueError(f"In {context}: Unknown strategy '{strategy_id}'")
        validated.append(canonical)
        seen.add(canonical)
    
    return validated


def validate_strategy_routing(
    routes: Dict[str, List[Dict[str, str]]],
) -> Dict[str, List[Dict[str, str]]]:
    """
    Validate HubRoutingTable structure (strategy_id → routes).
    
    Args:
        routes: Dictionary mapping strategy_id to list of tenant/account routes.
        
    Returns:
        Validated routes dictionary.
        
    Raises:
        ValueError: If any strategy_id is invalid.
    """
    validated: Dict[str, List[Dict[str, str]]] = {}
    
    for strategy_id, route_list in routes.items():
        valid_strategy_id = canonicalize_strategy_name(
            str(strategy_id or "").strip(),
            source="validate_strategy_routing",
            warn_alias=False,
        )
        if not valid_strategy_id:
            raise ValueError(f"Unknown strategy '{strategy_id}'")
        
        validated_routes: List[Dict[str, str]] = []
        for route in route_list or []:
            if isinstance(route, dict):
                tenant_id = route.get("tenant_id", "").strip()
                broker_account_id = route.get("broker_account_id", "").strip()
                
                if not tenant_id or not broker_account_id:
                    raise ValueError(
                        f"Route for strategy '{strategy_id}' missing "
                        "tenant_id or broker_account_id"
                    )
                
                validated_routes.append({
                    "tenant_id": tenant_id,
                    "broker_account_id": broker_account_id,
                })
        
        if validated_routes:
            validated[valid_strategy_id] = validated_routes
    
    return validated


def validate_control_tower_matrix(
    matrix: Dict[str, Dict[str, bool]],
) -> Dict[str, Dict[str, bool]]:
    """
    Validate Control Tower enablement matrix (tenant_id → strategy_id → bool).
    
    Args:
        matrix: Dict[tenant_id][strategy_id] → enabled boolean.
        
    Returns:
        Validated matrix.
        
    Raises:
        ValueError: If any strategy_id is invalid.
    """
    validated: Dict[str, Dict[str, bool]] = {}
    
    for tenant_id, strategies in matrix.items():
        validated_strategies: Dict[str, bool] = {}
        for strategy_id, enabled in strategies.items():
            canonical = canonicalize_strategy_name(
                strategy_id,
                source="validate_control_tower_matrix",
                warn_alias=False,
            )
            if not canonical:
                raise ValueError(f"Unknown strategy '{strategy_id}'")
            validated_strategies[canonical] = bool(enabled)
        
        if validated_strategies:
            validated[tenant_id] = validated_strategies
    
    return validated


def get_strategy_display_name(strategy_id: str) -> str:
    """
    Get human-readable display name for a strategy.
    
    Args:
        strategy_id: Canonical strategy name.
        
    Returns:
        Display name (e.g., "EMA 20 Strategy").
        
    Raises:
        ValueError: If strategy_id is invalid.
    """
    metadata = get_strategy_metadata(strategy_id)
    return metadata.display_name


def list_all_strategies_metadata() -> Dict[str, Dict[str, Any]]:
    """
    Get metadata for all authorized strategies.
    
    Returns:
        Dict mapping strategy_id to metadata dict.
    """
    result: Dict[str, Dict[str, Any]] = {}
    for name, metadata in AUTHORIZED_STRATEGIES.items():
        result[name] = {
            "canonical_name": metadata.canonical_name,
            "display_name": metadata.display_name,
            "description": metadata.description,
            "underlying_agnostic": metadata.underlying_agnostic,
        }
    return result


def validate_strategy_params(
    strategy_id: str,
    params: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Validate strategy parameters (future expansion point).
    
    Args:
        strategy_id: Canonical strategy name.
        params: Strategy parameters dict.
        
    Returns:
        Validated parameters.
        
    Raises:
        ValueError: If validation fails.
    """
    # Ensure strategy_id is valid
    canonical = canonicalize_strategy_name(
        strategy_id,
        source="validate_strategy_params",
        warn_alias=False,
    )
    if not canonical:
        raise ValueError(f"Unknown strategy '{strategy_id}'")
    
    # TODO: Add per-strategy parameter validation
    # For now, just ensure it's a dict
    if not isinstance(params, dict):
        raise ValueError(f"Strategy params must be dict, got {type(params)}")
    
    return dict(params)


class StrategyValidationError(ValueError):
    """Raised when strategy validation fails."""
    pass


def assert_strategy_is_canonical(strategy_id: str, context: str = "") -> None:
    """
    Assert that a strategy_id is canonical. Raises AssertionError if not.
    
    Useful for defensive programming and debugging.
    
    Args:
        strategy_id: Strategy ID to validate.
        context: Optional context for error message.
        
    Raises:
        AssertionError: If not canonical.
    """
    try:
        canonical = canonicalize_strategy_name(
            strategy_id,
            source=context or "assert_strategy_is_canonical",
            warn_alias=False,
        )
        if not canonical:
            raise ValueError(f"Unknown strategy '{strategy_id}'")
    except ValueError as e:
        ctx = f" ({context})" if context else ""
        raise AssertionError(f"Strategy '{strategy_id}' is not canonical{ctx}: {e}")
