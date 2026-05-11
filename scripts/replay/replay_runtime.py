"""
Deterministic replay runtime used by scripts.replay.replay_engine.

This module keeps the implementation isolated so replay_engine.py can remain a
small compatibility surface for existing imports and tests.
"""

from __future__ import annotations

import logging
import math
import os
from collections import defaultdict
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional, Sequence
from unittest.mock import MagicMock, patch

import psycopg

from app.config.boot_config import StrategyValueResolver
from app.core.clock import SimulatedClock
from app.core.maintenance import get_trading_session_calendar
from app.orders.replay_context import isolated_replay_order_sink
from app.strategies.adaptive.dynamic_policy import parse_dynamic_policy_config
from app.strategies.restart_state import sync_open_position_state as _REAL_SYNC_OPEN_POSITION_STATE
from app.strategies.adaptive.market_context import MarketContextBuilder
from app.strategies.adaptive.regime import CLASSIFIER_VERSION, Regime, RegimeClassifier

from scripts.replay.execution_models import (
    END_POLICY_DAILY_MTM,
    END_POLICY_FORCE_EXIT,
    ExecutionConfig,
    build_tick_path,
    normalize_execution_config,
)
from scripts.replay.mock_execution import (
    MockExecutionRecorder,
    MockOrderClient,
    MockRiskManager,
    ReplayMarketContext,
    build_mock_instrument_meta,
)
from scripts.replay.order_sink import ReplayOrderSink
from scripts.replay.schema import (
    build_select_list,
    inspect_table_schema,
    normalize_table_name,
    regime_sidecar_table_name,
)

logger = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
_NIFTY_TUESDAY_EXPIRY_EFFECTIVE = date(2025, 9, 1)

_REQUIRED_COLUMNS = (
    "ts_start",
    "ts_end",
    "label",
    "timeframe_seconds",
    "o",
    "h",
    "l",
    "c",
)
_OPTIONAL_COLUMNS = (
    "atr",
    "rsi",
    "macd",
    "macd_signal",
    "macd_hist",
    "ema_20",
    "ema_30",
    "ema_50",
    "exclusive_nifty_ce_buy_ema20_30s",
    "adx",
    "plus_di",
    "minus_di",
    "di_spread",
)


@dataclass
class BarRow:
    """A single replay row from indicator_bars."""

    ts_start: datetime
    ts_end: Optional[datetime]
    label: str
    timeframe_seconds: int
    o: float
    h: float
    l: float  # noqa: E741
    c: float
    atr: Optional[float]
    rsi: Optional[float]
    macd: Optional[float]
    macd_signal: Optional[float]
    macd_hist: Optional[float]
    ema_20: Optional[float]
    ema_30: Optional[float]
    ema_50: Optional[float]
    exclusive_nifty_ce_buy_ema20_30s: Optional[float]
    adx: Optional[float]
    plus_di: Optional[float]
    minus_di: Optional[float]
    di_spread: Optional[float]
    derived_indicators: Dict[str, Any] = field(default_factory=dict)
    regime: Optional[str] = None
    series_index: int = 0

    @property
    def event_ts(self) -> datetime:
        if self.ts_end is not None:
            return self.ts_end.astimezone(UTC)
        return (self.ts_start + timedelta(seconds=int(self.timeframe_seconds))).astimezone(UTC)


