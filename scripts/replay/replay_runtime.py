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
from app.orders.replay_context import isolated_replay_order_sink

from scripts.replay.execution_models import (
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
from scripts.replay.schema import build_select_list, inspect_table_schema, normalize_table_name

logger = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

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
    l: float
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

    select_list = build_select_list(schema, _REQUIRED_COLUMNS + _OPTIONAL_COLUMNS)
    query = f"""
        SELECT {select_list}
        FROM {normalized_table}
        WHERE {" AND ".join(conditions)}
        ORDER BY ts_start ASC
    """

    rows: List[BarRow] = []
    null_counts = {column: 0 for column in _OPTIONAL_COLUMNS}
    fetch_size = max(1, int(chunk_size or 0))
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
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

    profile = {
        "label": label,
        "timeframe_seconds": int(timeframe_seconds) if timeframe_seconds is not None else None,
        "bars_loaded": len(rows),
        "timezone": "Asia/Kolkata",
        "source_table": normalized_table,
        "available_columns": list(schema.available_columns),
        "missing_optional_columns": list(schema.missing_optional_columns),
        "null_indicator_counts": {k: int(v) for k, v in sorted(null_counts.items()) if int(v) > 0},
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


STRATEGY_BUILDERS = {
    "ema20_strategy": build_ema20_strategy,
    "put_momentum_scalper": build_put_momentum_strategy,
    "exclusive_nifty_ce_buy": build_exclusive_ce_strategy,
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
        if hasattr(strategy, "_now_ist"):
            strategy._now_ist = lambda: self._clock.now_local(IST)
        if hasattr(strategy, "_now_utc"):
            strategy._now_utc = lambda: self._clock.now_utc()

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

    def _update_strategy_last_prices(
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
        if strategy is not None and hasattr(strategy, "last_price"):
            strategy.last_price.update(prices)
        return prices

    def _finalize_open_positions(
        self,
        *,
        last_bar: BarRow,
        label: str,
        reason: str,
    ) -> None:
        strategy = self._strategy
        if strategy is None:
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
        )
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
            "available_columns": [],
            "execution": cfg.execution.as_dict(),
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
            )
            bars = list(batch)
            profile = dict(getattr(batch, "replay_profile", {}) or {})
            db_bar_count_by_timeframe[int(tf)] = len(bars)
            merged_profile["bars_loaded_by_timeframe"][str(int(tf))] = len(bars)
            merged_profile["missing_optional_columns_by_timeframe"][str(int(tf))] = list(
                profile.get("missing_optional_columns") or []
            )
            merged_profile["null_indicator_counts_by_timeframe"][str(int(tf))] = dict(
                profile.get("null_indicator_counts") or {}
            )
            available_columns = set(merged_profile.get("available_columns") or [])
            available_columns.update(profile.get("available_columns") or [])
            merged_profile["available_columns"] = sorted(available_columns)
            self._bars_by_timeframe[int(tf)] = bars
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
                    self._finalize_open_positions(
                        last_bar=prev_bar,
                        label=label,
                        reason="REPLAY_SESSION_BOUNDARY",
                    )
                    self._price_book_state = {}
                    self._close_history = defaultdict(list)

                self._current_session_date = session_date
                indicators = self._build_indicator_payload(bar)
                self._dispatch_ticks_for_bar(bar=bar, indicators=indicators)

                self._clock._current_utc = bar.event_ts.astimezone(UTC)
                next_open_price, next_open_ts = self._future_open_for_bar(bar)
                price_map = self._update_strategy_last_prices(
                    underlying_price=float(bar.c),
                    atr=bar.atr,
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
                    )
                )
                try:
                    self._strategy.on_bar(label, bar.timeframe_seconds, bar_to_candle(bar), indicators)
                except Exception as exc:
                    logger.debug("on_bar exception at %s: %s", bar.event_ts, exc)

                self._close_history[int(bar.timeframe_seconds)].append(float(bar.c))
                prev_session_date = session_date
                prev_bar = bar

            self._finalize_open_positions(last_bar=all_bars[-1], label=label, reason="REPLAY_EOD")
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


def _compute_ema(values: Sequence[float], period: int) -> Optional[float]:
    period_int = max(2, int(period))
    if len(values) < period_int:
        return None
    k = 2.0 / (period_int + 1.0)
    ema = float(values[0])
    for value in values[1:]:
        ema = (float(value) * k) + (ema * (1.0 - k))
    return float(ema)


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
    "load_bars_from_postgres",
    "run_single_replay",
]
