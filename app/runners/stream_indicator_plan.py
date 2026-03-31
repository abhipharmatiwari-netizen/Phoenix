"""Planning helpers for the stream indicator engine footprint."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from app.strategies.naming import canonicalize_strategy_name

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IndicatorEnginePlan:
    ema_periods_by_timeframe: dict[int, list[int]]
    engine_timeframes: list[int]
    adx_period: int


def parse_timeframe_to_seconds(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    try:
        text = str(value).strip().lower()
        if text.endswith("m"):
            return int(float(text[:-1]) * 60)
        if text.endswith("s"):
            return int(float(text[:-1]))
        if text.endswith("h"):
            return int(float(text[:-1]) * 3600)
    except Exception:
        return None
    return None


def _normalize_strategy_entries(entries: Any) -> list[dict[str, Any]]:
    if isinstance(entries, list):
        return [entry for entry in entries if isinstance(entry, dict)]
    if isinstance(entries, dict):
        return [
            dict(cfg, name=name)
            for name, cfg in entries.items()
            if isinstance(cfg, dict)
        ]
    return []


def _register_ema_period(
    ema_periods_by_timeframe: dict[int, set[int]],
    timeframe_seconds: Any,
    period: Any,
) -> None:
    try:
        tf_int = int(timeframe_seconds)
        period_int = int(period)
    except (TypeError, ValueError):
        return
    if tf_int <= 0 or period_int <= 0:
        return
    ema_periods_by_timeframe.setdefault(tf_int, set()).add(period_int)


def build_indicator_engine_plan(strategy_cfg: dict[str, Any]) -> IndicatorEnginePlan:
    ema_periods_by_tf: dict[int, list[int]] = {}
    indicator_cfg = (
        strategy_cfg.get("indicators", {}) if isinstance(strategy_cfg, dict) else {}
    )
    tf_cfgs = (
        indicator_cfg.get("timeframes", {}) if isinstance(indicator_cfg, dict) else {}
    )
    configured_timeframes: set[int] = set()
    if isinstance(tf_cfgs, dict):
        for tf_key, tf_cfg in tf_cfgs.items():
            if not isinstance(tf_cfg, dict):
                continue
            tf_seconds = parse_timeframe_to_seconds(tf_key)
            if tf_seconds is None:
                continue
            configured_timeframes.add(tf_seconds)
            ema_cfg = tf_cfg.get("ema")
            if not isinstance(ema_cfg, dict):
                continue
            periods: list[int] = []
            for period in ema_cfg.get("periods", []) or []:
                try:
                    period_val = int(period)
                except (TypeError, ValueError):
                    logger.warning(
                        "Invalid EMA period %s for timeframe %s",
                        period,
                        tf_key,
                    )
                    continue
                if period_val > 0:
                    periods.append(period_val)
            if periods:
                ema_periods_by_tf[tf_seconds] = sorted(set(periods))

    ema_5m_periods_env = os.getenv("EMA_5M_PERIODS", "20")
    ema_5m_periods: set[int] = set()
    for part in ema_5m_periods_env.split(","):
        token = part.strip()
        if not token:
            continue
        try:
            ema_5m_periods.add(int(token))
        except ValueError:
            logger.warning(
                "Invalid EMA_5M_PERIODS=%s, falling back to 20",
                ema_5m_periods_env,
            )
            ema_5m_periods = {20}
            break

    for section in (strategy_cfg or {}).values():
        if not isinstance(section, dict):
            continue
        period_val = section.get("ema_period")
        if period_val is None:
            continue
        try:
            ema_5m_periods.add(int(period_val))
        except (TypeError, ValueError):
            logger.warning("Invalid ema_period in strategy config: %s", period_val)

    if not ema_5m_periods:
        ema_5m_periods = {20}
    ema_periods_by_tf.setdefault(300, sorted(ema_5m_periods))

    strategy_timeframes: set[int] = set()
    dynamic_ema_periods_by_tf: dict[int, set[int]] = {}
    adx_period_candidates: set[int] = set()
    for entry in _normalize_strategy_entries(strategy_cfg.get("strategies", [])):
        params = entry.get("params", {}) if isinstance(entry.get("params"), dict) else {}
        canonical_name = canonicalize_strategy_name(
            entry.get("name"),
            source="stream_indicator_plan",
            warn_alias=False,
        )
        signal_tf_seconds = 300
        tf_val = params.get("timeframe_seconds") or params.get("signal_timeframe")
        if tf_val is not None:
            try:
                tf_sec = int(tf_val)
            except (TypeError, ValueError):
                tf_sec = None
            if tf_sec is not None and tf_sec > 0:
                signal_tf_seconds = tf_sec
                strategy_timeframes.add(tf_sec)

        period_val = params.get("ema_period")
        if period_val is not None:
            try:
                period_int = int(period_val)
            except (TypeError, ValueError):
                logger.warning("Invalid ema_period in strategy config: %s", period_val)
            else:
                if period_int > 0:
                    _register_ema_period(
                        dynamic_ema_periods_by_tf,
                        signal_tf_seconds,
                        period_int,
                    )

        dynamic_cfg = params.get("dynamic_policy")
        if isinstance(dynamic_cfg, dict):
            profiles = dynamic_cfg.get("profiles", {})
            if isinstance(profiles, dict):
                for profile_name, profile_cfg in profiles.items():
                    if not isinstance(profile_cfg, dict):
                        continue
                    profile_ema = profile_cfg.get("ema_period")
                    if profile_ema is None:
                        continue
                    try:
                        profile_ema_int = int(profile_ema)
                    except (TypeError, ValueError):
                        logger.warning(
                            "Invalid dynamic profile ema_period in strategy config: strategy=%s profile=%s value=%s",
                            entry.get("name"),
                            profile_name,
                            profile_ema,
                        )
                        continue
                    if profile_ema_int > 0:
                        _register_ema_period(
                            dynamic_ema_periods_by_tf,
                            signal_tf_seconds,
                            profile_ema_int,
                        )

        for key in ("orb_bar_minutes", "bar_minutes"):
            value = params.get(key)
            if value is None:
                continue
            try:
                tf_sec = int(value) * 60
            except (TypeError, ValueError):
                continue
            if tf_sec > 0:
                strategy_timeframes.add(tf_sec)

        tf5_seconds = None
        tf15_seconds = None
        tf5 = params.get("timeframe_seconds_5m")
        if tf5 is not None:
            try:
                tf5_seconds = int(tf5)
            except (TypeError, ValueError):
                tf5_seconds = None
        tf15 = params.get("timeframe_seconds_15m")
        if tf15 is not None:
            try:
                tf15_seconds = int(tf15)
            except (TypeError, ValueError):
                tf15_seconds = None
        for tf_val in (tf5_seconds, tf15_seconds):
            if tf_val is not None and tf_val > 0:
                strategy_timeframes.add(tf_val)

        if tf5_seconds is not None:
            for key in ("ema_period_5m", "ema_5m_fast", "ema_5m_slow"):
                _register_ema_period(dynamic_ema_periods_by_tf, tf5_seconds, params.get(key))
        if tf15_seconds is not None:
            for key in ("ema_period_15m", "ema_15m_period"):
                _register_ema_period(dynamic_ema_periods_by_tf, tf15_seconds, params.get(key))

        if canonical_name == "put_momentum_scalper":
            base_tf5 = tf5_seconds or 300
            base_tf15 = tf15_seconds or 900
            _register_ema_period(dynamic_ema_periods_by_tf, base_tf5, 20)
            _register_ema_period(dynamic_ema_periods_by_tf, base_tf5, 50)
            _register_ema_period(dynamic_ema_periods_by_tf, base_tf15, 20)
        elif canonical_name == "put_reversion_writer":
            _register_ema_period(dynamic_ema_periods_by_tf, tf5_seconds or 300, 20)

        adx_period_val = params.get("adx_period")
        if adx_period_val is not None:
            try:
                parsed = int(adx_period_val)
            except (TypeError, ValueError):
                logger.warning("Invalid adx_period in strategy config: %s", adx_period_val)
            else:
                if parsed >= 2:
                    adx_period_candidates.add(parsed)
                else:
                    logger.warning("Invalid adx_period in strategy config: %s", adx_period_val)

    for timeframe_seconds, periods in dynamic_ema_periods_by_tf.items():
        merged = set(ema_periods_by_tf.get(timeframe_seconds, []))
        merged.update(periods)
        ema_periods_by_tf[timeframe_seconds] = sorted(merged)

    engine_adx_period = 14
    if adx_period_candidates:
        if len(adx_period_candidates) > 1:
            logger.warning(
                "Multiple EMA20 adx_period values configured (%s); using %d for shared indicator engine",
                sorted(adx_period_candidates),
                min(adx_period_candidates),
            )
        engine_adx_period = min(adx_period_candidates)

    engine_timeframes = sorted(
        set((60, 300, 900)) | configured_timeframes | strategy_timeframes
    )
    return IndicatorEnginePlan(
        ema_periods_by_timeframe=ema_periods_by_tf,
        engine_timeframes=engine_timeframes,
        adx_period=engine_adx_period,
    )


__all__ = [
    "IndicatorEnginePlan",
    "build_indicator_engine_plan",
    "parse_timeframe_to_seconds",
]