class ReplayBarBatch(list):
    """List-like replay rows with attached schema/data profile metadata."""

    def __init__(
        self,
        rows: Iterable[BarRow],
        *,
        replay_profile: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(rows)
        self.replay_profile: Dict[str, Any] = dict(replay_profile or {})


UNDERLYING_MAP = {
    "NIFTY": {
        "env_prefix": "NIFTY_",
        "underlying_label": "NIFTY_IDX",
        "lot_size": 65,
        "exchange": "NSE",
    },
    "BANKNIFTY": {
        "env_prefix": "BANKNIFTY_",
        "underlying_label": "BANKNIFTY_IDX",
        "lot_size": 30,
        "exchange": "NSE",
    },
    "NG_FUT": {
        "env_prefix": "NG_",
        "underlying_label": "NG_FUT",
        "lot_size": 1250,
        "exchange": "MCX",
    },
}

VALID_COMBINATIONS = {
    "ema20_strategy": ["NIFTY", "BANKNIFTY", "NG_FUT"],
    "put_momentum_scalper": ["NIFTY", "BANKNIFTY"],
    "exclusive_nifty_ce_buy": ["NIFTY"],
    "nifty_weekly_credit_spreads": ["NIFTY"],
}

INVALID_COMBINATIONS_REASONS = {
    ("put_momentum_scalper", "NG_FUT"): (
        "PUT_MOM requires NSE/NFO weekly Put options. NG_FUT trades on MCX "
        "with a different option chain structure (commodity options with "
        "different expiry cycles, lot sizes, and margin rules). The strategy's "
        "strike selection logic (_select_put_option) expects NFO-style ATM/ITM "
        "put instruments which are not available for MCX Natural Gas."
    ),
    ("exclusive_nifty_ce_buy", "BANKNIFTY"): (
        "ExclusiveNiftyCeBuy is registered as underlying_agnostic=False in the "
        "strategy registry and is explicitly designed for NIFTY index only. "
        "The signal parameters (RSI 58-72, ADX/DI thresholds, vol_quantile) "
        "were calibrated exclusively for NIFTY's price characteristics."
    ),
    ("exclusive_nifty_ce_buy", "NG_FUT"): (
        "ExclusiveNiftyCeBuy is NIFTY-exclusive (underlying_agnostic=False). "
        "NG_FUT is a commodity futures contract on MCX - no CE option chain "
        "compatible with the strategy's _select_call_option logic."
    ),
}


def _mock_settings() -> MagicMock:
    s = MagicMock()
    s.use_hub_router_for_nifty_options = False
    s.use_hub_router_for_banknifty_options = False
    s.use_hub_router_for_finnifty_options = False
    s.use_hub_router_for_sensex_options = False
    s.use_hub_router_for_midcpnifty_options = False
    s.use_hub_router_for_ng_options = False
    return s


def load_bars_from_postgres(
    dsn: str,
    label: str,
    timeframe_seconds: Optional[int] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    table: str = "indicator_bars",
    chunk_size: int = 5000,
    strategy_id: Optional[str] = None,
    strategy_params: Optional[Dict[str, Any]] = None,
) -> ReplayBarBatch:
    """Load historical bars from Postgres, preserving schema diagnostics."""

    schema = inspect_table_schema(
        dsn,
        table,
        required_columns=_REQUIRED_COLUMNS,
        optional_columns=_OPTIONAL_COLUMNS,
    )
    normalized_table, _ = normalize_table_name(table)
    conditions = ["label = %s"]
    params: List[Any] = [label]
    if timeframe_seconds is not None:
        conditions.append("timeframe_seconds = %s")
        params.append(int(timeframe_seconds))
    if start_date is not None:
        conditions.append("ts_start >= %s")
        params.append(_session_start_ist(start_date))
    if end_date is not None:
        conditions.append("ts_start < %s")
        params.append(_session_start_ist(end_date + timedelta(days=1)))

    rows: List[BarRow] = []
    null_counts = {column: 0 for column in _OPTIONAL_COLUMNS}
    fetch_size = max(1, int(chunk_size or 0))
    regime_sidecar_table, _ = regime_sidecar_table_name(table)
    regime_versions = _replay_regime_classifier_versions(
        strategy_id=strategy_id,
        strategy_params=strategy_params,
    )
    regime_sidecar_available = False
    with psycopg.connect(dsn, autocommit=True) as conn:
        regime_sidecar_available = (
            "id" in schema.available_set
            and _regime_sidecar_available(conn, regime_sidecar_table)
        )
        select_list = build_select_list(schema, _REQUIRED_COLUMNS + _OPTIONAL_COLUMNS)
        regime_select = "NULL::TEXT AS replay_regime"
        query_params = list(params)
        if regime_sidecar_available:
            version_placeholders = ", ".join(["%s"] * len(regime_versions))
            regime_select = (
                f"(SELECT br.regime FROM {regime_sidecar_table} br "
                f"WHERE br.bar_id = {normalized_table}.id "
                f"AND br.classifier_version IN ({version_placeholders}) "
                "ORDER BY CASE WHEN br.classifier_version = %s THEN 0 ELSE 1 END "
                "LIMIT 1) AS replay_regime"
            )
            query_params = [*regime_versions, regime_versions[0], *query_params]
        query = f"""
            SELECT {select_list}, {regime_select}
            FROM {normalized_table}
            WHERE {" AND ".join(conditions)}
            ORDER BY ts_start ASC
        """
        with conn.cursor() as cur:
            cur.execute(query, query_params)
            while True:
                batch = cur.fetchmany(fetch_size)
                if not batch:
                    break
                for raw in batch:
                    row = _build_bar_row(raw, series_index=len(rows))
                    rows.append(row)
                    for indicator_name in _OPTIONAL_COLUMNS:
                        if getattr(row, indicator_name) is None:
                            null_counts[indicator_name] += 1

    synthetic_counts: Dict[str, int] = {}
    if str(strategy_id or "") == "exclusive_nifty_ce_buy":
        synthetic_counts.update(_backfill_exclusive_ce_ema(rows))
    synthetic_counts.update(_attach_synthetic_regimes(rows))

    profile = {
        "label": label,
        "timeframe_seconds": int(timeframe_seconds) if timeframe_seconds is not None else None,
        "bars_loaded": len(rows),
        "timezone": "Asia/Kolkata",
        "source_table": normalized_table,
        "regime_sidecar_table": regime_sidecar_table,
        "regime_sidecar_available": bool(regime_sidecar_available),
        "regime_classifier_versions": list(regime_versions),
        "available_columns": list(schema.available_columns),
        "missing_optional_columns": list(schema.missing_optional_columns),
        "null_indicator_counts": {k: int(v) for k, v in sorted(null_counts.items()) if int(v) > 0},
        "synthetic_indicator_counts": synthetic_counts,
        "regime_classifier_version": CLASSIFIER_VERSION,
    }
    logger.info(
        "Loaded %d bars for label=%s tf=%s range=[%s, %s] missing_optional_columns=%s",
        len(rows),
        label,
        timeframe_seconds,
        start_date,
        end_date,
        profile["missing_optional_columns"],
    )
    return ReplayBarBatch(rows, replay_profile=profile)


def _regime_sidecar_available(conn: Any, sidecar_table: str) -> bool:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)", (sidecar_table,))
            row = cur.fetchone()
            if not row or row[0] is None:
                return False
            cur.execute(f"SELECT bar_id, regime, classifier_version FROM {sidecar_table} WHERE FALSE")
            description = getattr(cur, "description", None) or ()
            columns = {str(col.name) for col in description if getattr(col, "name", None)}
            return {"bar_id", "regime", "classifier_version"}.issubset(columns)
    except Exception:
        logger.debug(
            "Replay regime sidecar unavailable table=%s",
            sidecar_table,
            exc_info=True,
        )
        return False


def _replay_regime_classifier_versions(
    *,
    strategy_id: Optional[str],
    strategy_params: Optional[Dict[str, Any]],
) -> tuple[str, ...]:
    del strategy_id
    params = strategy_params if isinstance(strategy_params, dict) else {}
    policy_cfg = parse_dynamic_policy_config(params.get("dynamic_policy") or {})
    if policy_cfg.enabled and policy_cfg.policy_hash:
        return (f"{CLASSIFIER_VERSION}:{policy_cfg.policy_hash}", CLASSIFIER_VERSION)
    return (CLASSIFIER_VERSION,)


