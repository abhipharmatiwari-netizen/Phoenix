"""
Firestore helpers for the control plane (A2):
- tenants
- broker_accounts
- subscriptions
- strategy_configs
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Type, TypeVar

try:  # pragma: no cover - optional in lightweight test environments
    from google.cloud import firestore  # type: ignore
except Exception:  # pragma: no cover - optional in lightweight test environments
    firestore = None

from app.config.settings import get_settings
from app.core.identifiers import BrokerAccountId, TenantId
from app.tenants.models import (
    BrokerAccountModel,
    StrategyConfigModel,
    SubscriptionModel,
    TenantModel,
)
from app.tenants.subscription_service import is_subscription_active
from app.tenants import postgres_control_plane as pg_store

logger = logging.getLogger(__name__)

TENANTS_COLLECTION = "tenants"
BROKER_ACCOUNTS_COLLECTION = "broker_accounts"
SUBSCRIPTIONS_COLLECTION = "subscriptions"
STRATEGY_CONFIGS_COLLECTION = "strategy_configs"

_firestore_client: Optional[Any] = None
_ModelT = TypeVar(
    "_ModelT",
    bound=TenantModel | BrokerAccountModel | SubscriptionModel | StrategyConfigModel,
)


def _use_postgres() -> bool:
    return get_settings().control_plane_backend.lower() == "postgres"


def _dual_read_enabled() -> bool:
    settings = get_settings()
    return _use_postgres() and bool(settings.control_plane_dual_read_compare)


def _dual_write_enabled() -> bool:
    settings = get_settings()
    return _use_postgres() and bool(getattr(settings, "control_plane_dual_write", False))


def _log_limit() -> int:
    settings = get_settings()
    return max(0, int(settings.control_plane_dual_read_log_limit or 0))


def _normalize_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _normalize_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_value(v) for v in value]
    return value


def _model_to_dict(model: Any) -> Dict[str, Any]:
    if model is None:
        return {}
    if hasattr(model, "model_dump"):
        return _normalize_value(model.model_dump())
    if hasattr(model, "dict"):
        return _normalize_value(model.dict())
    return _normalize_value(model)


def _compare_single(
    label: str,
    primary: Any,
    secondary: Any,
    id_field: str,
) -> None:
    limit = _log_limit()
    if limit == 0:
        return
    if primary is None and secondary is None:
        return
    if primary is None or secondary is None:
        logger.warning(
            "[DUAL_READ] %s mismatch: primary=%s secondary=%s",
            label,
            "present" if primary is not None else "missing",
            "present" if secondary is not None else "missing",
        )
        return
    p = _model_to_dict(primary)
    s = _model_to_dict(secondary)
    if p != s:
        logger.warning(
            "[DUAL_READ] %s mismatch id=%s",
            label,
            p.get(id_field) or s.get(id_field),
        )


def _compare_list(
    label: str,
    primary: Iterable[Any],
    secondary: Iterable[Any],
    id_field: str,
) -> None:
    limit = _log_limit()
    if limit == 0:
        return
    p_map = {getattr(m, id_field): _model_to_dict(m) for m in primary}
    s_map = {getattr(m, id_field): _model_to_dict(m) for m in secondary}
    missing_secondary = [k for k in p_map.keys() if k not in s_map]
    missing_primary = [k for k in s_map.keys() if k not in p_map]
    if missing_secondary:
        logger.warning(
            "[DUAL_READ] %s missing in secondary (first %d): %s",
            label,
            min(limit, len(missing_secondary)),
            ",".join(str(x) for x in missing_secondary[:limit]),
        )
    if missing_primary:
        logger.warning(
            "[DUAL_READ] %s missing in primary (first %d): %s",
            label,
            min(limit, len(missing_primary)),
            ",".join(str(x) for x in missing_primary[:limit]),
        )
    mismatches = []
    for key in p_map.keys() & s_map.keys():
        if p_map[key] != s_map[key]:
            mismatches.append(key)
    if mismatches:
        logger.warning(
            "[DUAL_READ] %s field mismatches (first %d): %s",
            label,
            min(limit, len(mismatches)),
            ",".join(str(x) for x in mismatches[:limit]),
        )


# ----- Firestore-only helpers (avoid recursion when backend=postgres) -----
def _fs_get_tenant(tenant_id: TenantId) -> Optional[TenantModel]:
    doc = _client().collection(TENANTS_COLLECTION).document(tenant_id).get()
    if not doc.exists:
        return None
    return _doc_to_model(doc, TenantModel, id_field="tenant_id")


def _fs_get_all_active_tenants() -> List[TenantModel]:
    docs = (
        _client()
        .collection(TENANTS_COLLECTION)
        .where(field_path="status", op_string="==", value="active")
        .stream()
    )
    return [
        model
        for doc in docs
        if (model := _doc_to_model(doc, TenantModel, id_field="tenant_id"))
    ]


def _fs_get_all_tenants() -> List[TenantModel]:
    docs = _client().collection(TENANTS_COLLECTION).stream()
    return [
        model
        for doc in docs
        if (model := _doc_to_model(doc, TenantModel, id_field="tenant_id"))
    ]


def _fs_get_broker_account(
    broker_account_id: BrokerAccountId,
) -> Optional[BrokerAccountModel]:
    doc = (
        _client()
        .collection(BROKER_ACCOUNTS_COLLECTION)
        .document(broker_account_id)
        .get()
    )
    if not doc.exists:
        return None
    return _doc_to_model(doc, BrokerAccountModel, id_field="broker_account_id")


def _fs_get_broker_account_by_client_code(client_code: str) -> Optional[BrokerAccountModel]:
    if not client_code:
        return None
    docs = (
        _client()
        .collection(BROKER_ACCOUNTS_COLLECTION)
        .where(field_path="client_code", op_string="==", value=client_code)
        .stream()
    )
    for doc in docs:
        model = _doc_to_model(doc, BrokerAccountModel, id_field="broker_account_id")
        if model is not None:
            return model
    return None


def _fs_get_broker_accounts_for_tenant(tenant_id: TenantId) -> List[BrokerAccountModel]:
    docs = (
        _client()
        .collection(BROKER_ACCOUNTS_COLLECTION)
        .where(field_path="tenant_id", op_string="==", value=tenant_id)
        .stream()
    )
    return [
        model
        for doc in docs
        if (
            model := _doc_to_model(
                doc, BrokerAccountModel, id_field="broker_account_id"
            )
        )
    ]


def _fs_get_all_broker_accounts() -> List[BrokerAccountModel]:
    docs = _client().collection(BROKER_ACCOUNTS_COLLECTION).stream()
    return [
        model
        for doc in docs
        if (
            model := _doc_to_model(
                doc, BrokerAccountModel, id_field="broker_account_id"
            )
        )
    ]


def _fs_get_enabled_broker_accounts() -> List[BrokerAccountModel]:
    docs = (
        _client()
        .collection(BROKER_ACCOUNTS_COLLECTION)
        .where(field_path="enabled", op_string="==", value=True)
        .stream()
    )
    return [
        model
        for doc in docs
        if (
            model := _doc_to_model(
                doc, BrokerAccountModel, id_field="broker_account_id"
            )
        )
    ]


def _fs_get_subscriptions_for_account(
    broker_account_id: BrokerAccountId,
) -> List[SubscriptionModel]:
    docs = (
        _client()
        .collection(SUBSCRIPTIONS_COLLECTION)
        .where(field_path="broker_account_id", op_string="==", value=broker_account_id)
        .stream()
    )
    return [
        model
        for doc in docs
        if (model := _doc_to_model(doc, SubscriptionModel, id_field="subscription_id"))
    ]


def _fs_get_all_subscriptions() -> List[SubscriptionModel]:
    docs = _client().collection(SUBSCRIPTIONS_COLLECTION).stream()
    return [
        model
        for doc in docs
        if (model := _doc_to_model(doc, SubscriptionModel, id_field="subscription_id"))
    ]


def _fs_get_active_broker_accounts() -> List[BrokerAccountModel]:
    docs = (
        _client()
        .collection(BROKER_ACCOUNTS_COLLECTION)
        .where(field_path="enabled", op_string="==", value=True)
        .stream()
    )
    accounts: List[BrokerAccountModel] = [
        model
        for doc in docs
        if (
            model := _doc_to_model(
                doc, BrokerAccountModel, id_field="broker_account_id"
            )
        )
    ]
    subscriptions = list(_fs_get_all_subscriptions())
    active_account_ids = {
        sub.broker_account_id
        for sub in subscriptions
        if is_subscription_active(sub, now=datetime.now(timezone.utc))
    }
    tenant_ids = {tenant.tenant_id for tenant in _fs_get_all_active_tenants()}
    if tenant_ids:
        accounts = [acc for acc in accounts if acc.tenant_id in tenant_ids]
    else:
        accounts = []
    return [acc for acc in accounts if acc.broker_account_id in active_account_ids]


def _fs_get_strategy_configs_for_account(
    broker_account_id: BrokerAccountId,
) -> List[StrategyConfigModel]:
    docs = (
        _client()
        .collection(STRATEGY_CONFIGS_COLLECTION)
        .where(field_path="broker_account_id", op_string="==", value=broker_account_id)
        .stream()
    )
    return [
        model
        for doc in docs
        if (
            model := _doc_to_model(
                doc, StrategyConfigModel, id_field="strategy_config_id"
            )
        )
    ]


def _fs_get_strategy_configs_for_tenant(tenant_id: TenantId) -> List[StrategyConfigModel]:
    docs = (
        _client()
        .collection(STRATEGY_CONFIGS_COLLECTION)
        .where(field_path="tenant_id", op_string="==", value=tenant_id)
        .stream()
    )
    return [
        model
        for doc in docs
        if (
            model := _doc_to_model(
                doc, StrategyConfigModel, id_field="strategy_config_id"
            )
        )
    ]


def _fs_get_strategy_configs_for_tenant_strategy(
    tenant_id: TenantId, strategy_id: str
) -> List[StrategyConfigModel]:
    docs = (
        _client()
        .collection(STRATEGY_CONFIGS_COLLECTION)
        .where(field_path="tenant_id", op_string="==", value=tenant_id)
        .where(field_path="strategy_id", op_string="==", value=strategy_id)
        .stream()
    )
    return [
        model
        for doc in docs
        if (
            model := _doc_to_model(
                doc, StrategyConfigModel, id_field="strategy_config_id"
            )
        )
    ]


def _fs_upsert_tenant(model: TenantModel) -> TenantModel:
    _client().collection(TENANTS_COLLECTION).document(model.tenant_id).set(
        model.dict(), merge=True
    )
    return model


def _fs_upsert_broker_account(model: BrokerAccountModel) -> BrokerAccountModel:
    _client().collection(BROKER_ACCOUNTS_COLLECTION).document(
        model.broker_account_id
    ).set(model.dict(), merge=True)
    return model


def _fs_upsert_subscription(model: SubscriptionModel) -> SubscriptionModel:
    _client().collection(SUBSCRIPTIONS_COLLECTION).document(model.subscription_id).set(
        model.dict(), merge=True
    )
    return model


def _fs_upsert_strategy_config(model: StrategyConfigModel) -> StrategyConfigModel:
    _client().collection(STRATEGY_CONFIGS_COLLECTION).document(
        model.strategy_config_id
    ).set(model.dict(), merge=True)
    return model
# Return the shared Firestore client (or a mock if enabled).
def _client() -> Any:
    global _firestore_client
    if firestore is None:
        raise RuntimeError("google-cloud-firestore is required for Firestore control-plane operations")
    if _firestore_client is None:
        # Check if we're in mock/development mode
        if os.getenv("MOCK_FIRESTORE", "").lower() == "true":
            logger.warning("MOCK_FIRESTORE enabled: returning mock Firestore client")
            return _create_mock_firestore_client()
        _firestore_client = firestore.Client()
    return _firestore_client


# Create a mock Firestore client for local development.
def _create_mock_firestore_client():
    """Create a mock Firestore client for local development."""
    from unittest.mock import MagicMock
    
    mock_client = MagicMock(spec=firestore.Client)
    # Mock collection and query methods to return empty results
    mock_collection = MagicMock()
    mock_query = MagicMock()
    mock_query.stream.return_value = []
    mock_collection.where.return_value = mock_query
    mock_collection.document.return_value.get.return_value.to_dict.return_value = {}
    mock_client.collection.return_value = mock_collection
    
    return mock_client


# Convert a Firestore document to a model instance.
def _doc_to_model(doc, model_cls: Type[_ModelT], *, id_field: str) -> Optional[_ModelT]:
    data = doc.to_dict()
    if not data:
        return None
    data.setdefault(id_field, doc.id)
    return model_cls(**data)


# ------------------- Tenants ------------------- #


# Create or update a tenant document.
def upsert_tenant(model: TenantModel) -> TenantModel:
    if _use_postgres():
        result = pg_store.upsert_tenant(model)
        if _dual_write_enabled():
            try:
                _fs_upsert_tenant(model)
            except Exception:
                logger.exception(
                    "Dual-write Firestore upsert_tenant failed tenant_id=%s",
                    model.tenant_id,
                )
        return result
    return _fs_upsert_tenant(model)


# Fetch a tenant by id.
def get_tenant(tenant_id: TenantId) -> Optional[TenantModel]:
    if _use_postgres():
        result = pg_store.get_tenant(tenant_id)
        if _dual_read_enabled():
            fs_result = _fs_get_tenant(tenant_id)
            _compare_single("get_tenant", result, fs_result, "tenant_id")
        return result
    return _fs_get_tenant(tenant_id)


# Fetch all active tenants.
def get_all_active_tenants() -> List[TenantModel]:
    if _use_postgres():
        result = pg_store.get_all_active_tenants()
        if _dual_read_enabled():
            fs_result = _fs_get_all_active_tenants()
            _compare_list("get_all_active_tenants", result, fs_result, "tenant_id")
        return result
    return _fs_get_all_active_tenants()


# Fetch all tenants.
def get_all_tenants() -> List[TenantModel]:
    if _use_postgres():
        result = pg_store.get_all_tenants()
        if _dual_read_enabled():
            fs_result = _fs_get_all_tenants()
            _compare_list("get_all_tenants", result, fs_result, "tenant_id")
        return result
    return _fs_get_all_tenants()


# ------------------- Broker accounts ------------------- #


# Create or update a broker account document.
def upsert_broker_account(model: BrokerAccountModel) -> BrokerAccountModel:
    if _use_postgres():
        result = pg_store.upsert_broker_account(model)
        if _dual_write_enabled():
            try:
                _fs_upsert_broker_account(model)
            except Exception:
                logger.exception(
                    "Dual-write Firestore upsert_broker_account failed broker_account_id=%s",
                    model.broker_account_id,
                )
        return result
    return _fs_upsert_broker_account(model)


# Fetch a broker account by id.
def get_broker_account(
    broker_account_id: BrokerAccountId,
) -> Optional[BrokerAccountModel]:
    if _use_postgres():
        result = pg_store.get_broker_account(broker_account_id)
        if _dual_read_enabled():
            fs_result = _fs_get_broker_account(broker_account_id)
            _compare_single(
                "get_broker_account", result, fs_result, "broker_account_id"
            )
        return result
    return _fs_get_broker_account(broker_account_id)


# Fetch a broker account by client code.
def get_broker_account_by_client_code(client_code: str) -> Optional[BrokerAccountModel]:
    if _use_postgres():
        result = pg_store.get_broker_account_by_client_code(client_code)
        if _dual_read_enabled():
            fs_result = _fs_get_broker_account_by_client_code(client_code)
            _compare_single(
                "get_broker_account_by_client_code",
                result,
                fs_result,
                "broker_account_id",
            )
        return result
    return _fs_get_broker_account_by_client_code(client_code)


# Fetch all broker accounts for a tenant.
def get_broker_accounts_for_tenant(tenant_id: TenantId) -> List[BrokerAccountModel]:
    if _use_postgres():
        result = pg_store.get_broker_accounts_for_tenant(tenant_id)
        if _dual_read_enabled():
            fs_result = _fs_get_broker_accounts_for_tenant(tenant_id)
            _compare_list(
                "get_broker_accounts_for_tenant",
                result,
                fs_result,
                "broker_account_id",
            )
        return result
    return _fs_get_broker_accounts_for_tenant(tenant_id)


# Fetch all broker accounts.
def get_all_broker_accounts() -> List[BrokerAccountModel]:
    if _use_postgres():
        result = pg_store.get_all_broker_accounts()
        if _dual_read_enabled():
            fs_result = _fs_get_all_broker_accounts()
            _compare_list(
                "get_all_broker_accounts", result, fs_result, "broker_account_id"
            )
        return result
    return _fs_get_all_broker_accounts()


# Fetch broker accounts that are enabled.
def get_enabled_broker_accounts() -> List[BrokerAccountModel]:
    if _use_postgres():
        result = pg_store.get_enabled_broker_accounts()
        if _dual_read_enabled():
            fs_result = _fs_get_enabled_broker_accounts()
            _compare_list(
                "get_enabled_broker_accounts",
                result,
                fs_result,
                "broker_account_id",
            )
        return result
    return _fs_get_enabled_broker_accounts()


# Fetch enabled broker accounts that also have active subscriptions.
def get_active_broker_accounts() -> List[BrokerAccountModel]:
    if _use_postgres():
        result = pg_store.get_active_broker_accounts()
        if _dual_read_enabled():
            fs_result = _fs_get_active_broker_accounts()
            _compare_list(
                "get_active_broker_accounts",
                result,
                fs_result,
                "broker_account_id",
            )
        return result
    return _fs_get_active_broker_accounts()


# ------------------- Subscriptions ------------------- #


# Create or update a subscription document.
def upsert_subscription(model: SubscriptionModel) -> SubscriptionModel:
    if _use_postgres():
        result = pg_store.upsert_subscription(model)
        if _dual_write_enabled():
            try:
                _fs_upsert_subscription(model)
            except Exception:
                logger.exception(
                    "Dual-write Firestore upsert_subscription failed subscription_id=%s",
                    model.subscription_id,
                )
        return result
    return _fs_upsert_subscription(model)


# Fetch subscriptions for a broker account.
def get_subscriptions_for_account(
    broker_account_id: BrokerAccountId,
) -> List[SubscriptionModel]:
    if _use_postgres():
        result = pg_store.get_subscriptions_for_account(broker_account_id)
        if _dual_read_enabled():
            fs_result = _fs_get_subscriptions_for_account(broker_account_id)
            _compare_list(
                "get_subscriptions_for_account",
                result,
                fs_result,
                "subscription_id",
            )
        return result
    return _fs_get_subscriptions_for_account(broker_account_id)


# Fetch all subscriptions.
def get_all_subscriptions() -> List[SubscriptionModel]:
    if _use_postgres():
        result = pg_store.get_all_subscriptions()
        if _dual_read_enabled():
            fs_result = _fs_get_all_subscriptions()
            _compare_list(
                "get_all_subscriptions", result, fs_result, "subscription_id"
            )
        return result
    return _fs_get_all_subscriptions()


# ------------------- Strategy configs ------------------- #


# Create or update a strategy config document.
def upsert_strategy_config(model: StrategyConfigModel) -> StrategyConfigModel:
    if _use_postgres():
        result = pg_store.upsert_strategy_config(model)
        if _dual_write_enabled():
            try:
                _fs_upsert_strategy_config(model)
            except Exception:
                logger.exception(
                    "Dual-write Firestore upsert_strategy_config failed strategy_config_id=%s",
                    model.strategy_config_id,
                )
        return result
    return _fs_upsert_strategy_config(model)


# Fetch strategy configs for a broker account.
def get_strategy_configs_for_account(
    broker_account_id: BrokerAccountId,
) -> List[StrategyConfigModel]:
    if _use_postgres():
        result = pg_store.get_strategy_configs_for_account(broker_account_id)
        if _dual_read_enabled():
            fs_result = _fs_get_strategy_configs_for_account(broker_account_id)
            _compare_list(
                "get_strategy_configs_for_account",
                result,
                fs_result,
                "strategy_config_id",
            )
        return result
    return _fs_get_strategy_configs_for_account(broker_account_id)


# Fetch all strategy configs for a tenant.
def get_strategy_configs_for_tenant(tenant_id: TenantId) -> List[StrategyConfigModel]:
    """
    Fetch all strategy configs for a tenant.
    """
    if _use_postgres():
        result = pg_store.get_strategy_configs_for_tenant(tenant_id)
        if _dual_read_enabled():
            fs_result = _fs_get_strategy_configs_for_tenant(tenant_id)
            _compare_list(
                "get_strategy_configs_for_tenant",
                result,
                fs_result,
                "strategy_config_id",
            )
        return result
    return _fs_get_strategy_configs_for_tenant(tenant_id)


# Fetch strategy configs for a tenant and strategy id.
def get_strategy_configs_for_tenant_strategy(
    tenant_id: TenantId, strategy_id: str
) -> List[StrategyConfigModel]:
    """
    Fetch strategy configs for a tenant + strategy pair.
    """
    if _use_postgres():
        result = pg_store.get_strategy_configs_for_tenant_strategy(
            tenant_id, strategy_id
        )
        if _dual_read_enabled():
            fs_result = _fs_get_strategy_configs_for_tenant_strategy(
                tenant_id, strategy_id
            )
            _compare_list(
                "get_strategy_configs_for_tenant_strategy",
                result,
                fs_result,
                "strategy_config_id",
            )
        return result
    return _fs_get_strategy_configs_for_tenant_strategy(tenant_id, strategy_id)


__all__ = [
    "TENANTS_COLLECTION",
    "BROKER_ACCOUNTS_COLLECTION",
    "SUBSCRIPTIONS_COLLECTION",
    "STRATEGY_CONFIGS_COLLECTION",
    "upsert_tenant",
    "get_tenant",
    "get_all_active_tenants",
    "get_all_tenants",
    "upsert_broker_account",
    "get_broker_account",
    "get_broker_account_by_client_code",
    "get_broker_accounts_for_tenant",
    "get_all_broker_accounts",
    "get_enabled_broker_accounts",
    "get_active_broker_accounts",
    "upsert_subscription",
    "get_subscriptions_for_account",
    "get_all_subscriptions",
    "upsert_strategy_config",
    "get_strategy_configs_for_account",
    "get_strategy_configs_for_tenant",
    "get_strategy_configs_for_tenant_strategy",
]
