"""NIFTY OI/ML CE seller strategy.

The strategy remains disabled by default in ``strategy_env.yaml``. When enabled
with order routing, v1 only submits bear-call spreads: buy the hedge first,
then sell the short call through the normal Phoenix strategy bridge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import os
from datetime import date, datetime, time, timezone, timedelta
from typing import Any, Dict, Optional

from app.brokers.base import (
    OrderPurpose,
    OrderRequest,
    OrderResponse,
    OrderSide,
    OrderType,
    ProductType,
)
from app.config.boot_config import StrategyValueResolver
from app.orders.strategy_bridge import place_order_via_bridge
from app.risk.option_sell_guard import OptionSellStructure
from app.strategies.base import BaseStrategy
from app.strategies.identifiers import OI_ML_CE_SELLER_ID
from app.strategies.oi_ml.decision import OiMlEntryAction
from app.strategies.oi_ml.greek_risk import (
    OiMlGreekRiskConfig,
    abs_delta_value,
    greek_metadata_from_quote,
)
from app.strategies.oi_ml.order_intents import (
    OiMlOrderIntent,
    OiMlOrderIntentLeg,
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


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_ist(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=IST)
    return value.astimezone(IST)


@dataclass
class OiMlOpenSpread:
    spread_id: str
    intent: OiMlOrderIntent
    short_leg: OiMlOrderIntentLeg
    long_leg: OiMlOrderIntentLeg
    quantity_lots: int
    remaining_lots: int
    entry_credit: float
    entry_time: datetime
    status: str = "OPEN"
    exit_attempts: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


class OiMlCeSellerStrategy(BaseStrategy):
    """Disabled-by-default v1 NIFTY bear-call-spread strategy."""

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
        self.order_routing_enabled = _parse_bool(
            self.params.get("order_routing_enabled"),
            default=False,
        )
        self.take_profit_pct_of_credit = float(
            self.params.get("take_profit_pct_of_credit", 0.60)
        )
        self.stop_loss_mult_credit = float(
            self.params.get("stop_loss_mult_credit", 1.80)
        )
        self.spot_stop_buffer_points = float(
            self.params.get("spot_stop_buffer_points", 0.0)
        )
        self.max_vix = float(self.params.get("max_vix", 22.0))
        self.max_quote_age_seconds = int(self.params.get("max_quote_age_seconds", 120))
        self.lots = int(self.params.get("lots", 1))
        self.lot_size = int(self.params.get("lot_size", 65))
        self.spread_width_points = int(self.params.get("spread_width_points", 200))
        self.underlying = str(self.params.get("underlying", "NIFTY")).strip().upper()
        self.expiry = _parse_date(self.params.get("expiry"))
        self.tenant_id = self.params.get("tenant_id")
        self.account_id = self.params.get("account_id")
        self.greek_risk_config = self._load_greek_risk_config()
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
        self.open_spreads: Dict[str, OiMlOpenSpread] = {}
        self.no_trade_counts: Dict[str, int] = {}
        self.last_decision: Any = None
        self.last_intent_result: Any = None
        self.last_execution_result: Any = None
        self.staged_entries: list[Any] = []
        self.staged_order_intents: list[Any] = []
        self.shadow_lifecycle_records: list[Any] = []
        self._spread_seq = 0

    def _record_no_trade(self, reason: str) -> None:
        self.no_trade_counts[reason] = int(self.no_trade_counts.get(reason, 0)) + 1
        logger.debug("[%s] no_trade reason=%s", self._strategy_id, reason)

    def on_tick(self, label: str, ltp: float) -> None:
        try:
            price = float(ltp)
            self.last_price[str(label)] = price
            meta = self.instrument_meta.get(str(label), {})
            symbol = meta.get("symbol") or meta.get("tradingsymbol") or meta.get("trading_symbol")
            if symbol:
                self.last_price[str(symbol)] = price
            if str(label) == self.underlying_label:
                self.last_price[self.underlying] = price
        except (TypeError, ValueError):
            self._record_no_trade("invalid_tick_price")
            return
        self._maybe_manage_exits(datetime.now(IST))

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
        try:
            close_price = float(getattr(candle, "c", self.last_price.get(str(label), 0.0)))
            if close_price > 0:
                self.last_price[str(label)] = close_price
                self.last_price[self.underlying] = close_price
        except (TypeError, ValueError):
            pass
        self._refresh_open_spread_greeks(bar_ts)
        self._maybe_manage_exits(bar_ts)
        if self.decision_engine is None:
            self._record_no_trade("strategy_scaffold_fail_closed")
            return
        if self.expiry is None:
            self._record_no_trade("missing_expiry")
            return
        if not self._inside_entry_window(bar_ts):
            self._record_no_trade("outside_entry_window")
            return
        if len(self.open_spreads) >= self.max_open_spreads:
            self._record_no_trade("max_open_spreads_reached")
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
                if not self.order_routing_enabled:
                    self.staged_entries.append(decision.selected)
                    self.staged_order_intents.append(intent_result.intent)
                    self._record_no_trade("order_intent_staged_no_order_routing")
                    return
                execution = self._enter_spread(intent_result.intent, decision.selected)
                self.last_execution_result = execution
                if execution.get("ok"):
                    self._record_no_trade("spread_entry_submitted")
                    return
                self._record_no_trade(f"spread_entry_rejected:{execution.get('reason', 'unknown')}")
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
        if not submit_orders or not self.order_routing_enabled:
            self.open_spreads.clear()
            return
        for spread_id in list(self.open_spreads):
            self._exit_spread(spread_id, reason=reason)

    def _inside_entry_window(self, value: datetime) -> bool:
        now = _as_ist(value).time()
        return self.entry_start_time <= now <= self.entry_end_time and now < self.time_stop

    def _enter_spread(self, intent: OiMlOrderIntent, selected: Any) -> dict[str, Any]:
        if self.allow_naked or intent.structure is not OptionSellStructure.BEAR_CALL_SPREAD:
            return {"ok": False, "reason": "v1_requires_bear_call_spread"}
        legs = {leg.role: leg for leg in intent.legs}
        long_leg = legs.get("CE_LONG")
        short_leg = legs.get("CE_SHORT")
        if long_leg is None or short_leg is None:
            return {"ok": False, "reason": "missing_spread_legs"}

        lots = self._lots_for_leg(long_leg)
        long_resp = self._submit_leg(
            long_leg,
            side=OrderSide.BUY,
            purpose=OrderPurpose.ENTRY,
            lots=lots,
            tag="OI_ML_ENTRY_HEDGE",
            idempotency_key=f"{intent.intent_id}:entry:hedge",
            selected=selected,
            intent=intent,
        )
        if not self._accepted(long_resp):
            return {"ok": False, "reason": "hedge_leg_rejected", "response": long_resp}

        filled_long = self._filled_lots(long_resp, requested_lots=lots)
        if filled_long <= 0:
            return {"ok": False, "reason": "hedge_leg_zero_fill", "response": long_resp}

        short_resp = self._submit_leg(
            short_leg,
            side=OrderSide.SELL,
            purpose=OrderPurpose.ENTRY,
            lots=filled_long,
            tag="OI_ML_ENTRY_SHORT",
            idempotency_key=f"{intent.intent_id}:entry:short",
            selected=selected,
            intent=intent,
        )
        if not self._accepted(short_resp):
            rollback = self._submit_leg(
                long_leg,
                side=OrderSide.SELL,
                purpose=OrderPurpose.EXIT,
                lots=filled_long,
                tag="OI_ML_ROLLBACK_HEDGE",
                idempotency_key=f"{intent.intent_id}:rollback:hedge",
                selected=selected,
                intent=intent,
                exit_reason="SHORT_LEG_FAILED_ROLLBACK",
            )
            return {
                "ok": False,
                "reason": "short_leg_rejected_hedge_rollback_submitted",
                "response": short_resp,
                "rollback": rollback,
            }

        filled_short = self._filled_lots(short_resp, requested_lots=filled_long)
        if filled_short < filled_long:
            residual_hedge = filled_long - filled_short
            if residual_hedge > 0:
                self._submit_leg(
                    long_leg,
                    side=OrderSide.SELL,
                    purpose=OrderPurpose.EXIT,
                    lots=residual_hedge,
                    tag="OI_ML_RESIDUAL_HEDGE_CLOSE",
                    idempotency_key=f"{intent.intent_id}:rollback:hedge_residual",
                    selected=selected,
                    intent=intent,
                    exit_reason="SHORT_PARTIAL_HEDGE_RESIDUAL",
                )
        if filled_short <= 0:
            return {"ok": False, "reason": "short_leg_zero_fill", "response": short_resp}

        self._spread_seq += 1
        spread_id = f"oi_ml_spread_{self._spread_seq}"
        spread = OiMlOpenSpread(
            spread_id=spread_id,
            intent=intent,
            short_leg=short_leg,
            long_leg=long_leg,
            quantity_lots=filled_short,
            remaining_lots=filled_short,
            entry_credit=float(intent.estimated_net_credit_points),
            entry_time=datetime.now(IST),
            metadata={
                "selected_strike": getattr(getattr(selected, "quote", None), "strike", None),
                "max_loss_rupees": float(intent.estimated_max_loss_rupees),
                "greek_risk": dict(getattr(selected, "metadata", {}) or {}).get("greek_risk"),
                "entry_greeks": greek_metadata_from_quote(getattr(selected, "quote", None)),
                "current_greeks": greek_metadata_from_quote(getattr(selected, "quote", None)),
            },
        )
        self.open_spreads[spread_id] = spread
        return {"ok": True, "spread_id": spread_id, "lots": filled_short}

    def _maybe_manage_exits(self, now: datetime) -> None:
        if not self.open_spreads:
            return
        for spread_id, spread in list(self.open_spreads.items()):
            reason = self._exit_reason(spread, now)
            if reason:
                if self.order_routing_enabled:
                    self._exit_spread(spread_id, reason=reason)
                else:
                    self.open_spreads.pop(spread_id, None)

    def _exit_reason(self, spread: OiMlOpenSpread, now: datetime) -> str | None:
        now_time = _as_ist(now).time()
        if now_time >= time(15, 20):
            return "EOD"
        if now_time >= self.time_stop:
            return "TIME_STOP"
        if spread.metadata.get("oi_invalidated"):
            return "OI_INVALIDATION"
        spot = self.last_price.get(self.underlying_label) or self.last_price.get(self.underlying)
        if spot is not None and float(spot) >= (
            float(spread.short_leg.strike) - float(self.spot_stop_buffer_points)
        ):
            return "SPOT_STOP"
        vix = spread.metadata.get("vix")
        if vix is not None and float(vix) > float(self.max_vix):
            return "VOL_STOP"
        greek_exit = self._greek_exit_reason(spread)
        if greek_exit:
            return greek_exit
        close_debit = self._spread_close_debit(spread)
        if close_debit is None:
            return None
        if close_debit <= spread.entry_credit * (1.0 - self.take_profit_pct_of_credit):
            return "TAKE_PROFIT"
        stop_mult, greek_tightened = self._effective_stop_loss_mult_credit(spread)
        if close_debit >= spread.entry_credit * stop_mult:
            return "GREEK_TIGHT_LOSS_STOP" if greek_tightened else "LOSS_STOP"
        return None

    def _exit_spread(self, spread_id: str, *, reason: str) -> None:
        spread = self.open_spreads.get(spread_id)
        if spread is None or spread.remaining_lots <= 0:
            self.open_spreads.pop(spread_id, None)
            return
        lots = int(spread.remaining_lots)
        spread.exit_attempts += 1
        short_resp = self._submit_leg(
            spread.short_leg,
            side=OrderSide.BUY,
            purpose=OrderPurpose.EXIT,
            lots=lots,
            tag=f"OI_ML_EXIT_{reason}",
            idempotency_key=f"{spread.intent.intent_id}:exit:short:{spread.exit_attempts}",
            selected=None,
            intent=spread.intent,
            exit_reason=reason,
        )
        if not self._accepted(short_resp):
            return
        closed_lots = self._filled_lots(short_resp, requested_lots=lots)
        if closed_lots <= 0:
            return
        self._submit_leg(
            spread.long_leg,
            side=OrderSide.SELL,
            purpose=OrderPurpose.EXIT,
            lots=closed_lots,
            tag=f"OI_ML_EXIT_HEDGE_{reason}",
            idempotency_key=f"{spread.intent.intent_id}:exit:hedge:{spread.exit_attempts}",
            selected=None,
            intent=spread.intent,
            exit_reason=reason,
        )
        spread.remaining_lots = max(0, int(spread.remaining_lots) - int(closed_lots))
        if spread.remaining_lots <= 0:
            spread.status = "CLOSED"
            self.open_spreads.pop(spread_id, None)

    def mark_spread_oi_invalidated(self, spread_id: str) -> None:
        spread = self.open_spreads.get(spread_id)
        if spread is not None:
            spread.metadata["oi_invalidated"] = True

    def mark_spread_vix(self, spread_id: str, value: float) -> None:
        spread = self.open_spreads.get(spread_id)
        if spread is not None:
            spread.metadata["vix"] = float(value)

    def mark_spread_greeks(
        self,
        spread_id: str,
        *,
        delta: float | None = None,
        gamma: float | None = None,
        theta: float | None = None,
        vega: float | None = None,
        iv: float | None = None,
    ) -> None:
        spread = self.open_spreads.get(spread_id)
        if spread is None:
            return
        current = dict(spread.metadata.get("current_greeks") or {})
        for key, value in {
            "delta": delta,
            "gamma": gamma,
            "theta": theta,
            "vega": vega,
            "iv": iv,
        }.items():
            if value is not None:
                current[key] = float(value)
        if "delta" in current:
            current["abs_delta"] = abs_delta_value(current["delta"])
        if "gamma" in current:
            current["abs_gamma"] = abs(float(current["gamma"]))
        if "vega" in current:
            current["abs_vega"] = abs(float(current["vega"]))
        spread.metadata["current_greeks"] = current

    def _submit_leg(
        self,
        leg: OiMlOrderIntentLeg,
        *,
        side: OrderSide,
        purpose: OrderPurpose,
        lots: int,
        tag: str,
        idempotency_key: str,
        selected: Any,
        intent: OiMlOrderIntent,
        exit_reason: str | None = None,
    ) -> OrderResponse:
        order_req = OrderRequest(
            symbol=leg.symbol,
            quantity=max(1, int(lots)),
            side=side,
            order_type=leg.order_type if purpose is OrderPurpose.ENTRY else OrderType.MARKET,
            product_type=leg.product_type,
            time_in_force=leg.time_in_force,
            limit_price=leg.price_hint if leg.order_type is OrderType.LIMIT and purpose is OrderPurpose.ENTRY else None,
            stop_price=None,
            tag=tag,
            purpose=purpose,
            exchange=leg.exchange,
            symbol_token=leg.symbol_token,
            idempotency_key=idempotency_key,
            position_label=f"{self._strategy_id}:{leg.role}:{leg.strike}",
            strategy_context=self._strategy_context(
                selected=selected,
                intent=intent,
                leg=leg,
                exit_reason=exit_reason,
            ),
            exit_reason=exit_reason,
            strategy_id=self._strategy_id,
            account_id=str(self.account_id) if self.account_id is not None else None,
        )
        return place_order_via_bridge(
            strategy_id=self._strategy_id,
            order_req=order_req,
            tenant_id=self.tenant_id,
            broker_account_id=self.account_id,
        )

    def _strategy_context(
        self,
        *,
        selected: Any,
        intent: OiMlOrderIntent,
        leg: OiMlOrderIntentLeg,
        exit_reason: str | None,
    ) -> Dict[str, Any]:
        quote = getattr(selected, "quote", None)
        score = getattr(selected, "score", None)
        guard = getattr(selected, "guard_result", None)
        return {
            "strategy_id": self._strategy_id,
            "structure": intent.structure.value,
            "allow_naked": False,
            "quote": self._quote_context(quote) if quote is not None else None,
            "ml_score": getattr(score, "probability", None),
            "predicted_mae_premium": getattr(score, "predicted_mae_premium", None),
            "premium_received": float(intent.estimated_net_credit_points),
            "max_loss_rupees": float(intent.estimated_max_loss_rupees),
            "vix": getattr(quote, "vix", None) if quote is not None else None,
            "leg_role": leg.role,
            "leg_symbol": leg.symbol,
            "source_snapshot_ts": leg.source_snapshot_ts.isoformat()
            if leg.source_snapshot_ts is not None
            else None,
            "exit_reason": exit_reason,
            "guard_reasons": list(getattr(guard, "reasons", ()) or ()),
            "data_fresh": True,
            "data_age_seconds": 0,
            "current_open_risk_rupees": sum(
                float(open_spread.intent.estimated_max_loss_rupees)
                for open_spread in self.open_spreads.values()
            ),
            "max_open_risk_rupees": float(self.max_spread_loss_rupees)
            * max(1, int(self.max_open_spreads)),
        }

    @staticmethod
    def _quote_context(quote: Any) -> Dict[str, Any]:
        row = quote.normalized()
        return {
            "snapshot_ts": row.snapshot_ts.isoformat(),
            "source_ts": row.source_ts.isoformat() if row.source_ts else None,
            "ingested_at": row.ingested_at.isoformat() if row.ingested_at else None,
            "underlying": row.underlying,
            "expiry": row.expiry.isoformat(),
            "strike": row.strike,
            "option_type": row.option_type,
            "trading_symbol": row.trading_symbol,
            "exchange": row.exchange,
            "provider": row.provider,
            "symbol_token": row.symbol_token,
            "oi": row.oi,
            "volume": row.volume,
            "iv": float(row.iv) if row.iv is not None else None,
            "delta": float(row.delta) if row.delta is not None else None,
            "gamma": float(row.gamma) if row.gamma is not None else None,
            "theta": float(row.theta) if row.theta is not None else None,
            "vega": float(row.vega) if row.vega is not None else None,
            "bid": float(row.bid) if row.bid is not None else None,
            "ask": float(row.ask) if row.ask is not None else None,
            "ltp": float(row.ltp) if row.ltp is not None else None,
            "underlying_ltp": float(row.underlying_ltp)
            if row.underlying_ltp is not None
            else None,
            "vix": float(row.vix) if row.vix is not None else None,
            "raw_hash": row.raw_hash,
            "quality_flags": dict(row.quality_flags or {}),
        }

    @staticmethod
    def _accepted(response: OrderResponse) -> bool:
        status = str(getattr(response, "status", "") or "").upper()
        return status not in {"REJECTED", "FAILED", "ERROR", "CANCELLED", "EXPIRED"}

    @staticmethod
    def _filled_lots(response: OrderResponse, *, requested_lots: int) -> int:
        filled = int(getattr(response, "filled_quantity", 0) or 0)
        if filled <= 0 and OiMlCeSellerStrategy._accepted(response):
            return max(1, int(requested_lots))
        return max(0, min(int(requested_lots), filled))

    def _lots_for_leg(self, leg: OiMlOrderIntentLeg) -> int:
        quantity = max(1, int(leg.quantity))
        return max(1, int(round(quantity / max(1, int(self.lot_size)))))

    def _spread_close_debit(self, spread: OiMlOpenSpread) -> float | None:
        short_price = self._price_for_leg(spread.short_leg)
        long_price = self._price_for_leg(spread.long_leg)
        if short_price is None or long_price is None:
            return None
        return max(0.0, float(short_price) - float(long_price))

    def _price_for_leg(self, leg: OiMlOrderIntentLeg) -> float | None:
        for key in (leg.symbol, str(leg.strike), f"{self._strategy_id}:{leg.role}:{leg.strike}"):
            value = self.last_price.get(str(key))
            if value is not None:
                return float(value)
        return float(leg.price_hint) if leg.price_hint > 0 else None

    def _load_greek_risk_config(self) -> OiMlGreekRiskConfig:
        return OiMlGreekRiskConfig(
            enabled=_parse_bool(self.params.get("greek_risk_enabled"), default=True),
            require_greeks=_parse_bool(self.params.get("require_greeks"), default=True),
            require_oi_wall=_parse_bool(self.params.get("require_oi_wall"), default=True),
            target_abs_delta=float(self.params.get("target_abs_delta", 0.20)),
            min_abs_delta=float(self.params.get("min_abs_delta", 0.05)),
            max_abs_delta=float(self.params.get("max_abs_delta", 0.35)),
            max_abs_gamma=float(self.params.get("max_abs_gamma", 0.0030)),
            max_abs_vega=_optional_float(self.params.get("max_abs_vega")),
            force_spread_abs_gamma=float(self.params.get("force_spread_abs_gamma", 0.0015)),
            force_spread_abs_vega=float(self.params.get("force_spread_abs_vega", 8.0)),
            size_down_abs_delta=float(self.params.get("size_down_abs_delta", 0.25)),
            size_down_abs_gamma=float(self.params.get("size_down_abs_gamma", 0.0012)),
            size_down_abs_vega=float(self.params.get("size_down_abs_vega", 7.0)),
            size_down_lot_multiplier=float(self.params.get("size_down_lot_multiplier", 0.50)),
            exit_abs_delta=float(self.params.get("exit_abs_delta", 0.45)),
            exit_abs_gamma=float(self.params.get("exit_abs_gamma", 0.0040)),
            exit_iv_expansion_pct=float(self.params.get("exit_iv_expansion_pct", 0.25)),
            tighten_abs_delta=float(self.params.get("tighten_abs_delta", 0.30)),
            tighten_abs_gamma=float(self.params.get("tighten_abs_gamma", 0.0020)),
            tighten_iv_expansion_pct=float(self.params.get("tighten_iv_expansion_pct", 0.15)),
            tightened_stop_loss_mult_credit=float(
                self.params.get("tightened_stop_loss_mult_credit", 1.25)
            ),
        )

    def _refresh_open_spread_greeks(self, now: datetime) -> None:
        if not self.open_spreads or self.decision_engine is None:
            return
        repository = getattr(self.decision_engine, "repository", None)
        fetch = getattr(repository, "fetch_latest_snapshot", None)
        if not callable(fetch):
            return
        decision_ts = _as_ist(now).astimezone(timezone.utc)
        min_snapshot_ts = decision_ts - timedelta(
            seconds=max(0, int(self.max_quote_age_seconds))
        )
        provider = getattr(getattr(self.decision_engine, "config", None), "provider", None)
        expiries = sorted({spread.intent.expiry for spread in self.open_spreads.values()})
        for expiry in expiries:
            try:
                snapshot = fetch(
                    underlying=self.underlying,
                    expiry=expiry,
                    decision_ts=decision_ts,
                    min_snapshot_ts=min_snapshot_ts,
                    provider=provider,
                )
            except Exception:
                logger.debug("[%s] open-spread Greek refresh failed", self._strategy_id, exc_info=True)
                continue
            rows = [quote.normalized() for quote in snapshot]
            for spread_id, spread in self.open_spreads.items():
                if spread.intent.expiry != expiry:
                    continue
                quote = _find_short_quote(rows, spread.short_leg)
                if quote is not None:
                    spread.metadata["current_greeks"] = greek_metadata_from_quote(quote)

    def _greek_exit_reason(self, spread: OiMlOpenSpread) -> str | None:
        cfg = self.greek_risk_config
        if not cfg.enabled:
            return None
        current = dict(spread.metadata.get("current_greeks") or {})
        abs_delta = _metadata_float(current, "abs_delta")
        abs_gamma = _metadata_float(current, "abs_gamma")
        if abs_delta is not None and abs_delta >= float(cfg.exit_abs_delta):
            return "GREEK_DELTA_STOP"
        if abs_gamma is not None and abs_gamma >= float(cfg.exit_abs_gamma):
            return "GREEK_GAMMA_STOP"
        if self._iv_expansion_pct(spread) >= float(cfg.exit_iv_expansion_pct):
            return "GREEK_IV_EXPANSION_STOP"
        return None

    def _effective_stop_loss_mult_credit(self, spread: OiMlOpenSpread) -> tuple[float, bool]:
        cfg = self.greek_risk_config
        base = float(self.stop_loss_mult_credit)
        if not cfg.enabled:
            return base, False
        current = dict(spread.metadata.get("current_greeks") or {})
        tighten = False
        abs_delta = _metadata_float(current, "abs_delta")
        abs_gamma = _metadata_float(current, "abs_gamma")
        if abs_delta is not None and abs_delta >= float(cfg.tighten_abs_delta):
            tighten = True
        if abs_gamma is not None and abs_gamma >= float(cfg.tighten_abs_gamma):
            tighten = True
        if self._iv_expansion_pct(spread) >= float(cfg.tighten_iv_expansion_pct):
            tighten = True
        if not tighten:
            return base, False
        return min(base, float(cfg.tightened_stop_loss_mult_credit)), True

    @staticmethod
    def _iv_expansion_pct(spread: OiMlOpenSpread) -> float:
        entry = dict(spread.metadata.get("entry_greeks") or {})
        current = dict(spread.metadata.get("current_greeks") or {})
        entry_iv = _metadata_float(entry, "iv")
        current_iv = _metadata_float(current, "iv")
        if entry_iv is None or entry_iv <= 0 or current_iv is None:
            return 0.0
        return max(0.0, (current_iv - entry_iv) / entry_iv)


def _metadata_float(values: Dict[str, Any], key: str) -> float | None:
    value = values.get(key)
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _find_short_quote(rows: list[Any], leg: OiMlOrderIntentLeg) -> Any | None:
    for row in rows:
        if (
            row.strike == int(leg.strike)
            and row.option_type == str(leg.option_type).strip().upper()
            and (
                not leg.symbol_token
                or not row.symbol_token
                or str(row.symbol_token) == str(leg.symbol_token)
            )
        ):
            return row
    return None


__all__ = ["OiMlCeSellerStrategy", "OiMlOpenSpread"]