def bar_to_candle(bar: BarRow) -> SimpleNamespace:
    return SimpleNamespace(
        start_ts=bar.ts_start,
        end_ts=bar.ts_end or bar.event_ts,
        o=bar.o,
        h=bar.h,
        low=bar.l,
        l=bar.l,
        c=bar.c,
    )


def bar_to_indicators(
    bar: BarRow,
    *,
    strategy_id: Optional[str] = None,
    close_history: Optional[Sequence[float]] = None,
    requested_ema_period: Optional[int] = None,
) -> Dict[str, Any]:
    indicators: Dict[str, Any] = {}
    for key in (
        "atr",
        "rsi",
        "macd",
        "macd_signal",
        "macd_hist",
        "ema_20",
        "ema_30",
        "ema_50",
        "adx",
        "plus_di",
        "minus_di",
        "di_spread",
    ):
        value = getattr(bar, key)
        if value is not None:
            indicators[key] = value

    if bar.exclusive_nifty_ce_buy_ema20_30s is not None:
        indicators["exclusive_nifty_ce_buy_ema20_30s"] = bar.exclusive_nifty_ce_buy_ema20_30s
        if strategy_id == "exclusive_nifty_ce_buy" and indicators.get("ema_20") is None:
            indicators["ema_20"] = bar.exclusive_nifty_ce_buy_ema20_30s

    for key, value in dict(getattr(bar, "derived_indicators", {}) or {}).items():
        if value is not None and indicators.get(str(key)) is None:
            indicators[str(key)] = value

    if bar.regime:
        indicators["regime"] = str(bar.regime)

    preview = list(close_history or [])
    preview.append(float(bar.c))
    derived_periods = {20, 30, 50}
    if requested_ema_period is not None:
        derived_periods.add(int(requested_ema_period))
    for period in sorted(derived_periods):
        key = f"ema_{int(period)}"
        if indicators.get(key) is not None:
            continue
        ema_value = _compute_ema(preview, int(period))
        if ema_value is not None:
            indicators[key] = ema_value

    return indicators


def build_ema20_strategy(
    underlying_key: str,
    params: Optional[Dict[str, Any]] = None,
) -> Any:
    from app.strategies.ema20_strategy import Ema20Strategy

    uinfo = UNDERLYING_MAP[underlying_key]
    meta = build_mock_instrument_meta(uinfo["underlying_label"], uinfo["lot_size"])
    p = params or {}
    return Ema20Strategy(
        instrument_meta=meta,
        env_prefix=uinfo["env_prefix"],
        underlying_label=uinfo["underlying_label"],
        product_type="INTRADAY",
        signal_timeframe=int(p.get("signal_timeframe", p.get("timeframe_seconds", 300))),
        ema_period=int(p.get("ema_period", 20)),
        min_atr=p.get("min_atr"),
        require_rsi_falling=p.get("require_rsi_falling", True),
        use_adx_filter=bool(p.get("use_adx_filter", False)),
        adx_period=int(p.get("adx_period", 14)),
        min_adx=float(p.get("min_adx", 18.0)),
        require_bearish_di=bool(p.get("require_bearish_di", True)),
        min_di_spread=float(p.get("min_di_spread", 0.0)),
        sl_pct=float(p.get("sl_pct", 0.30)),
        tp_pct=float(p.get("tp_pct", 0.30)),
        trail_buffer_pct=float(p.get("trail_buffer_pct", 0.0)),
        trail_trigger_pct=p.get("trail_trigger_pct"),
        first_entry_time=p.get("first_entry_time"),
        square_off_time=p.get("square_off_time"),
        # PHX#182/#183/#184: profit-booking enhancements.
        tp1_pct=p.get("tp1_pct"),
        tp1_qty_pct=float(p.get("tp1_qty_pct", 0.0)),
        giveback_pct=p.get("giveback_pct"),
        giveback_arm_pct=p.get("giveback_arm_pct"),
        decay_tighten_minutes_before_eod=p.get("decay_tighten_minutes_before_eod"),
        decay_tp_multiplier=float(p.get("decay_tp_multiplier", 1.0)),
        decay_trail_buffer_multiplier=float(p.get("decay_trail_buffer_multiplier", 1.0)),
        risk_manager=MockRiskManager(),
        config_resolver=StrategyValueResolver(
            env_prefix=uinfo["env_prefix"],
            params=p,
            env=dict(os.environ),
        ),
    )


def build_put_momentum_strategy(
    underlying_key: str,
    params: Optional[Dict[str, Any]] = None,
) -> Any:
    from app.strategies.put_momentum_scalper import PutMomentumScalperStrategy

    uinfo = UNDERLYING_MAP[underlying_key]
    meta = build_mock_instrument_meta(uinfo["underlying_label"], uinfo["lot_size"])
    replay_params = dict(params or {})
    return PutMomentumScalperStrategy(
        instrument_meta=meta,
        order_client=MockOrderClient(),
        risk_manager=MockRiskManager(),
        env_prefix=uinfo["env_prefix"],
        underlying_label=uinfo["underlying_label"],
        params=replay_params,
        config_resolver=StrategyValueResolver(
            env_prefix=uinfo["env_prefix"],
            params=replay_params,
            env=dict(os.environ),
        ),
    )


def build_exclusive_ce_strategy(
    underlying_key: str,
    params: Optional[Dict[str, Any]] = None,
) -> Any:
    from app.strategies.exclusive_nifty_ce_buy import ExclusiveNiftyCeBuyStrategy

    uinfo = UNDERLYING_MAP[underlying_key]
    meta = build_mock_instrument_meta(uinfo["underlying_label"], uinfo["lot_size"])
    replay_params = dict(params or {})
    return ExclusiveNiftyCeBuyStrategy(
        instrument_meta=meta,
        order_client=MockOrderClient(),
        risk_manager=MockRiskManager(),
        env_prefix=uinfo["env_prefix"],
        underlying_label=uinfo["underlying_label"],
        params=replay_params,
        config_resolver=StrategyValueResolver(
            env_prefix=uinfo["env_prefix"],
            params=replay_params,
            env=dict(os.environ),
        ),
    )


