"""Fail-closed scaffold for the NIFTY OI/ML CE seller strategy.

The strategy is registered so Phoenix can validate config, routing, and
strict-intraday wiring while the option-chain and ML pipeline is built. It can
stage guarded candidates for inspection, but it must not place orders until leg
construction and router integration are explicitly implemented.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, time, timezone, timedelta
from typing import Any, Dict, Optional

from app.brokers.base import ProductType
from app.config.boot_config import StrategyValueResolver
from app.strategies.base import BaseStrategy
from app.strategies.identifiers import OI_ML_CE_SELLER_ID
from app.strategies.oi_ml.decision import OiMlEntryAction
from app.strategies.oi_ml.order_intents import (
    OiMlOrderIntentConfig,
    build_order_intent_from_candidate,
)

logger = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))


def _parse_time(value: Any, default: time) -> time:
    try:
        hour, minute = (int(part) for part in str(value).split(":", maxsplit=1))
        return time(hour=hour, minute=minute)
    except Exception:
        return default


def _parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _as_ist(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=IST)
    return value.astimezone(IST)


class OiMlCeSellerStrategy(BaseStrategy):
    """Disabled-by-default v1 shell for NIFTY bear-call-spread research."""

    def __init__(
        self,
        instrument_meta: Dict[str, Dict[str, Any]],
        order_client: Any = None,
        risk_manager: Any = None,
        *,
        env_prefix: str = "NIFTY_OI_ML_",
        underlying_label: str = "NIFTY_IDX",
        params: Optional[Dict[str, Any]] = None,
        config_resolver: Optional[StrategyValueResolver] = None,
        decision_engine: Any = None,
        order_intent_builder: Any = None,
        shadow_lifecycle_store: Any = None,
    ) -> None:
        self.instrument_meta = instrument_meta
        self.order_client = order_client
        self.risk_manager = risk_manager
        self.env_prefix = env_prefix
        self.underlying_label = underlying_label
        self.params = dict(params or {})
        self._cfg = config_resolver or StrategyValueResolver(
            env_prefix=self.env_prefix,
            params=self.params,
            env=dict(os.environ),
        )
        self._strategy_id = OI_ML_CE_SELLER_ID
        self.decision_engine = decision_engine
        self.order_intent_builder = order_intent_builder or build_order_intent_from_candidate
        self.shadow_lifecycle_store = shadow_lifecycle_store

        self.trade_mode = self._cfg.get_str("TRADE_MODE", "PAPER").upper()
        self.product_type = ProductType(
            str(self.params.get("product_type") or "INTRADAY").upper()
        )
        self.account_equity = float(self.params.get("account_equity", 400000))
        self.allow_naked = _parse_bool(self.params.get("allow_naked"), default=False)
        self.entry_start_time = _parse_time(
            self.params.get("entry_start_time", "09:50"),
            time(9, 50),
        )
        self.entry_end_time = _parse_time(
            self.params.get("entry_end_time", "14:15"),
            time(14, 15),
        )
        self.time_stop = _parse_time(self.params.get("time_stop", "14:55"), time(14, 55))
        self.max_open_spreads = int(self.params.get("max_open_spreads", 1))
        self.max_spread_loss_rupees = float(
            self.params.get("max_spread_loss_rupees", 5000)
        )
        self.lots = int(self.params.get("lots", 1))
        self.lot_size = int(self.params.get("lot_size", 65))
        self.spread_width_points = int(self.params.get("spread_width_points", 200))
        self.underlying = str(self.params.get("underlying", "NIFTY")).strip().upper()
        self.expiry = _parse_date(self.params.get("expiry"))
        self.tenant_id = self.params.get("tenant_id")
        self.account_id = self.params.get("account_id")
        self.order_intent_config = OiMlOrderIntentConfig(
            strategy_id=self._strategy_id,
            lots=self.lots,
            lot_size=self.lot_size,
            spread_width_points=self.spread_width_points,
            product_type=self.product_type,
            max_spread_loss_rupees=self.max_spread_loss_rupees,
        )

        self.last_price: Dict[str, float] = {}
        self.last_bar_ts_by_label: Dict[str, datetime] = {}
        self.open_spreads: Dict[str, Any] = {}
        self.no_trade_counts: Dict[str, int] = {}
        self.last_decision: Any = None
        self.last_intent_result: Any = None
        self.staged_entries: list[Any] = []
        self.staged_order_intents: list[Any] = []
        self.shadow_lifecycle_records: list[Any] = []

    def _record_no_trade(self, reason: str) -> None:
        self.no_trade_counts[reason] = int(self.no_trade_counts.get(reason, 0)) + 1
        logger.debug("[%s] no_trade reason=%s", self._strategy_id, reason)

    def on_tick(self, label: str, ltp: float) -> None:
        try:
            self.last_price[str(label)] = float(ltp)
        except (TypeError, ValueError):
            self._record_no_trade("invalid_tick_price")

    def on_bar(
        self,
        label: str,
        timeframe_seconds: int,
        candle: Any,
        indicators: Dict[str, Any],
    ) -> None:
        start_ts = getattr(candle, "start_ts", None)
        if isinstance(start_ts, datetime):
            bar_ts = _as_ist(start_ts)
            self.last_bar_ts_by_label[str(label)] = bar_ts
        else:
            bar_ts = datetime.now(IST)
        if str(label) != self.underlying_label:
            return
        if self.decision_engine is None:
            self._record_no_trade("strategy_scaffold_fail_closed")
            return
        if self.expiry is None:
            self._record_no_trade("missing_expiry")
            return

        try:
            decision = self.decision_engine.evaluate_entry(
                underlying=self.underlying,
                expiry=self.expiry,
                decision_ts=bar_ts,
                tenant_id=self.tenant_id,
                account_id=self.account_id,
            )
        except Exception as exc:
            logger.exception("[%s] decision engine failed", self._strategy_id)
            self._record_no_trade(f"decision_engine_exception:{type(exc).__name__}")
            return

        self.last_decision = decision
        if decision.action is OiMlEntryAction.STAGE_ENTRY and decision.selected is not None:
            try:
                intent_result = self.order_intent_builder(
                    decision.selected,
                    created_at=bar_ts,
                    config=self.order_intent_config,
                )
            except Exception as exc:
                logger.exception("[%s] order-intent build failed", self._strategy_id)
                self._record_no_trade(f"order_intent_exception:{type(exc).__name__}")
                return
            self.last_intent_result = intent_result
            if getattr(intent_result, "ok", False) and getattr(intent_result, "intent", None) is not None:
                if self.shadow_lifecycle_store is not None:
                    try:
                        record = self.shadow_lifecycle_store.record_intent(
                            intent_result.intent,
                            decision_reason=str(getattr(decision, "reason", "")) or None,
                            tenant_id=self.tenant_id,
                            broker_account_id=self.account_id,
                        )
                    except Exception as exc:
                        logger.exception("[%s] shadow lifecycle record failed", self._strategy_id)
                        self._record_no_trade(f"shadow_lifecycle_exception:{type(exc).__name__}")
                        return
                    self.shadow_lifecycle_records.append(record)
                self.staged_entries.append(decision.selected)
                self.staged_order_intents.append(intent_result.intent)
                self._record_no_trade("order_intent_staged_no_order_routing")
                return
            reasons = tuple(getattr(intent_result, "reasons", ()) or ())
            reason = reasons[0] if reasons else "unknown"
            self._record_no_trade(f"order_intent_rejected:{reason}")
            return
        self._record_no_trade(str(getattr(decision, "reason", "no_trade")))

    def force_exit_all(
        self,
        reason: str = "FORCED_EXIT",
        *,
        submit_orders: bool = True,
    ) -> None:
        self.open_spreads.clear()


__all__ = ["OiMlCeSellerStrategy"]
