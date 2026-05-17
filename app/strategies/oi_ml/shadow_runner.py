"""Standalone shadow runner for the intraday OI/ML CE seller.

This module is intended for a sidecar/container deployment. It never builds
``OrderRequest`` objects and never calls the strategy bridge. Its only broker
interaction is optional read-only market quote fetching for OI snapshots.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
import logging
import os
import time as time_module
from typing import Mapping
from zoneinfo import ZoneInfo

from app.data.option_chain_repository import OptionChainRepository
from app.data.postgres import connect_with_retry, get_control_plane_dsn
from app.risk.option_sell_guard import OptionSellGuardConfig
from app.strategies.oi_ml.decision import OiMlCeDecisionEngine, OiMlDecisionConfig, OiMlEntryAction
from app.strategies.oi_ml.order_intents import OiMlOrderIntentConfig, build_order_intent_from_candidate
from app.strategies.oi_ml.scoring import (
    ConstantOiMlScorer,
    LightGbmOiMlScorer,
    MissingOiMlScorer,
    OiMlScorer,
)
from app.strategies.oi_ml.shadow_lifecycle import PostgresOiMlShadowLifecycleStore


logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class OiMlShadowRunnerConfig:
    enabled: bool
    underlying: str = "NIFTY"
    expiry: date = field(default_factory=lambda: _next_weekly_expiry())
    expiry_is_explicit: bool = False
    provider: str | None = "angel"
    cadence_seconds: int = 60
    max_iterations: int | None = None
    market_window_only: bool = True
    start_time: time = time(9, 45)
    end_time: time = time(14, 30)
    capture_snapshot: bool = True
    scorer_mode: str = "missing"
    constant_probability: float = 0.0
    constant_mae_premium: float = 0.0
    lightgbm_classifier_path: str | None = None
    lightgbm_feature_names_path: str | None = None
    lightgbm_mae_model_path: str | None = None
    lightgbm_default_mae_premium: float = 0.0
    lots: int = 1
    lot_size: int = 65
    spread_width_points: int = 200
    allow_naked: bool = False
    tenant_id: str | None = None
    broker_account_id: str | None = None


@dataclass(frozen=True)
class OiMlShadowRunResult:
    decision_action: str
    reason: str
    snapshot_stored_rows: int = 0
    intent_id: str | None = None
    shadow_record_id: int | None = None


def load_shadow_runner_config(
    *,
    env: Mapping[str, str] | None = None,
) -> OiMlShadowRunnerConfig:
    source = env or os.environ
    expiry_value = source.get("OI_ML_SHADOW_EXPIRY") or source.get("OI_SNAPSHOTTER_EXPIRY")
    parsed_expiry = _parse_date(expiry_value)
    return OiMlShadowRunnerConfig(
        enabled=_bool(source.get("OI_ML_SHADOW_ENABLED"), default=False),
        underlying=str(source.get("OI_ML_SHADOW_UNDERLYING") or "NIFTY").strip().upper(),
        expiry=parsed_expiry or _next_weekly_expiry(),
        expiry_is_explicit=parsed_expiry is not None,
        provider=_optional_str(source.get("OI_ML_SHADOW_PROVIDER") or source.get("OI_SNAPSHOTTER_PROVIDER") or "angel"),
        cadence_seconds=_int(source.get("OI_ML_SHADOW_CADENCE_SECONDS"), 60, minimum=5),
        max_iterations=_optional_int(source.get("OI_ML_SHADOW_MAX_ITERATIONS"), minimum=1),
        market_window_only=_bool(source.get("OI_ML_SHADOW_MARKET_WINDOW_ONLY"), default=True),
        start_time=_parse_time(source.get("OI_ML_SHADOW_START_TIME"), time(9, 45)),
        end_time=_parse_time(source.get("OI_ML_SHADOW_END_TIME"), time(14, 30)),
        capture_snapshot=_bool(source.get("OI_ML_SHADOW_CAPTURE_SNAPSHOT"), default=True),
        scorer_mode=str(source.get("OI_ML_SHADOW_SCORER") or "missing").strip().lower(),
        constant_probability=_float(source.get("OI_ML_SHADOW_CONSTANT_PROBABILITY"), 0.0),
        constant_mae_premium=_float(source.get("OI_ML_SHADOW_CONSTANT_MAE_PREMIUM"), 0.0),
        lightgbm_classifier_path=_optional_str(
            source.get("OI_ML_SHADOW_LIGHTGBM_CLASSIFIER_PATH")
            or source.get("OI_ML_CLASSIFIER_MODEL_PATH")
        ),
        lightgbm_feature_names_path=_optional_str(
            source.get("OI_ML_SHADOW_LIGHTGBM_FEATURE_NAMES_PATH")
            or source.get("OI_ML_FEATURE_NAMES_PATH")
        ),
        lightgbm_mae_model_path=_optional_str(
            source.get("OI_ML_SHADOW_LIGHTGBM_MAE_MODEL_PATH")
            or source.get("OI_ML_MAE_MODEL_PATH")
        ),
        lightgbm_default_mae_premium=_float(
            source.get("OI_ML_SHADOW_LIGHTGBM_DEFAULT_MAE_PREMIUM")
            or source.get("OI_ML_DEFAULT_MAE_PREMIUM"),
            0.0,
        ),
        lots=_int(source.get("OI_ML_SHADOW_LOTS"), 1, minimum=1),
        lot_size=_int(source.get("OI_ML_SHADOW_LOT_SIZE"), 65, minimum=1),
        spread_width_points=_int(source.get("OI_ML_SHADOW_SPREAD_WIDTH_POINTS"), 200, minimum=1),
        allow_naked=_bool(source.get("OI_ML_SHADOW_ALLOW_NAKED"), default=False),
        tenant_id=_optional_str(source.get("HUB_DEFAULT_TENANT_ID")),
        broker_account_id=_optional_str(source.get("HUB_DEFAULT_BROKER_ACCOUNT_ID")),
    )


def run_shadow_once(
    config: OiMlShadowRunnerConfig,
    *,
    now: datetime | None = None,
) -> OiMlShadowRunResult:
    if not config.enabled:
        return OiMlShadowRunResult(decision_action="DISABLED", reason="shadow_runner_disabled")

    current = (now or datetime.now(IST)).astimezone(IST)
    if config.market_window_only and not _within_window(current, config.start_time, config.end_time):
        return OiMlShadowRunResult(decision_action="NO_TRADE", reason="outside_shadow_window")

    snapshot_stored = 0
    dsn = get_control_plane_dsn()
    with connect_with_retry(dsn, autocommit=False) as conn:
        if config.capture_snapshot:
            snapshot_stored = _capture_snapshot(config)

        repository = OptionChainRepository(conn)
        scorer = _build_scorer(config)
        decision_engine = OiMlCeDecisionEngine(
            repository,
            scorer,
            config=OiMlDecisionConfig(
                provider=config.provider,
                lot_size=config.lot_size,
                spread_width_points=float(config.spread_width_points),
                allow_naked=config.allow_naked,
                guard_config=OptionSellGuardConfig(allow_naked=config.allow_naked),
            ),
        )
        decision = decision_engine.evaluate_entry(
            underlying=config.underlying,
            expiry=config.expiry,
            decision_ts=current,
            tenant_id=config.tenant_id,
            account_id=config.broker_account_id,
        )
        if decision.action is not OiMlEntryAction.STAGE_ENTRY or decision.selected is None:
            return OiMlShadowRunResult(
                decision_action=decision.action.value,
                reason=decision.reason,
                snapshot_stored_rows=snapshot_stored,
            )

        intent_result = build_order_intent_from_candidate(
            decision.selected,
            created_at=current,
            config=OiMlOrderIntentConfig(
                lots=config.lots,
                lot_size=config.lot_size,
                spread_width_points=config.spread_width_points,
            ),
        )
        if not intent_result.ok or intent_result.intent is None:
            reason = (intent_result.reasons or ("order_intent_rejected",))[0]
            return OiMlShadowRunResult(
                decision_action="NO_TRADE",
                reason=f"order_intent_rejected:{reason}",
                snapshot_stored_rows=snapshot_stored,
            )

        store = PostgresOiMlShadowLifecycleStore(conn)
        record = store.record_intent(
            intent_result.intent,
            decision_reason=decision.reason,
            tenant_id=config.tenant_id,
            broker_account_id=config.broker_account_id,
        )
        conn.commit()
        return OiMlShadowRunResult(
            decision_action=decision.action.value,
            reason=decision.reason,
            snapshot_stored_rows=snapshot_stored,
            intent_id=intent_result.intent.intent_id,
            shadow_record_id=record.record_id,
        )


def run_shadow_loop(config: OiMlShadowRunnerConfig) -> int:
    if not config.enabled:
        logger.info("oi_ml_shadow disabled; set OI_ML_SHADOW_ENABLED=true")
        return 0
    iterations = 0
    while True:
        iterations += 1
        try:
            result = run_shadow_once(config)
            logger.info(
                "oi_ml_shadow iteration=%d action=%s reason=%s snapshot_rows=%d intent_id=%s record_id=%s",
                iterations,
                result.decision_action,
                result.reason,
                result.snapshot_stored_rows,
                result.intent_id,
                result.shadow_record_id,
            )
        except Exception as exc:
            logger.exception("oi_ml_shadow iteration=%d failed: %s", iterations, exc)

        if config.max_iterations is not None and iterations >= config.max_iterations:
            return 0
        time_module.sleep(max(5, int(config.cadence_seconds)))


def main() -> int:
    logging.basicConfig(
        level=str(os.getenv("LOG_LEVEL", "INFO")).upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    config = _resolve_listed_expiry(load_shadow_runner_config())
    logger.info(
        "oi_ml_shadow starting enabled=%s underlying=%s expiry=%s provider=%s scorer=%s capture_snapshot=%s",
        config.enabled,
        config.underlying,
        config.expiry.isoformat(),
        config.provider,
        config.scorer_mode,
        config.capture_snapshot,
    )
    return run_shadow_loop(config)


def _capture_snapshot(config: OiMlShadowRunnerConfig) -> int:
    from app.data.oi_snapshotter_runtime import load_runtime_config, run_runtime

    runtime_config = load_runtime_config(
        enabled=True,
        provider=config.provider,
        underlying=config.underlying,
        expiry=config.expiry,
        once=True,
    )
    results = run_runtime(runtime_config)
    return sum(result.stored_count for result in results)


def _resolve_listed_expiry(
    config: OiMlShadowRunnerConfig,
    *,
    today: date | None = None,
) -> OiMlShadowRunnerConfig:
    if str(config.provider or "").strip().lower() != "angel":
        return config
    scrip_master = _load_scrip_master()
    from app.data.angel_option_chain_provider import (
        listed_option_expiries,
        next_listed_option_expiry,
    )

    current_day = today or datetime.now(IST).date()
    if config.expiry_is_explicit:
        expiries = set(
            listed_option_expiries(
                scrip_master,
                underlying=config.underlying,
                on_or_after=current_day,
            )
        )
        if config.expiry not in expiries:
            raise RuntimeError(
                "configured OI/ML shadow expiry is not listed by scrip master: "
                f"underlying={config.underlying} expiry={config.expiry.isoformat()}"
            )
        return config

    resolved = next_listed_option_expiry(
        scrip_master,
        underlying=config.underlying,
        on_or_after=current_day,
    )
    if resolved is None:
        raise RuntimeError(
            f"no listed OI/ML option expiry found for underlying={config.underlying}"
        )
    if resolved != config.expiry:
        logger.info(
            "oi_ml_shadow resolved listed expiry underlying=%s calendar_default=%s listed=%s",
            config.underlying,
            config.expiry.isoformat(),
            resolved.isoformat(),
        )
    return replace(config, expiry=resolved)


def _load_scrip_master() -> object:
    from app.core.instruments_resolver import load_scrip_master

    return load_scrip_master()


def _build_scorer(config: OiMlShadowRunnerConfig) -> OiMlScorer:
    if config.scorer_mode == "constant":
        return ConstantOiMlScorer(
            probability=config.constant_probability,
            predicted_mae_premium=config.constant_mae_premium,
        )
    if config.scorer_mode in {"lightgbm", "lgbm"}:
        missing = []
        if not config.lightgbm_classifier_path:
            missing.append("OI_ML_SHADOW_LIGHTGBM_CLASSIFIER_PATH")
        if not config.lightgbm_feature_names_path:
            missing.append("OI_ML_SHADOW_LIGHTGBM_FEATURE_NAMES_PATH")
        if missing:
            raise RuntimeError(
                "OI/ML LightGBM scorer missing required artifact env vars: "
                + ", ".join(missing)
            )
        return LightGbmOiMlScorer.from_artifacts(
            classifier_path=config.lightgbm_classifier_path,
            feature_names_path=config.lightgbm_feature_names_path,
            mae_model_path=config.lightgbm_mae_model_path,
            default_mae_premium=config.lightgbm_default_mae_premium,
        )
    return MissingOiMlScorer()


def _next_weekly_expiry(today: date | None = None) -> date:
    base = today or datetime.now(IST).date()
    days_until_thursday = (3 - base.weekday()) % 7
    if days_until_thursday == 0:
        return base
    return base + timedelta(days=days_until_thursday)


def _within_window(value: datetime, start: time, end: time) -> bool:
    now_time = value.astimezone(IST).time()
    return start <= now_time <= end


def _parse_date(value: object) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        return None


def _parse_time(value: object, default: time) -> time:
    if value in (None, ""):
        return default
    try:
        hour, minute = (int(part) for part in str(value).split(":", maxsplit=1))
        return time(hour, minute)
    except Exception:
        return default


def _bool(value: object, *, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _int(value: object, default: int, *, minimum: int) -> int:
    try:
        parsed = int(value) if value not in (None, "") else int(default)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(int(minimum), parsed)


def _optional_int(value: object, *, minimum: int) -> int | None:
    if value in (None, ""):
        return None
    return _int(value, int(minimum), minimum=minimum)


def _float(value: object, default: float) -> float:
    try:
        return float(value) if value not in (None, "") else float(default)
    except (TypeError, ValueError):
        return float(default)


def _optional_str(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "OiMlShadowRunResult",
    "OiMlShadowRunnerConfig",
    "load_shadow_runner_config",
    "run_shadow_loop",
    "run_shadow_once",
]