def build_nifty_spread_strategy(
    underlying_key: str,
    params: Optional[Dict[str, Any]] = None,
) -> Any:
    import app.strategies.nifty_weekly_credit_spreads as spread_mod

    if underlying_key != "NIFTY":
        raise ValueError("nifty_weekly_credit_spreads replay is NIFTY-only")
    spread_mod.sync_open_position_state = _REAL_SYNC_OPEN_POSITION_STATE
    uinfo = UNDERLYING_MAP[underlying_key]
    replay_params = {
        "account_equity": 1_000_000,
        "lot_size": uinfo["lot_size"],
        "entry_start_time": "09:15",
        "entry_end_time": "15:10",
        "min_days_to_expiry": 0,
        "max_days_to_expiry": 7,
        "atm_straddle_min_pct": 0.001,
        "atm_straddle_max_pct": 0.20,
        "atr_max_pct_5m": 0.10,
        "risk_per_trade_pct": 0.50,
        "max_total_risk_pct": 1.00,
        "max_open_spreads": 10,
    }
    replay_params.update(dict(params or {}))
    return spread_mod.NiftyWeeklyCreditSpreadStrategy(
        instrument_meta=build_nifty_spread_instrument_meta(
            uinfo["underlying_label"],
            uinfo["lot_size"],
        ),
        order_client=MockOrderClient(),
        risk_manager=MockRiskManager(),
        env_prefix=uinfo["env_prefix"],
        underlying_label=uinfo["underlying_label"],
        params=replay_params,
        config_resolver=StrategyValueResolver(
            env_prefix=uinfo["env_prefix"],
            params=replay_params,
            env=dict(os.environ),
        ),
    )


STRATEGY_BUILDERS = {
    "ema20_strategy": build_ema20_strategy,
    "put_momentum_scalper": build_put_momentum_strategy,
    "exclusive_nifty_ce_buy": build_exclusive_ce_strategy,
    "nifty_weekly_credit_spreads": build_nifty_spread_strategy,
}


@dataclass
class ReplayConfig:
    """Configuration for a single replay run."""

    dsn: str
    strategy_id: str
    underlying_key: str
    strategy_params: Dict[str, Any]
    timeframes: List[int]
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    table: str = "indicator_bars"
    isolated_execution: bool = True
    chunk_size: int = 5000
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    filter_regime: Optional[str] = None


