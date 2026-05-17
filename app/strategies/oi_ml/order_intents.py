"""Dry-run order-intent builder for the OI/ML CE seller.

The objects in this module are inert. They describe the entry legs Phoenix
would need to construct later, but they are not ``OrderRequest`` instances and
they are never submitted to the strategy bridge from this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence

from app.brokers.base import (
    OrderPurpose,
    OrderSide,
    OrderType,
    ProductType,
    TimeInForce,
)
from app.data.option_chain_provider import OptionQuote
from app.risk.option_sell_guard import OptionSellStructure
from app.strategies.identifiers import OI_ML_CE_SELLER_ID
from app.strategies.oi_ml.decision import OiMlCandidatePlan


@dataclass(frozen=True)
class OiMlOrderIntentConfig:
    """Sizing and order defaults for inert intent construction."""

    strategy_id: str = OI_ML_CE_SELLER_ID
    lots: int = 1
    lot_size: int = 65
    spread_width_points: int = 200
    product_type: ProductType = ProductType.INTRADAY
    order_type: OrderType = OrderType.LIMIT
    time_in_force: TimeInForce = TimeInForce.DAY
    min_net_credit_points: float = 0.05
    max_spread_loss_rupees: float | None = 5000.0
    tag_prefix: str = "oi_ml_ce_seller"


@dataclass(frozen=True)
class OiMlOrderIntentLeg:
    """One inert entry leg."""

    role: str
    side: OrderSide
    symbol: str
    exchange: str
    symbol_token: str
    expiry: date
    strike: int
    option_type: str
    quantity: int
    price_hint: float
    order_type: OrderType
    product_type: ProductType
    time_in_force: TimeInForce
    purpose: OrderPurpose = OrderPurpose.ENTRY
    source_snapshot_ts: datetime | None = None


@dataclass(frozen=True)
class OiMlOrderIntent:
    """Dry-run entry plan for audit, paper shadowing, and later routing work."""

    intent_id: str
    strategy_id: str
    structure: OptionSellStructure
    underlying: str
    expiry: date
    short_strike: int
    quantity: int
    created_at: datetime
    legs: tuple[OiMlOrderIntentLeg, ...]
    estimated_net_credit_points: float
    estimated_max_loss_rupees: float
    dry_run_only: bool = True
    guard_reasons: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OiMlOrderIntentBuildResult:
    """Result of converting a guarded candidate into inert order intent."""

    intent: OiMlOrderIntent | None
    reasons: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.intent is not None


def build_order_intent_from_candidate(
    plan: OiMlCandidatePlan,
    *,
    created_at: datetime,
    config: OiMlOrderIntentConfig | None = None,
) -> OiMlOrderIntentBuildResult:
    """Build a dry-run intent from a selected guarded candidate."""

    cfg = config or OiMlOrderIntentConfig()
    reasons: list[str] = []
    if not plan.guard_result.allowed:
        reasons.append("guard_result_not_allowed")
    quantity = int(cfg.lots) * int(cfg.lot_size)
    if quantity <= 0:
        reasons.append("invalid_quantity")

    short_quote = plan.quote.normalized()
    if short_quote.option_type != "CE":
        reasons.append("unsupported_option_type")
    if not short_quote.symbol_token:
        reasons.append("missing_short_leg_symbol_token")

    if reasons:
        return OiMlOrderIntentBuildResult(intent=None, reasons=tuple(reasons))

    if plan.structure is OptionSellStructure.BEAR_CALL_SPREAD:
        return _build_bear_call_spread_intent(
            plan,
            short_quote=short_quote,
            quantity=quantity,
            created_at=_aware_utc(created_at),
            config=cfg,
        )
    if plan.structure is OptionSellStructure.NAKED_SHORT_CE:
        return _build_naked_short_ce_intent(
            plan,
            short_quote=short_quote,
            quantity=quantity,
            created_at=_aware_utc(created_at),
            config=cfg,
        )
    return OiMlOrderIntentBuildResult(
        intent=None,
        reasons=("unsupported_structure",),
    )


def _build_bear_call_spread_intent(
    plan: OiMlCandidatePlan,
    *,
    short_quote: OptionQuote,
    quantity: int,
    created_at: datetime,
    config: OiMlOrderIntentConfig,
) -> OiMlOrderIntentBuildResult:
    long_strike = int(short_quote.strike) + int(config.spread_width_points)
    long_quote = _find_quote(
        plan.snapshot,
        underlying=short_quote.underlying,
        expiry=short_quote.expiry,
        strike=long_strike,
        option_type="CE",
        provider=short_quote.provider,
    )
    if long_quote is None:
        return OiMlOrderIntentBuildResult(
            intent=None,
            reasons=("missing_long_leg_quote",),
            metadata={"long_strike": long_strike},
        )
    if not long_quote.symbol_token:
        return OiMlOrderIntentBuildResult(
            intent=None,
            reasons=("missing_long_leg_symbol_token",),
            metadata={"long_strike": long_strike},
        )

    short_price = _sell_price(short_quote)
    long_price = _buy_price(long_quote)
    if short_price is None or long_price is None:
        return OiMlOrderIntentBuildResult(
            intent=None,
            reasons=("invalid_leg_price",),
        )

    width = float(long_quote.strike - short_quote.strike)
    net_credit = short_price - long_price
    max_loss = max(0.0, (width - net_credit) * quantity)
    if net_credit < float(config.min_net_credit_points):
        return OiMlOrderIntentBuildResult(
            intent=None,
            reasons=("net_credit_below_min",),
            metadata={"estimated_net_credit_points": net_credit},
        )
    if (
        config.max_spread_loss_rupees is not None
        and max_loss > float(config.max_spread_loss_rupees)
    ):
        return OiMlOrderIntentBuildResult(
            intent=None,
            reasons=("estimated_spread_loss_above_limit",),
            metadata={"estimated_max_loss_rupees": max_loss},
        )

    legs = (
        _leg(
            role="CE_SHORT",
            side=OrderSide.SELL,
            quote=short_quote,
            quantity=quantity,
            price_hint=short_price,
            config=config,
        ),
        _leg(
            role="CE_LONG",
            side=OrderSide.BUY,
            quote=long_quote,
            quantity=quantity,
            price_hint=long_price,
            config=config,
        ),
    )
    return OiMlOrderIntentBuildResult(
        intent=_intent(
            plan=plan,
            short_quote=short_quote,
            created_at=created_at,
            quantity=quantity,
            legs=legs,
            net_credit=net_credit,
            max_loss=max_loss,
            config=config,
        )
    )


def _build_naked_short_ce_intent(
    plan: OiMlCandidatePlan,
    *,
    short_quote: OptionQuote,
    quantity: int,
    created_at: datetime,
    config: OiMlOrderIntentConfig,
) -> OiMlOrderIntentBuildResult:
    short_price = _sell_price(short_quote)
    if short_price is None:
        return OiMlOrderIntentBuildResult(
            intent=None,
            reasons=("invalid_leg_price",),
        )
    legs = (
        _leg(
            role="CE_SHORT",
            side=OrderSide.SELL,
            quote=short_quote,
            quantity=quantity,
            price_hint=short_price,
            config=config,
        ),
    )
    return OiMlOrderIntentBuildResult(
        intent=_intent(
            plan=plan,
            short_quote=short_quote,
            created_at=created_at,
            quantity=quantity,
            legs=legs,
            net_credit=short_price,
            max_loss=float(plan.max_loss_rupees),
            config=config,
            metadata={"risk_note": "naked_loss_uses_guard_cap_not_theoretical"},
        )
    )


def _intent(
    *,
    plan: OiMlCandidatePlan,
    short_quote: OptionQuote,
    created_at: datetime,
    quantity: int,
    legs: tuple[OiMlOrderIntentLeg, ...],
    net_credit: float,
    max_loss: float,
    config: OiMlOrderIntentConfig,
    metadata: Mapping[str, Any] | None = None,
) -> OiMlOrderIntent:
    intent_id = (
        f"{config.tag_prefix}:{plan.structure.value}:"
        f"{short_quote.expiry.isoformat()}:{short_quote.strike}:"
        f"{created_at.isoformat()}"
    )
    return OiMlOrderIntent(
        intent_id=intent_id,
        strategy_id=config.strategy_id,
        structure=plan.structure,
        underlying=short_quote.underlying,
        expiry=short_quote.expiry,
        short_strike=short_quote.strike,
        quantity=quantity,
        created_at=created_at,
        legs=legs,
        estimated_net_credit_points=float(net_credit),
        estimated_max_loss_rupees=float(max_loss),
        dry_run_only=True,
        guard_reasons=tuple(plan.guard_result.reasons),
        metadata=dict(metadata or {}),
    )


def _leg(
    *,
    role: str,
    side: OrderSide,
    quote: OptionQuote,
    quantity: int,
    price_hint: float,
    config: OiMlOrderIntentConfig,
) -> OiMlOrderIntentLeg:
    return OiMlOrderIntentLeg(
        role=role,
        side=side,
        symbol=quote.trading_symbol,
        exchange=quote.exchange,
        symbol_token=str(quote.symbol_token or ""),
        expiry=quote.expiry,
        strike=quote.strike,
        option_type=quote.option_type,
        quantity=quantity,
        price_hint=float(price_hint),
        order_type=config.order_type,
        product_type=config.product_type,
        time_in_force=config.time_in_force,
        source_snapshot_ts=quote.snapshot_ts,
    )


def _find_quote(
    quotes: Sequence[OptionQuote],
    *,
    underlying: str,
    expiry: date,
    strike: int,
    option_type: str,
    provider: str,
) -> OptionQuote | None:
    target_underlying = str(underlying).strip().upper()
    target_provider = str(provider).strip().lower()
    target_type = str(option_type).strip().upper()
    for quote in quotes:
        row = quote.normalized()
        if (
            row.underlying == target_underlying
            and row.expiry == expiry
            and int(row.strike) == int(strike)
            and row.option_type == target_type
            and row.provider == target_provider
        ):
            return row
    return None


def _sell_price(quote: OptionQuote) -> float | None:
    return _first_positive(quote.bid, _mid(quote), quote.ltp)


def _buy_price(quote: OptionQuote) -> float | None:
    return _first_positive(quote.ask, _mid(quote), quote.ltp)


def _mid(quote: OptionQuote) -> float | None:
    bid = _float(quote.bid)
    ask = _float(quote.ask)
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return None
    return (bid + ask) / 2.0


def _first_positive(*values: Any) -> float | None:
    for value in values:
        parsed = _float(value)
        if parsed is not None and parsed > 0:
            return parsed
    return None


def _float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "OiMlOrderIntent",
    "OiMlOrderIntentBuildResult",
    "OiMlOrderIntentConfig",
    "OiMlOrderIntentLeg",
    "build_order_intent_from_candidate",
]
