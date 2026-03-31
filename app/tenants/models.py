"""
Pydantic models for Firestore control-plane collections (A2):
- tenants
- broker_accounts
- subscriptions
- strategy_configs
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, ConfigDict, field_validator

from app.core.identifiers import BrokerAccountId, StrategyId, TenantId
from app.strategies.naming import (
    canonicalize_strategy_name,
    validate_canonical_name_strict,
)


# Firestore model for a tenant record.
class TenantModel(BaseModel):
    tenant_id: TenantId = Field(
        ..., description="Firestore document id for the tenant."
    )
    name: str
    email: str
    phone: Optional[str] = None
    status: str = "active"
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    notes: Optional[str] = None

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)


# Firestore model for a broker account record.
class BrokerAccountModel(BaseModel):
    broker_account_id: BrokerAccountId = Field(
        ..., description="Firestore document id for the broker account."
    )
    tenant_id: TenantId
    broker_type: str
    display_name: str
    client_code: str
    secret_ref: str
    trading_mode: str = "PAPER"
    enabled: bool = True
    default_strategies: List[StrategyId] = Field(default_factory=list)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    meta: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    @field_validator("default_strategies", mode="before")
    @classmethod
    def validate_default_strategies(
        cls, value: Any
    ) -> List[StrategyId]:
        if value in (None, ""):
            return []
        parsed = value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError("default_strategies must be valid JSON") from exc
        if not isinstance(parsed, list):
            raise ValueError("default_strategies must be a list")
        out: List[StrategyId] = []
        for item in parsed:
            strategy_name = str(item or "").strip()
            validate_canonical_name_strict(strategy_name)
            out.append(StrategyId(strategy_name))
        return out


# Firestore model for a subscription record.
class SubscriptionModel(BaseModel):
    subscription_id: str
    tenant_id: TenantId
    broker_account_id: BrokerAccountId
    mode: str
    start_at: datetime
    end_at: datetime
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)


# Firestore model for a strategy configuration record.
class StrategyConfigModel(BaseModel):
    strategy_config_id: str
    tenant_id: TenantId
    broker_account_id: BrokerAccountId
    strategy_id: StrategyId
    enabled: bool = True
    params: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    @field_validator("strategy_id", mode="before")
    @classmethod
    def validate_strategy_id(cls, value: Any) -> StrategyId:
        strategy_name = canonicalize_strategy_name(
            str(value or "").strip(),
            source="StrategyConfigModel.strategy_id",
            warn_alias=False,
        )
        if not strategy_name:
            raise ValueError(f"Invalid strategy_id '{value}'")
        return StrategyId(strategy_name)


__all__ = [
    "TenantModel",
    "BrokerAccountModel",
    "SubscriptionModel",
    "StrategyConfigModel",
]