class ReplayEngine:
    """Deterministic bar-by-bar replay harness."""

    def __init__(self, config: ReplayConfig) -> None:
        self.config = config
        self.recorder = MockExecutionRecorder(
            execution_config=normalize_execution_config(config.execution)
        )
        self._clock: Optional[SimulatedClock] = None
        self._strategy: Optional[Any] = None
        self._close_history: Dict[int, List[float]] = defaultdict(list)
        self._bars_by_timeframe: Dict[int, List[BarRow]] = {}
        self._price_book_state: Dict[str, float] = {}
        self._current_session_date: Optional[date] = None

    def _patch_strategy_clock(self, strategy: Any) -> None:
        if self._clock is None:
            return
        strategy._now_ist = lambda: self._clock.now_local(IST)
        strategy._now_utc = lambda: self._clock.now_utc()
        if self.config.strategy_id == "nifty_weekly_credit_spreads":
            strategy._current_expiry_and_chain = (  # type: ignore[attr-defined]
                lambda: _next_weekly_expiry(strategy._now_ist().date())
            )

    def _build_indicator_payload(self, bar: BarRow) -> Dict[str, Any]:
        requested_ema_period: Optional[int] = None
        if self.config.strategy_id == "ema20_strategy":
            requested_ema_period = int(self.config.strategy_params.get("ema_period", 20))
        return bar_to_indicators(
            bar,
            strategy_id=self.config.strategy_id,
            close_history=self._close_history.get(int(bar.timeframe_seconds), []),
            requested_ema_period=requested_ema_period,
        )

    def _future_open_for_bar(self, bar: BarRow) -> tuple[Optional[float], Optional[datetime]]:
        series = self._bars_by_timeframe.get(int(bar.timeframe_seconds), [])
        offset = int(self.config.execution.latency_bars) + 1
        target_index = int(bar.series_index) + offset
        if target_index >= len(series):
            return None, None
        future_bar = series[target_index]
        if future_bar.event_ts.astimezone(IST).date() != bar.event_ts.astimezone(IST).date():
            return None, None
        return float(future_bar.o), future_bar.ts_start

    def _build_strategy_price_map(
        self,
        *,
        underlying_price: float,
        atr: Optional[float],
    ) -> Dict[str, float]:
        strategy = self._strategy
        label = UNDERLYING_MAP[self.config.underlying_key]["underlying_label"]
        key = self.config.underlying_key.replace("_FUT", "").replace("_IDX", "")
        ce_label = f"{key}_ATM_CE"
        pe_label = f"{key}_ATM_PE"

        session_anchor = self._price_book_state.get("anchor_underlying")
        if self._current_session_date is None or session_anchor is None:
            base_premium = max(
                1.0,
                float(atr or 0.0) * 2.0 if atr is not None else 0.0,
                abs(float(underlying_price)) * 0.02,
            )
            self._price_book_state = {
                "anchor_underlying": float(underlying_price),
                "anchor_premium": float(base_premium),
            }

        move = float(underlying_price) - float(self._price_book_state["anchor_underlying"])
        premium_anchor = float(self._price_book_state["anchor_premium"])
        delta = 0.35
        ce_price = max(0.5, premium_anchor + (delta * move))
        pe_price = max(0.5, premium_anchor - (delta * move))

        prices = {
            label: float(underlying_price),
            ce_label: float(ce_price),
            pe_label: float(pe_price),
        }
        if strategy is not None:
            meta_map = getattr(strategy, "instrument_meta", {}) or {}
            if isinstance(meta_map, dict):
                for option_label, meta in meta_map.items():
                    if option_label == label or not isinstance(meta, dict):
                        continue
                    option_type = _option_type_from_meta(option_label, meta)
                    strike = _strike_from_label(option_label)
                    if option_type not in {"CE", "PE"} or strike is None:
                        continue
                    prices[str(option_label)] = _option_proxy_price(
                        underlying_price=float(underlying_price),
                        strike=float(strike),
                        option_type=option_type,
                        atr=atr,
                    )
        return prices

    def _update_strategy_last_prices(
        self,
        *,
        underlying_price: float,
        atr: Optional[float],
    ) -> Dict[str, float]:
        strategy = self._strategy
        prices = self._build_strategy_price_map(
            underlying_price=underlying_price,
            atr=atr,
        )
        if strategy is not None and hasattr(strategy, "last_price"):
            strategy.last_price.update(prices)
        return prices

    def _recorder_has_open_position(self) -> bool:
        """Return True when replay fills show an unpaired entry for this run."""

        open_qty = 0
        for fill in self.recorder.fills:
            if fill.strategy_id != self.config.strategy_id:
                continue
            if fill.underlying != self.config.underlying_key:
                continue
            qty = int(getattr(fill, "quantity", 0) or 0)
            if fill.purpose == "ENTRY":
                open_qty += qty
            elif fill.purpose == "EXIT":
                open_qty = max(0, open_qty - qty)
        return open_qty > 0

    def _finalize_open_positions(
        self,
        *,
        last_bar: BarRow,
        label: str,
        reason: str,
        submit_orders: bool = True,
    ) -> None:
        strategy = self._strategy
        if strategy is None:
            return
        if not submit_orders and not self._recorder_has_open_position():
            return

        underlying_price = float(last_bar.c)
        indicators = self._build_indicator_payload(last_bar)
        price_map = self._update_strategy_last_prices(
            underlying_price=underlying_price,
            atr=last_bar.atr,
        )
        fill_context = ReplayMarketContext(
            timestamp=last_bar.event_ts,
            underlying=self.config.underlying_key,
            current_price=underlying_price,
            phase="forced_finalization",
            timeframe_seconds=last_bar.timeframe_seconds,
            bar_start_ts=last_bar.ts_start,
            bar_end_ts=last_bar.ts_end or last_bar.event_ts,
            price_open=last_bar.o,
            price_high=last_bar.h,
            price_low=last_bar.l,
            price_close=last_bar.c,
            indicators=indicators,
            instrument_prices=price_map,
        )
        self.recorder.set_market_context(fill_context)
        self.recorder.add_finalization_event(
            strategy_id=self.config.strategy_id,
            underlying=self.config.underlying_key,
            reason=reason,
            timestamp=fill_context.timestamp.isoformat(),
            price=underlying_price,
            realized=bool(submit_orders),
        )
        if not submit_orders:
            return
        force_exit_all = getattr(strategy, "force_exit_all", None)
        if not callable(force_exit_all):
            return
        try:
            force_exit_all(reason=reason, submit_orders=True)
        except Exception as exc:
            logger.warning(
                "Replay finalization failed to close open positions | strategy=%s underlying=%s reason=%s err=%s",
                self.config.strategy_id,
                self.config.underlying_key,
                reason,
                exc,
                exc_info=True,
            )

    def _dispatch_ticks_for_bar(self, *, bar: BarRow, indicators: Dict[str, Any]) -> None:
        strategy = self._strategy
        if strategy is None or self._clock is None:
            return

        next_open_price, next_open_ts = self._future_open_for_bar(bar)
        for tick in build_tick_path(
            ts_start=bar.ts_start,
            ts_end=bar.ts_end or bar.event_ts,
            o=bar.o,
            h=bar.h,
            l=bar.l,
            c=bar.c,
            tick_model=self.config.execution.tick_model,
        ):
            self._clock._current_utc = tick.timestamp.astimezone(UTC)
            price_map = self._update_strategy_last_prices(
                underlying_price=float(tick.price),
                atr=bar.atr,
            )
            self.recorder.set_market_context(
                ReplayMarketContext(
                    timestamp=tick.timestamp,
                    underlying=self.config.underlying_key,
                    current_price=float(tick.price),
                    phase=tick.phase,
                    timeframe_seconds=bar.timeframe_seconds,
                    bar_start_ts=bar.ts_start,
                    bar_end_ts=bar.ts_end or bar.event_ts,
                    price_open=bar.o,
                    price_high=bar.h,
                    price_low=bar.l,
                    price_close=bar.c,
                    next_open_price=next_open_price,
                    next_open_ts=next_open_ts,
                    indicators=indicators,
                    instrument_prices=price_map,
                )
            )
            try:
                strategy.on_tick(bar.label, float(tick.price))
            except Exception as exc:
                logger.debug("on_tick exception at %s phase=%s: %s", tick.timestamp, tick.phase, exc)

    def run(self) -> MockExecutionRecorder:
        cfg = self.config
        uinfo = UNDERLYING_MAP[cfg.underlying_key]
        label = uinfo["underlying_label"]

        all_bars: List[BarRow] = []
        db_bar_count_by_timeframe: Dict[int, int] = {}
        merged_profile = {
            "strategy_id": cfg.strategy_id,
            "underlying_key": cfg.underlying_key,
            "underlying_label": label,
            "timeframes": list(cfg.timeframes),
            "bars_loaded_by_timeframe": {},
            "missing_optional_columns_by_timeframe": {},
            "null_indicator_counts_by_timeframe": {},
            "synthetic_indicator_counts_by_timeframe": {},
            "available_columns": [],
            "execution": cfg.execution.as_dict(),
            "filter_regime": cfg.filter_regime,
            "timezone": "Asia/Kolkata",
            "source_table": cfg.table,
        }

        for tf in cfg.timeframes:
            batch = load_bars_from_postgres(
                dsn=cfg.dsn,
                label=label,
                timeframe_seconds=tf,
                start_date=cfg.start_date,
                end_date=cfg.end_date,
                table=cfg.table,
                chunk_size=cfg.chunk_size,
                strategy_id=cfg.strategy_id,
                strategy_params=cfg.strategy_params,
            )
            bars = list(batch)
            _precompute_derived_indicator_history(
                bars,
                strategy_id=cfg.strategy_id,
                strategy_params=cfg.strategy_params,
            )
            self._bars_by_timeframe[int(tf)] = list(bars)
            if cfg.filter_regime:
                wanted = _normalize_regime_name(cfg.filter_regime)
                bars = [
                    bar
                    for bar in bars
                    if _normalize_regime_name(bar.regime) == wanted
                ]
            profile = dict(getattr(batch, "replay_profile", {}) or {})
            db_bar_count_by_timeframe[int(tf)] = len(bars)
            merged_profile["bars_loaded_by_timeframe"][str(int(tf))] = len(bars)
            merged_profile["missing_optional_columns_by_timeframe"][str(int(tf))] = list(
                profile.get("missing_optional_columns") or []
            )
            merged_profile["null_indicator_counts_by_timeframe"][str(int(tf))] = dict(
                profile.get("null_indicator_counts") or {}
            )
            merged_profile["synthetic_indicator_counts_by_timeframe"][str(int(tf))] = dict(
                profile.get("synthetic_indicator_counts") or {}
            )
            available_columns = set(merged_profile.get("available_columns") or [])
            available_columns.update(profile.get("available_columns") or [])
            merged_profile["available_columns"] = sorted(available_columns)
            all_bars.extend(bars)

        self.recorder.set_db_bar_counts(db_bar_count_by_timeframe)
        self.recorder.set_data_profile(merged_profile)
        if not all_bars:
            logger.warning(
                "No bars found for %s %s timeframes=%s",
                cfg.strategy_id,
                cfg.underlying_key,
                cfg.timeframes,
            )
            return self.recorder

        all_bars.sort(
            key=lambda bar: (
                bar.event_ts,
                -int(bar.timeframe_seconds),
                bar.ts_start,
                int(bar.series_index),
            )
        )
        self.recorder.set_replay_window(
            trading_dates=[bar.event_ts.astimezone(IST).date() for bar in all_bars]
        )
        self._clock = SimulatedClock(all_bars[0].event_ts)
        replay_order_sink = ReplayOrderSink(recorder=self.recorder)

        with ExitStack() as stack:
            stack.enter_context(patch("app.config.settings.get_settings", return_value=_mock_settings()))
            stack.enter_context(patch("app.strategies.restart_state.find_restored_position", return_value=None))
            stack.enter_context(patch("app.strategies.restart_state.sync_open_position_state", return_value=None))
            stack.enter_context(
                patch(
                    "app.strategies.exclusive_nifty_ce_buy.build_exclusive_nifty_ce_indicator_store",
                    return_value=None,
                )
            )
            if cfg.isolated_execution:
                stack.enter_context(isolated_replay_order_sink(replay_order_sink))

            builder = STRATEGY_BUILDERS[cfg.strategy_id]
            self._strategy = builder(cfg.underlying_key, cfg.strategy_params)
            self._patch_strategy_clock(self._strategy)
            debug_gate_listener = getattr(self._strategy, "set_debug_gate_listener", None)
            if callable(debug_gate_listener):
                debug_gate_listener(
                    lambda payload, strategy_id=cfg.strategy_id: self.recorder.record_gate_decision(
                        strategy_id=strategy_id,
                        payload=payload,
                    )
                )

            prev_session_date: Optional[date] = None
            prev_bar: Optional[BarRow] = None
            end_policy = str(cfg.execution.end_policy)
            for bar in all_bars:
                session_date = bar.event_ts.astimezone(IST).date()
                if prev_session_date is not None and session_date != prev_session_date and prev_bar is not None:
                    self.recorder.add_session_event(
                        event="session_boundary",
                        previous_session=prev_session_date.isoformat(),
                        next_session=session_date.isoformat(),
                        strategy_id=cfg.strategy_id,
                        underlying=cfg.underlying_key,
                    )
                    # Issue #216: only the legacy force_exit policy closes
                    # at session boundaries. carry_over / daily_mtm let
                    # positions persist across days so PnL reflects natural
                    # exits (TP/SL/strategy-EOD) instead of replay-injected
                    # 15:00-IST market-close marks. Indicator close_history
                    # is preserved across carry-over boundaries so strategy
                    # indicators retain continuity, matching production. The
                    # synthetic option price book is preserved only when an
                    # option position is actually open; flat strategies need a
                    # fresh ATM anchor on the next session so overnight gaps do
                    # not corrupt the next independent option trade's fills.
                    if end_policy == END_POLICY_FORCE_EXIT:
                        self._finalize_open_positions(
                            last_bar=prev_bar,
                            label=label,
                            reason="REPLAY_SESSION_BOUNDARY",
                        )
                        self._price_book_state = {}
                        self._close_history = defaultdict(list)
                    else:
                        if not self._recorder_has_open_position():
                            self._price_book_state = {}
                    if end_policy == END_POLICY_DAILY_MTM:
                        # Snapshot unrealised mark at session close without
                        # closing the position. The recorder treats this as
                        # an audit event; the position remains open into
                        # the next session.
                        self.recorder.add_session_event(
                            event="daily_mtm_snapshot",
                            previous_session=prev_session_date.isoformat(),
                            next_session=session_date.isoformat(),
                            strategy_id=cfg.strategy_id,
                            underlying=cfg.underlying_key,
                            close_price=float(prev_bar.c),
                        )

                self._current_session_date = session_date
                indicators = self._build_indicator_payload(bar)
                self._dispatch_ticks_for_bar(bar=bar, indicators=indicators)

                self._clock._current_utc = bar.event_ts.astimezone(UTC)
                next_open_price, next_open_ts = self._future_open_for_bar(bar)
                price_map = self._update_strategy_last_prices(
                    underlying_price=float(bar.c),
                    atr=bar.atr,
                )
                next_price_map = (
                    self._build_strategy_price_map(
                        underlying_price=float(next_open_price),
                        atr=bar.atr,
                    )
                    if next_open_price is not None
                    else {}
                )
                self.recorder.set_market_context(
                    ReplayMarketContext(
                        timestamp=bar.event_ts,
                        underlying=cfg.underlying_key,
                        current_price=float(bar.c),
                        phase="bar_close",
                        timeframe_seconds=bar.timeframe_seconds,
                        bar_start_ts=bar.ts_start,
                        bar_end_ts=bar.ts_end or bar.event_ts,
                        price_open=bar.o,
                        price_high=bar.h,
                        price_low=bar.l,
                        price_close=bar.c,
                        next_open_price=next_open_price,
                        next_open_ts=next_open_ts,
                        indicators=indicators,
                        instrument_prices=price_map,
                        next_instrument_prices=next_price_map,
                    )
                )
                try:
                    self._strategy.on_bar(label, bar.timeframe_seconds, bar_to_candle(bar), indicators)
                except Exception as exc:
                    logger.debug("on_bar exception at %s: %s", bar.event_ts, exc)

                self._close_history[int(bar.timeframe_seconds)].append(float(bar.c))
                prev_session_date = session_date
                prev_bar = bar

            # Issue #216: only the legacy force_exit policy submits a real
            # window-end EXIT order. carry_over/daily_mtm record an unrealised
            # last-bar mark so reports can disclose open risk without letting
            # the arbitrary replay end date affect realized trade count,
            # win/loss stats, or net_pnl.
            window_end_is_realized = end_policy == END_POLICY_FORCE_EXIT
            window_end_reason = (
                "REPLAY_EOD" if window_end_is_realized else "REPLAY_WINDOW_END_FORCED"
            )
            self._finalize_open_positions(
                last_bar=all_bars[-1],
                label=label,
                reason=window_end_reason,
                submit_orders=window_end_is_realized,
            )
            indicator_snapshot = getattr(self._strategy, "indicator_availability_snapshot", None)
            if callable(indicator_snapshot):
                self.recorder.data_profile["strategy_indicator_snapshot"] = indicator_snapshot()

        return self.recorder


