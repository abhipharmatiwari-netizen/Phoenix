"""
CapitalEngine: simple per-account capital checks.

Phase 1:
- Define config/state and method signatures.
- Implement minimal checks using notional size if basic inputs are available.

Later phases (after data plane wiring) can extend this with:
- StateStore / PnL integration,
- per-strategy sizing rules.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

from app.brokers.base import Balance, OrderRequest, Position
from app.config.runtime_config import get_runtime_settings
from app.config.settings import get_settings
from app.core.feature_flags import load_stability_feature_flags
from app.core.identifiers import BrokerAccountId, StrategyId, TenantId
from app.core.logging_utils import log_event

logger = logging.getLogger(__name__)


# Per-account capital configuration values.
@dataclass
class CapitalConfig:
    """
    Per-account capital settings.

    Values are in absolute currency units (e.g., INR) for now.
    """

    max_notional_per_order: Optional[float] = 5_00_000.0  # 5L per order
    max_gross_exposure: Optional[float] = 10_00_000.0  # 10L per account


# Placeholder for capital state (reserved for future use).
@dataclass
class CapitalState:
    """
    Placeholder for derived state if needed later.
    Currently unused; kept for future extension.
    """

    dummy: int = 0


@dataclass(frozen=True)
class CapitalRequirementEstimate:
    required_capital: Optional[float]
    reason_code: str
    method: str


@dataclass(frozen=True)
class CapitalDecision:
    allowed: bool
    reason: str
    reason_code: str
    mode: str
    required_capital: Optional[float] = None
    available_capital: Optional[float] = None
    order_notional: Optional[float] = None
    gross_exposure_after: Optional[float] = None
    shadow_blocked: bool = False


class CapitalEngine:
    """
    Minimal capital check engine.

    For now:
    - Maintains simple per-account CapitalConfig.
    - Provides can_afford_order(...) which can be called by OrderRouter.

    In this phase we rely on whatever balance/positions view the caller has.
    """

    # Initialize config storage and override caches.
    def __init__(self) -> None:
        # keyed by (tenant_id, broker_account_id)
        self._configs: Dict[Tuple[TenantId, BrokerAccountId], CapitalConfig] = {}
        self._loaded_overrides: set[Tuple[TenantId, BrokerAccountId]] = set()
        self._firestore_cache: Dict[BrokerAccountId, Optional[Dict[str, Any]]] = {}

    # Get or create capital config for an account, applying overrides once.
    def get_config(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
    ) -> CapitalConfig:
        key = (tenant_id, broker_account_id)
        cfg = self._configs.get(key)
        if cfg is None:
            cfg = CapitalConfig()
            self._configs[key] = cfg
        if key not in self._loaded_overrides:
            self._apply_overrides(cfg, tenant_id, broker_account_id)
            self._loaded_overrides.add(key)
        return cfg

    # Apply Firestore and env-based limit overrides.
    def _apply_overrides(
        self,
        cfg: CapitalConfig,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
    ) -> None:
        # Firestore overrides (account-level) first, then env overrides.
        firestore_limits = self._load_firestore_limits(broker_account_id)
        if firestore_limits:
            self._apply_limits_dict(cfg, firestore_limits)

        env_limits = _lookup_env_limits(tenant_id, broker_account_id)
        if env_limits:
            self._apply_limits_dict(cfg, env_limits)

    # Apply a limits dict to the config values.
    def _apply_limits_dict(self, cfg: CapitalConfig, limits: Dict[str, Any]) -> None:
        if "max_notional_per_order" in limits:
            raw = limits.get("max_notional_per_order")
            if raw is None:
                cfg.max_notional_per_order = None
            else:
                val = _coerce_float(raw)
                if val is not None:
                    cfg.max_notional_per_order = val
        if "max_gross_exposure" in limits:
            raw = limits.get("max_gross_exposure")
            if raw is None:
                cfg.max_gross_exposure = None
            else:
                val = _coerce_float(raw)
                if val is not None:
                    cfg.max_gross_exposure = val

    # Load account-level limits from Firestore metadata.
    def _load_firestore_limits(
        self, broker_account_id: BrokerAccountId
    ) -> Optional[Dict[str, Any]]:
        cached = self._firestore_cache.get(broker_account_id)
        if cached is not None:
            return cached
        try:
            from app.tenants.firestore_client import get_broker_account
        except Exception:
            self._firestore_cache[broker_account_id] = None
            return None
        try:
            account = get_broker_account(broker_account_id)
        except Exception:
            logger.debug(
                "CapitalEngine: Firestore lookup failed for broker_account_id=%s",
                broker_account_id,
            )
            self._firestore_cache[broker_account_id] = None
            return None
        if not account or not isinstance(account.meta, dict):
            self._firestore_cache[broker_account_id] = None
            return None
        limits = account.meta.get("capital_limits")
        if not isinstance(limits, dict):
            self._firestore_cache[broker_account_id] = None
            return None
        self._firestore_cache[broker_account_id] = limits
        return limits

    # Suggest an adjusted quantity that respects max_notional_per_order.
    def suggest_order_quantity(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        order: OrderRequest,
        lot_size: Optional[int],
        reference_price: Optional[float] = None,
    ) -> tuple[int, str]:
        cfg = self.get_config(tenant_id, broker_account_id)
        try:
            requested_qty = int(order.quantity or 0)
        except Exception:
            requested_qty = 0
        if requested_qty <= 0:
            return 0, "invalid_requested_quantity"

        try:
            lot = int(lot_size or 0)
        except Exception:
            lot = 0
        if lot <= 0:
            return requested_qty, "lot_size_missing"

        if cfg.max_notional_per_order is None:
            return requested_qty, "max_notional_per_order_unbounded"

        price = 0.0
        try:
            if order.limit_price is not None and float(order.limit_price) > 0:
                price = float(order.limit_price)
        except Exception:
            price = 0.0
        if price <= 0 and reference_price is not None:
            try:
                if float(reference_price) > 0:
                    price = float(reference_price)
            except Exception:
                price = 0.0
        if price <= 0:
            return requested_qty, "price_unavailable_for_sizing"

        max_qty = int(float(cfg.max_notional_per_order) // price)
        if max_qty <= 0:
            return 0, "max_notional_too_low_for_price"

        max_qty = (max_qty // lot) * lot
        if max_qty <= 0:
            return 0, "max_notional_below_one_lot"

        if requested_qty <= max_qty:
            return requested_qty, "within_max_notional_per_order"
        return max_qty, f"resized_for_max_notional_per_order_{requested_qty}_to_{max_qty}"

    @staticmethod
    def _margin_mode() -> str:
        return load_stability_feature_flags().capital_margin_check_mode.upper()

    @staticmethod
    def _normalize_meta_kind(instrument_meta: Optional[Dict[str, Any]]) -> str:
        if not isinstance(instrument_meta, dict):
            return ""
        return str(
            instrument_meta.get("kind")
            or instrument_meta.get("instrument_type")
            or instrument_meta.get("segment")
            or ""
        ).strip().upper()

    def estimate_order_requirement(
        self,
        *,
        order: OrderRequest,
        instrument_meta: Optional[Dict[str, Any]],
        account_state: Optional[Dict[str, Any]],
        config: Any,
    ) -> CapitalRequirementEstimate:
        state = account_state or {}
        price = _coerce_float(
            state.get("reference_price")
            or order.limit_price
            or order.stop_price
        )
        quantity = _coerce_int(order.quantity)
        if quantity <= 0:
            return CapitalRequirementEstimate(
                required_capital=None,
                reason_code="INVALID_QUANTITY",
                method="invalid",
            )
        if price is None or price <= 0:
            return CapitalRequirementEstimate(
                required_capital=None,
                reason_code="MISSING_PRICE",
                method="missing_price",
            )

        meta = instrument_meta if isinstance(instrument_meta, dict) else {}
        meta_kind = self._normalize_meta_kind(meta)
        symbol_upper = str(order.symbol or "").strip().upper()
        side = str(getattr(order.side, "value", order.side) or "").strip().upper()
        lot_size = _coerce_int(
            state.get("lot_size")
            or meta.get("lot_size")
            or 0
        )
        lots = max(1, quantity // lot_size) if lot_size > 0 else 1
        notional = float(price) * float(quantity)
        is_option = meta_kind in {"CE", "PE", "OPTION", "OPTIDX", "OPTSTK"} or symbol_upper.endswith(
            ("CE", "PE")
        )
        is_futures = meta_kind in {"FUT", "FUTURE", "FUTURES"} or symbol_upper.endswith("FUT")

        if is_option and side == "BUY":
            return CapitalRequirementEstimate(
                required_capital=notional,
                reason_code="NOTIONAL",
                method="long_option_buy",
            )

        if is_option and side == "SELL":
            per_lot = max(
                0.0,
                float(getattr(config, "capital_margin_short_option_per_lot", 0.0) or 0.0),
            )
            required = max(notional, float(lots) * per_lot)
            return CapitalRequirementEstimate(
                required_capital=required,
                reason_code="MARGIN_SHORT_OPTION",
                method="short_option_sell",
            )

        if is_futures:
            per_lot = max(
                0.0,
                float(getattr(config, "capital_margin_futures_per_lot", 0.0) or 0.0),
            )
            rate = max(
                0.0,
                float(getattr(config, "capital_margin_futures_rate", 0.0) or 0.0),
            )
            required = max(notional * rate, float(lots) * per_lot, notional)
            if rate > 0.0 or per_lot > 0.0:
                required = max(notional * rate, float(lots) * per_lot)
            return CapitalRequirementEstimate(
                required_capital=required,
                reason_code="MARGIN_FUTURES",
                method="futures",
            )

        return CapitalRequirementEstimate(
            required_capital=notional,
            reason_code="NOTIONAL",
            method="fallback_notional",
        )

    def evaluate_order(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        balance: Optional[Balance],
        positions: Optional[List[Position]],
        order: OrderRequest,
        reference_price: Optional[float] = None,
        instrument_meta: Optional[Dict[str, Any]] = None,
        lot_size: Optional[int] = None,
    ) -> CapitalDecision:
        """
        Check whether an order passes basic capital constraints.

        Phase 1 behaviour:
        - If missing balance/positions, log and allow.
        - Enforce max_notional_per_order if we have a usable price.
        - Gross exposure check is basic and optional.
        """
        cfg = self.get_config(tenant_id, broker_account_id)
        runtime_settings = get_runtime_settings(
            base_settings=get_settings(),
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
        )
        margin_mode = self._margin_mode()

        allowed = True
        reason = "capital_ok"
        reason_code = "CAPITAL_OK"
        price = order.limit_price or 0.0
        price_source = "limit_price"
        if reference_price is not None:
            try:
                ref_price = float(reference_price)
            except (TypeError, ValueError):
                ref_price = 0.0
            if ref_price > 0:
                price = ref_price
                price_source = "reference_price"
        order_notional: Optional[float] = None
        gross_exposure_after: Optional[float] = None
        balance_total: Optional[float] = balance.total if balance is not None else None
        available_capital: Optional[float] = (
            float(balance.available) if balance is not None else None
        )
        required_capital: Optional[float] = None
        shadow_blocked = False

        if order.quantity <= 0:
            allowed = False
            reason = "quantity_must_be_positive"
            reason_code = "INVALID_QUANTITY"

        account_data_missing = balance is None or positions is None

        if allowed and account_data_missing:
            fail_closed = bool(
                getattr(
                    runtime_settings,
                    "capital_fail_closed_on_missing_state",
                    False,
                )
            )
            if fail_closed:
                allowed = False
                reason = "missing_balance_or_positions_fail_closed"
                reason_code = "MISSING_STATE"
                log_event(
                    logger,
                    event_type="CAPITAL_BALANCE_MISSING",
                    message="StateStore missing balance/positions; rejecting order (fail-closed)",
                    level=logging.WARNING,
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    strategy_id=strategy_id,
                )
            else:
                log_event(
                    logger,
                    event_type="CAPITAL_BALANCE_MISSING",
                    message="StateStore missing balance/positions; allowing order for now",
                    level=logging.WARNING,
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    strategy_id=strategy_id,
                )
                reason = "balance_or_positions_missing_allowing_order"
                reason_code = "MISSING_STATE"

        if allowed and price <= 0 and not account_data_missing:
            fail_closed_on_missing_price = bool(
                getattr(
                    runtime_settings,
                    "capital_fail_closed_on_missing_notional_price",
                    False,
                )
            )
            if fail_closed_on_missing_price:
                allowed = False
                reason = "missing_or_non_positive_price_fail_closed"
                reason_code = "MISSING_PRICE"
                log_event(
                    logger,
                    event_type="CAPITAL_NOTIONAL_MISSING",
                    message=(
                        "Non-positive price; rejecting order because "
                        "CAPITAL_FAIL_CLOSED_ON_MISSING_NOTIONAL_PRICE is enabled"
                    ),
                    level=logging.WARNING,
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    strategy_id=strategy_id,
                    symbol=order.symbol,
                    quantity=order.quantity,
                    price_source=price_source,
                )
            else:
                log_event(
                    logger,
                    event_type="CAPITAL_NOTIONAL_MISSING",
                    message="Non-positive price; skipping notional checks and allowing order",
                    level=logging.WARNING,
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    strategy_id=strategy_id,
                    symbol=order.symbol,
                    quantity=order.quantity,
                    price_source=price_source,
                )
                reason = "notional_unknown_allowing_order"
                reason_code = "MISSING_PRICE"

        if allowed and price > 0 and not account_data_missing:
            order_notional = price * order.quantity
            if (
                cfg.max_notional_per_order is not None
                and order_notional > cfg.max_notional_per_order
            ):
                allowed = False
                reason = f"order_notional_exceeds_limit_{order_notional:.2f}_gt_{cfg.max_notional_per_order:.2f}"
                reason_code = "NOTIONAL"

        if (
            allowed
            and not account_data_missing
            and cfg.max_gross_exposure is not None
            and order_notional is not None
            and positions is not None
        ):
            gross = 0.0
            for pos in positions:
                try:
                    gross += abs(pos.quantity * pos.avg_price)
                except Exception:
                    continue
            gross_exposure_after = gross + order_notional
            if gross_exposure_after > cfg.max_gross_exposure:
                allowed = False
                reason = f"gross_exposure_exceeds_limit_{gross_exposure_after:.2f}_gt_{cfg.max_gross_exposure:.2f}"
                reason_code = "GROSS_EXPOSURE"

        if allowed and not account_data_missing and margin_mode in {"SHADOW", "ENFORCE"}:
            estimate = self.estimate_order_requirement(
                order=order,
                instrument_meta=instrument_meta,
                account_state={
                    "balance": balance,
                    "positions": positions,
                    "lot_size": lot_size,
                    "reference_price": reference_price,
                },
                config=runtime_settings,
            )
            required_capital = estimate.required_capital
            if (
                required_capital is not None
                and available_capital is not None
                and available_capital < required_capital
            ):
                if margin_mode == "ENFORCE":
                    allowed = False
                    reason_code = estimate.reason_code
                    reason = (
                        f"{estimate.reason_code}:required={required_capital:.2f}:"
                        f"available={available_capital:.2f}"
                    )
                else:
                    shadow_blocked = True
                    log_event(
                        logger,
                        event_type="CAPITAL_MARGIN_SHADOW",
                        message="Margin requirement breached in SHADOW mode; not blocking order.",
                        level=logging.INFO,
                        tenant_id=tenant_id,
                        broker_account_id=broker_account_id,
                        strategy_id=strategy_id,
                        mode=margin_mode,
                        reason_code=estimate.reason_code,
                        required_capital=float(required_capital),
                        available_capital=float(available_capital),
                        symbol=order.symbol,
                    )

        log_event(
            logger,
            event_type="CAPITAL_CHECK",
            message="capital check decision",
            level=logging.INFO if allowed else logging.WARNING,
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
            extra_info="capital_decision",
            balance=float(balance_total or 0.0),
            order_notional=float(order_notional or 0.0),
            max_notional_per_order=float(cfg.max_notional_per_order or 0.0),
            gross_exposure_after=float(gross_exposure_after or 0.0),
            max_gross_exposure=float(cfg.max_gross_exposure or 0.0),
            allowed=bool(allowed),
            reason=reason,
            reason_code=reason_code,
            price_source=price_source,
            capital_margin_mode=margin_mode,
            required_capital=float(required_capital or 0.0),
            available_capital=float(available_capital or 0.0),
            shadow_blocked=shadow_blocked,
        )
        return CapitalDecision(
            allowed=allowed,
            reason=reason,
            reason_code=reason_code,
            mode=margin_mode,
            required_capital=required_capital,
            available_capital=available_capital,
            order_notional=order_notional,
            gross_exposure_after=gross_exposure_after,
            shadow_blocked=shadow_blocked,
        )

    # Check whether an order passes capital constraints.
    def can_afford_order(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        balance: Optional[Balance],
        positions: Optional[List[Position]],
        order: OrderRequest,
        reference_price: Optional[float] = None,
        instrument_meta: Optional[Dict[str, Any]] = None,
        lot_size: Optional[int] = None,
    ) -> tuple[bool, str]:
        decision = self.evaluate_order(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
            balance=balance,
            positions=positions,
            order=order,
            reference_price=reference_price,
            instrument_meta=instrument_meta,
            lot_size=lot_size,
        )
        return decision.allowed, decision.reason


__all__ = [
    "CapitalEngine",
    "CapitalConfig",
    "CapitalState",
    "CapitalRequirementEstimate",
    "CapitalDecision",
]


# Convert a value to float safely.
def _coerce_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any) -> int:
    try:
        if value is None:
            return 0
        return int(float(value))
    except (TypeError, ValueError):
        return 0


# Load and cache capital limits from environment JSON.
@lru_cache(maxsize=1)
def _load_env_limits() -> Dict[str, Any]:
    raw = os.getenv("CAPITAL_LIMITS_JSON", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception as exc:
        logger.warning("Invalid CAPITAL_LIMITS_JSON: %s", exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("Invalid CAPITAL_LIMITS_JSON: expected JSON object")
        return {}
    return data


# Find applicable env limits for a tenant/account.
def _lookup_env_limits(
    tenant_id: TenantId, broker_account_id: BrokerAccountId
) -> Optional[Dict[str, Any]]:
    data = _load_env_limits()
    if not data:
        return None
    keys = [
        f"{tenant_id}:{broker_account_id}",
        str(broker_account_id),
        str(tenant_id),
        "default",
        "*",
    ]
    for key in keys:
        val = data.get(key)
        if isinstance(val, dict):
            return val
    return None