def run_single_replay(
    dsn: str,
    strategy_id: str,
    underlying_key: str,
    params: Optional[Dict[str, Any]] = None,
    timeframes: Optional[List[int]] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    table: str = "indicator_bars",
    chunk_size: int = 5000,
    execution: Optional[ExecutionConfig] = None,
    filter_regime: Optional[str] = None,
) -> MockExecutionRecorder:
    """Convenience wrapper for running a single strategy replay."""

    if timeframes is None:
        if strategy_id == "put_momentum_scalper":
            timeframes = [300, 900]
        elif strategy_id == "exclusive_nifty_ce_buy":
            timeframes = [30]
        else:
            timeframes = [300]

    config = ReplayConfig(
        dsn=dsn,
        strategy_id=strategy_id,
        underlying_key=underlying_key,
        strategy_params=params or {},
        timeframes=timeframes,
        start_date=start_date,
        end_date=end_date,
        table=table,
        isolated_execution=True,
        chunk_size=chunk_size,
        execution=normalize_execution_config(execution),
        filter_regime=_normalize_regime_name(filter_regime),
    )
    return ReplayEngine(config).run()


def _build_bar_row(raw: Sequence[Any], *, series_index: int) -> BarRow:
    ts_start = _ensure_tz(raw[0])
    ts_end = _ensure_tz(raw[1]) if raw[1] is not None else None
    return BarRow(
        ts_start=ts_start,
        ts_end=ts_end,
        label=str(raw[2]),
        timeframe_seconds=int(raw[3]),
        o=_optional_float(raw[4]) or 0.0,
        h=_optional_float(raw[5]) or 0.0,
        l=_optional_float(raw[6]) or 0.0,
        c=_optional_float(raw[7]) or 0.0,
        atr=_optional_float(raw[8]),
        rsi=_optional_float(raw[9]),
        macd=_optional_float(raw[10]),
        macd_signal=_optional_float(raw[11]),
        macd_hist=_optional_float(raw[12]),
        ema_20=_optional_float(raw[13]),
        ema_30=_optional_float(raw[14]),
        ema_50=_optional_float(raw[15]),
        exclusive_nifty_ce_buy_ema20_30s=_optional_float(raw[16]),
        adx=_optional_float(raw[17]),
        plus_di=_optional_float(raw[18]),
        minus_di=_optional_float(raw[19]),
        di_spread=_optional_float(raw[20]),
        regime=(str(raw[21]) if len(raw) > 21 and raw[21] not in (None, "") else None),
        series_index=series_index,
    )


def _ensure_tz(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=IST)
    return value.astimezone(UTC)


def _session_start_ist(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=IST)


def _optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _normalize_regime_name(value: Any) -> Optional[str]:
    text = str(value or "").strip().upper()
    if not text:
        return None
    for regime in Regime:
        if regime.value == text:
            return regime.value
    return text


def _compute_ema(values: Sequence[float], period: int) -> Optional[float]:
    period_int = max(2, int(period))
    if len(values) < period_int:
        return None
    k = 2.0 / (period_int + 1.0)
    ema = float(values[0])
    for value in values[1:]:
        ema = (float(value) * k) + (ema * (1.0 - k))
    return float(ema)


def _strike_from_label(label: Any) -> Optional[float]:
    try:
        return float(str(label or "").rsplit("_", 1)[-1])
    except Exception:
        return None


def _option_type_from_meta(label: Any, meta: Dict[str, Any]) -> Optional[str]:
    text = f"{label} {meta.get('kind', '')} {meta.get('instrument_type', '')}".upper()
    if "CE" in text:
        return "CE"
    if "PE" in text:
        return "PE"
    return None


def _option_proxy_price(
    *,
    underlying_price: float,
    strike: float,
    option_type: str,
    atr: Optional[float],
) -> float:
    intrinsic = (
        max(underlying_price - strike, 0.0)
        if option_type == "CE"
        else max(strike - underlying_price, 0.0)
    )
    base_time_value = max(
        2.0,
        abs(float(underlying_price)) * 0.020,
        float(atr or 0.0) * 2.0,
    )
    width_scale = max(150.0, abs(float(underlying_price)) * 0.015)
    distance = abs(float(strike) - float(underlying_price))
    time_value = max(0.5, base_time_value * math.exp(-distance / width_scale))
    return max(0.5, float(intrinsic + time_value))


def _next_weekly_expiry(session_date: date) -> date:
    # NSE NIFTY weekly expiry moved from Thursday to Tuesday for contracts
    # expiring on or after 2025-09-01. Use the old Thursday candidate only
    # when that candidate itself is before the cutover; transition sessions
    # after the old Thursday roll to the first Tuesday contract.
    thursday_candidate = session_date + timedelta(days=(3 - session_date.weekday()) % 7)
    if thursday_candidate < _NIFTY_TUESDAY_EXPIRY_EFFECTIVE:
        return _previous_nse_trading_day(thursday_candidate)
    tuesday_candidate = session_date + timedelta(days=(1 - session_date.weekday()) % 7)
    return _previous_nse_trading_day(tuesday_candidate)


def _previous_nse_trading_day(value: date) -> date:
    calendar = get_trading_session_calendar()
    out = value
    while True:
        if out.weekday() < 5:
            probe = datetime.combine(out, time(hour=12), tzinfo=IST)
            if not calendar.is_holiday({"exchange": "NSE"}, probe):
                return out
        out -= timedelta(days=1)


def build_nifty_spread_instrument_meta(
    underlying_label: str,
    lot_size: int,
    *,
    min_strike: int = 12000,
    max_strike: int = 32000,
    step: int = 50,
) -> Dict[str, Dict[str, Any]]:
    meta = build_mock_instrument_meta(underlying_label, lot_size)
    meta[underlying_label]["underlying"] = "NIFTY"
    for strike in range(int(min_strike), int(max_strike) + int(step), int(step)):
        for option_type in ("CE", "PE"):
            label = f"NIFTY_{option_type}_{strike}"
            meta[label] = {
                "label": label,
                "broker_symbol": label,
                "symbol": label,
                "exchange": "NFO",
                "symbol_token": f"REPLAY_{option_type}_{strike}",
                "token": f"REPLAY_{option_type}_{strike}",
                "lot_size": int(lot_size),
                "instrument_type": "OPTIDX",
                "kind": option_type,
                "underlying": "NIFTY",
            }
    return meta


def _backfill_exclusive_ce_ema(rows: Sequence[BarRow], *, period: int = 20) -> Dict[str, int]:
    period_int = max(2, int(period))
    alpha = 2.0 / (period_int + 1.0)
    ema: Optional[float] = None
    seen = 0
    backfilled_private = 0
    backfilled_shared = 0
    for row in rows:
        if int(row.timeframe_seconds) != 30:
            continue
        close = _optional_float(row.c)
        if close is None:
            continue
        seen += 1
        ema = close if ema is None else (close * alpha) + (ema * (1.0 - alpha))
        if seen < period_int:
            continue
        if row.exclusive_nifty_ce_buy_ema20_30s is None:
            row.exclusive_nifty_ce_buy_ema20_30s = float(ema)
            backfilled_private += 1
        if row.ema_20 is None:
            row.ema_20 = float(ema)
            backfilled_shared += 1
    out: Dict[str, int] = {}
    if backfilled_private:
        out["exclusive_nifty_ce_buy_ema20_30s"] = backfilled_private
    if backfilled_shared:
        out["ema_20"] = backfilled_shared
    return out


def _precompute_derived_indicator_history(
    rows: Sequence[BarRow],
    *,
    strategy_id: Optional[str],
    strategy_params: Optional[Dict[str, Any]],
) -> None:
    requested_ema_period: Optional[int] = None
    if str(strategy_id or "") == "ema20_strategy":
        params = strategy_params if isinstance(strategy_params, dict) else {}
        requested_ema_period = int(params.get("ema_period", 20))
    close_history: List[float] = []
    for row in rows:
        indicators = bar_to_indicators(
            row,
            strategy_id=strategy_id,
            close_history=close_history,
            requested_ema_period=requested_ema_period,
        )
        for key, value in indicators.items():
            key_text = str(key)
            if not key_text.startswith("ema_"):
                continue
            if value is not None and getattr(row, key_text, None) is None:
                row.derived_indicators[key_text] = value
        close_history.append(float(row.c))


def _attach_synthetic_regimes(rows: Sequence[BarRow]) -> Dict[str, int]:
    builders: Dict[int, MarketContextBuilder] = {}
    classifiers: Dict[int, RegimeClassifier] = {}
    filled = 0
    for row in rows:
        tf = int(row.timeframe_seconds)
        builder = builders.setdefault(tf, MarketContextBuilder())
        classifier = classifiers.setdefault(tf, RegimeClassifier())
        context = builder.build(
            candle=bar_to_candle(row),
            indicators=bar_to_indicators(row),
        )
        regime = classifier.update(context)
        if row.regime:
            continue
        row.regime = regime.value
        filled += 1
    return {"regime": filled} if filled else {}


__all__ = [
    "BarRow",
    "INVALID_COMBINATIONS_REASONS",
    "ReplayBarBatch",
    "ReplayConfig",
    "ReplayEngine",
    "STRATEGY_BUILDERS",
    "UNDERLYING_MAP",
    "VALID_COMBINATIONS",
    "bar_to_candle",
    "bar_to_indicators",
    "build_mock_instrument_meta",
    "build_nifty_spread_instrument_meta",
    "load_bars_from_postgres",
    "run_single_replay",
]
