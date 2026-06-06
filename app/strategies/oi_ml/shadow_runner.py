"""Standalone shadow runner for the intraday OI/ML CE seller.

This module is intended for a sidecar/container deployment. It never builds
``OrderRequest`` objects and never calls the strategy bridge. Its only broker
interaction is optional read-only market quote fetching for OI snapshots.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
import json
import logging
import os
from pathlib import Path
import time as time_module
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from app.data.option_chain_repository import OptionChainRepository
from app.data.postgres import connect_with_retry, get_control_plane_dsn
from app.risk.option_sell_guard import OptionSellGuardConfig
from app.strategies.oi_ml.decision import OiMlCeDecisionEngine, OiMlDecisionConfig, OiMlEntryAction
from app.strategies.oi_ml.greek_risk import OiMlGreekRiskConfig
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
    snapshot_start_time: time = time(9, 15)
    snapshot_end_time: time = time(15, 30)
    capture_snapshot: bool = True
    scorer_mode: str = "missing"
    allow_constant_scorer: bool = False
    constant_probability: float = 0.0
    constant_mae_premium: float = 0.0
    lightgbm_classifier_path: str | None = None
    lightgbm_feature_names_path: str | None = None
    lightgbm_mae_model_path: str | None = None
    lightgbm_default_mae_premium: float = 0.0
    model_validation_report_path: str | None = None
    require_model_validation_report: bool = True
    lots: int = 1
    lot_size: int = 65
    spread_width_points: int = 200
    max_spread_loss_rupees: float = 5000.0
    max_open_spreads: int = 1
    allow_naked: bool = False
    validation_gate_enabled: bool = True
    validation_max_age_seconds: int = 180
    virtual_flat_time: time = time(15, 20)
    virtual_lifecycle_end_time: time = time(15, 45)
    greek_risk_config: OiMlGreekRiskConfig = field(default_factory=OiMlGreekRiskConfig)
    tenant_id: str | None = None
    broker_account_id: str | None = None


@dataclass(frozen=True)
class OiMlShadowRunResult:
    decision_action: str
    reason: str
    snapshot_stored_rows: int = 0
    intent_id: str | None = None
    shadow_record_id: int | None = None
    lifecycle_updates: int = 0


@dataclass(frozen=True)
class _ValidationGateResult:
    allowed: bool
    reason: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


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
        snapshot_start_time=_parse_time(
            source.get("OI_ML_SHADOW_SNAPSHOT_START_TIME")
            or source.get("OI_SNAPSHOTTER_START_TIME"),
            time(9, 15),
        ),
        snapshot_end_time=_parse_time(
            source.get("OI_ML_SHADOW_SNAPSHOT_END_TIME")
            or source.get("OI_SNAPSHOTTER_END_TIME"),
            time(15, 30),
        ),
        capture_snapshot=_bool(source.get("OI_ML_SHADOW_CAPTURE_SNAPSHOT"), default=True),
        scorer_mode=str(source.get("OI_ML_SHADOW_SCORER") or "missing").strip().lower(),
        allow_constant_scorer=_bool(
            source.get("OI_ML_SHADOW_ALLOW_CONSTANT_SCORER")
            or source.get("OI_ML_SHADOW_ALLOW_SMOKE_SCORER"),
            default=False,
        ),
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
        model_validation_report_path=_optional_str(
            source.get("OI_ML_SHADOW_MODEL_VALIDATION_REPORT_PATH")
            or source.get("OI_ML_MODEL_VALIDATION_REPORT_PATH")
        ),
        require_model_validation_report=_bool(
            source.get("OI_ML_SHADOW_REQUIRE_MODEL_VALIDATION_REPORT"),
            default=True,
        ),
        lots=_int(source.get("OI_ML_SHADOW_LOTS"), 1, minimum=1),
        lot_size=_int(source.get("OI_ML_SHADOW_LOT_SIZE"), 65, minimum=1),
        spread_width_points=_int(source.get("OI_ML_SHADOW_SPREAD_WIDTH_POINTS"), 200, minimum=1),
        max_spread_loss_rupees=_float(
            source.get("OI_ML_SHADOW_MAX_SPREAD_LOSS_RUPEES"),
            5000.0,
        ),
        max_open_spreads=_int(source.get("OI_ML_SHADOW_MAX_OPEN_SPREADS"), 1, minimum=1),
        allow_naked=_bool(source.get("OI_ML_SHADOW_ALLOW_NAKED"), default=False),
        validation_gate_enabled=_bool(
            source.get("OI_ML_SHADOW_VALIDATION_GATE_ENABLED"),
            default=True,
        ),
        validation_max_age_seconds=_int(
            source.get("OI_ML_SHADOW_VALIDATION_MAX_AGE_SECONDS"),
            180,
            minimum=30,
        ),
        virtual_flat_time=_parse_time(
            source.get("OI_ML_SHADOW_VIRTUAL_FLAT_TIME"),
            time(15, 20),
        ),
        virtual_lifecycle_end_time=_parse_time(
            source.get("OI_ML_SHADOW_VIRTUAL_LIFECYCLE_END_TIME"),
            time(15, 45),
        ),
        greek_risk_config=_load_greek_risk_config(source),
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
    inside_entry_window = _within_window(current, config.start_time, config.end_time)
    inside_snapshot_window = _within_window(
        current,
        config.snapshot_start_time,
        config.snapshot_end_time,
    )
    inside_lifecycle_window = _within_window(
        current,
        config.virtual_flat_time,
        config.virtual_lifecycle_end_time,
    )
    if (
        config.market_window_only
        and not inside_entry_window
        and not inside_snapshot_window
        and not inside_lifecycle_window
    ):
        return OiMlShadowRunResult(decision_action="NO_TRADE", reason="outside_shadow_window")

    snapshot_stored = 0
    lifecycle_updates = 0
    dsn = get_control_plane_dsn()
    with connect_with_retry(dsn, autocommit=False) as conn:
        if config.capture_snapshot and (not config.market_window_only or inside_snapshot_window):
            snapshot_stored = _capture_snapshot(config)
        store = PostgresOiMlShadowLifecycleStore(conn)
        if _time_at_or_after(current, config.virtual_flat_time):
            lifecycle_updates += store.flatten_due_virtual_positions(
                now=current,
                provider=config.provider,
                underlying=config.underlying,
                expiry=config.expiry,
                tenant_id=config.tenant_id,
                broker_account_id=config.broker_account_id,
                exit_reason="eod_virtual_flatten",
            )
            if lifecycle_updates:
                conn.commit()
        if config.market_window_only and not inside_entry_window:
            return OiMlShadowRunResult(
                decision_action="NO_TRADE",
                reason="outside_entry_window",
                snapshot_stored_rows=snapshot_stored,
                lifecycle_updates=lifecycle_updates,
            )

        repository = OptionChainRepository(conn)
        open_spreads = store.count_open_virtual_spreads(
            now=current,
            underlying=config.underlying,
            expiry=config.expiry,
            tenant_id=config.tenant_id,
            broker_account_id=config.broker_account_id,
        )
        if open_spreads >= int(config.max_open_spreads):
            return OiMlShadowRunResult(
                decision_action="NO_TRADE",
                reason="virtual_open_spread_limit_reached",
                snapshot_stored_rows=snapshot_stored,
                lifecycle_updates=lifecycle_updates,
            )

        validation_gate = _latest_validation_gate(conn, config=config, now=current)
        if not validation_gate.allowed:
            return OiMlShadowRunResult(
                decision_action="NO_TRADE",
                reason=f"validation_gate_failed:{validation_gate.reason}",
                snapshot_stored_rows=snapshot_stored,
                lifecycle_updates=lifecycle_updates,
            )

        scorer = _build_scorer(config)
        decision_engine = OiMlCeDecisionEngine(
            repository,
            scorer,
            config=OiMlDecisionConfig(
                provider=config.provider,
                lot_size=config.lot_size,
                spread_width_points=float(config.spread_width_points),
                allow_naked=config.allow_naked,
                guard_config=OptionSellGuardConfig(
                    allow_naked=config.allow_naked,
                    max_spread_loss_rupees=float(config.max_spread_loss_rupees),
                ),
                greek_risk_config=config.greek_risk_config,
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
                lifecycle_updates=lifecycle_updates,
            )

        intent_result = build_order_intent_from_candidate(
            decision.selected,
            created_at=current,
            config=OiMlOrderIntentConfig(
                lots=config.lots,
                lot_size=config.lot_size,
                spread_width_points=config.spread_width_points,
                max_spread_loss_rupees=float(config.max_spread_loss_rupees),
            ),
        )
        if not intent_result.ok or intent_result.intent is None:
            reason = (intent_result.reasons or ("order_intent_rejected",))[0]
            return OiMlShadowRunResult(
                decision_action="NO_TRADE",
                reason=f"order_intent_rejected:{reason}",
                snapshot_stored_rows=snapshot_stored,
                lifecycle_updates=lifecycle_updates,
            )

        record = store.record_intent(
            intent_result.intent,
            decision_reason=decision.reason,
            tenant_id=config.tenant_id,
            broker_account_id=config.broker_account_id,
        )
        record = store.mark_virtual_fill(
            record,
            filled_at=current,
            entry_credit_points=float(intent_result.intent.estimated_net_credit_points),
        )
        lifecycle_updates += 1
        conn.commit()
        return OiMlShadowRunResult(
            decision_action=decision.action.value,
            reason=decision.reason,
            snapshot_stored_rows=snapshot_stored,
            intent_id=intent_result.intent.intent_id,
            shadow_record_id=record.record_id,
            lifecycle_updates=lifecycle_updates,
        )


def run_shadow_loop(config: OiMlShadowRunnerConfig) -> int:
    if not config.enabled:
        logger.info("oi_ml_shadow disabled; set OI_ML_SHADOW_ENABLED=true")
        return 0
    iterations = 0
    consecutive_failures = 0
    active_config = config
    resolved_for_day = datetime.now(IST).date()
    while True:
        iterations += 1
        try:
            active_config, resolved_for_day = _refresh_listed_expiry_for_day(
                active_config,
                resolved_for_day=resolved_for_day,
                today=datetime.now(IST).date(),
            )
            result = run_shadow_once(active_config)
            consecutive_failures = 0
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
            consecutive_failures += 1
            reason = _classify_shadow_failure(exc)
            logger.warning(
                "oi_ml_shadow_ingestion_degraded iteration=%d reason=%s consecutive_failures=%d",
                iterations,
                reason,
                consecutive_failures,
            )
            if consecutive_failures == 1 or consecutive_failures % 10 == 0:
                logger.debug("oi_ml_shadow failure detail", exc_info=True)

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


def _refresh_listed_expiry_for_day(
    config: OiMlShadowRunnerConfig,
    *,
    resolved_for_day: date,
    today: date,
) -> tuple[OiMlShadowRunnerConfig, date]:
    if today == resolved_for_day:
        return config, resolved_for_day
    if config.expiry_is_explicit or str(config.provider or "").strip().lower() != "angel":
        return config, today
    refreshed = _resolve_listed_expiry(config, today=today)
    if refreshed.expiry != config.expiry:
        logger.info(
            "oi_ml_shadow refreshed listed expiry previous=%s current=%s trading_day=%s",
            config.expiry.isoformat(),
            refreshed.expiry.isoformat(),
            today.isoformat(),
        )
    return refreshed, today


def _load_scrip_master() -> object:
    from app.core.instruments_resolver import load_scrip_master

    return load_scrip_master()


def _build_scorer(config: OiMlShadowRunnerConfig) -> OiMlScorer:
    if config.scorer_mode == "constant":
        if not config.allow_constant_scorer:
            raise RuntimeError(
                "OI/ML constant scorer is smoke-only; set "
                "OI_ML_SHADOW_ALLOW_CONSTANT_SCORER=true only for explicit "
                "connectivity tests, or use OI_ML_SHADOW_SCORER=lightgbm "
                "with validated artifacts"
            )
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
        _validate_model_report(config)
        return LightGbmOiMlScorer.from_artifacts(
            classifier_path=config.lightgbm_classifier_path,
            feature_names_path=config.lightgbm_feature_names_path,
            mae_model_path=config.lightgbm_mae_model_path,
            default_mae_premium=config.lightgbm_default_mae_premium,
        )
    return MissingOiMlScorer()


def _validate_model_report(config: OiMlShadowRunnerConfig) -> None:
    if not config.require_model_validation_report:
        return
    if not config.model_validation_report_path:
        raise RuntimeError(
            "OI/ML LightGBM scorer requires a passed validation report: "
            "set OI_ML_SHADOW_MODEL_VALIDATION_REPORT_PATH"
        )
    path = Path(config.model_validation_report_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"OI/ML model validation report not found: {path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"OI/ML model validation report is not valid JSON: {path}"
        ) from exc

    report = payload.get("promotion") if isinstance(payload, Mapping) else None
    if not isinstance(report, Mapping):
        report = payload if isinstance(payload, Mapping) else {}
    if report.get("passed") is not True:
        reasons = report.get("reasons") if isinstance(report, Mapping) else None
        raise RuntimeError(
            "OI/ML model validation report did not pass promotion gates"
            + (f": {reasons}" if reasons else "")
        )


def _latest_validation_gate(
    conn: Any,
    *,
    config: OiMlShadowRunnerConfig,
    now: datetime,
) -> _ValidationGateResult:
    if not config.validation_gate_enabled:
        return _ValidationGateResult(allowed=True)
    params = {
        "underlying": str(config.underlying or "").strip().upper(),
        "expiry": config.expiry,
        "validation_min_ts": now.astimezone(IST) - timedelta(
            seconds=max(30, int(config.validation_max_age_seconds))
        ),
        "validation_max_ts": now.astimezone(IST),
    }
    sql = """
        SELECT validation_ts, status, severity, primary_quote_count, reference_quote_count
        FROM public.option_chain_validation_reports
        WHERE underlying = %(underlying)s
          AND expiry = %(expiry)s
          AND validation_ts >= %(validation_min_ts)s
          AND validation_ts <= %(validation_max_ts)s
        ORDER BY validation_ts DESC
        LIMIT 1
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        description = getattr(cur, "description", None)
    if row is None:
        return _ValidationGateResult(
            allowed=False,
            reason="missing_validation_report",
        )
    latest = _row_mapping(row, description)
    status = str(latest.get("status") or "").strip().upper()
    severity = str(latest.get("severity") or "").strip().upper()
    if status == "ERROR" or severity == "ERROR":
        return _ValidationGateResult(
            allowed=False,
            reason="latest_validation_error",
            metadata=latest,
        )
    return _ValidationGateResult(allowed=True, metadata=latest)


def _load_greek_risk_config(source: Mapping[str, str]) -> OiMlGreekRiskConfig:
    return OiMlGreekRiskConfig(
        enabled=_bool(source.get("OI_ML_SHADOW_GREEK_RISK_ENABLED"), default=True),
        require_greeks=_bool(source.get("OI_ML_SHADOW_REQUIRE_GREEKS"), default=True),
        require_oi_wall=_bool(source.get("OI_ML_SHADOW_REQUIRE_OI_WALL"), default=True),
        target_abs_delta=_float(source.get("OI_ML_SHADOW_TARGET_ABS_DELTA"), 0.20),
        min_abs_delta=_float(source.get("OI_ML_SHADOW_MIN_ABS_DELTA"), 0.05),
        max_abs_delta=_float(source.get("OI_ML_SHADOW_MAX_ABS_DELTA"), 0.35),
        max_abs_gamma=_float(source.get("OI_ML_SHADOW_MAX_ABS_GAMMA"), 0.0030),
        max_abs_vega=_optional_float(source.get("OI_ML_SHADOW_MAX_ABS_VEGA")),
        force_spread_abs_gamma=_float(
            source.get("OI_ML_SHADOW_FORCE_SPREAD_ABS_GAMMA"),
            0.0015,
        ),
        force_spread_abs_vega=_float(
            source.get("OI_ML_SHADOW_FORCE_SPREAD_ABS_VEGA"),
            8.0,
        ),
        size_down_abs_delta=_float(source.get("OI_ML_SHADOW_SIZE_DOWN_ABS_DELTA"), 0.25),
        size_down_abs_gamma=_float(source.get("OI_ML_SHADOW_SIZE_DOWN_ABS_GAMMA"), 0.0012),
        size_down_abs_vega=_float(source.get("OI_ML_SHADOW_SIZE_DOWN_ABS_VEGA"), 7.0),
        size_down_lot_multiplier=_float(
            source.get("OI_ML_SHADOW_SIZE_DOWN_LOT_MULTIPLIER"),
            0.50,
        ),
        exit_abs_delta=_float(source.get("OI_ML_SHADOW_EXIT_ABS_DELTA"), 0.45),
        exit_abs_gamma=_float(source.get("OI_ML_SHADOW_EXIT_ABS_GAMMA"), 0.0040),
        exit_iv_expansion_pct=_float(
            source.get("OI_ML_SHADOW_EXIT_IV_EXPANSION_PCT"),
            0.25,
        ),
        tighten_abs_delta=_float(source.get("OI_ML_SHADOW_TIGHTEN_ABS_DELTA"), 0.30),
        tighten_abs_gamma=_float(source.get("OI_ML_SHADOW_TIGHTEN_ABS_GAMMA"), 0.0020),
        tighten_iv_expansion_pct=_float(
            source.get("OI_ML_SHADOW_TIGHTEN_IV_EXPANSION_PCT"),
            0.15,
        ),
        tightened_stop_loss_mult_credit=_float(
            source.get("OI_ML_SHADOW_TIGHTENED_STOP_LOSS_MULT_CREDIT"),
            1.25,
        ),
    )


def _next_weekly_expiry(today: date | None = None) -> date:
    base = today or datetime.now(IST).date()
    days_until_thursday = (3 - base.weekday()) % 7
    if days_until_thursday == 0:
        return base
    return base + timedelta(days=days_until_thursday)


def _within_window(value: datetime, start: time, end: time) -> bool:
    now_time = value.astimezone(IST).time()
    return start <= now_time <= end


def _time_at_or_after(value: datetime, threshold: time) -> bool:
    return value.astimezone(IST).time() >= threshold


def _row_mapping(row: Any, description: Any) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    columns = [desc[0] for desc in description or []]
    return dict(zip(columns, row))


def _classify_shadow_failure(exc: Exception) -> str:
    if isinstance(exc, TimeoutError):
        return "provider_timeout"
    message = str(exc or "").lower()
    if "timeout" in message or "timed out" in message:
        return "provider_timeout"
    if "login" in message or "auth" in message:
        return "provider_login_failed"
    return type(exc).__name__


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


def _optional_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
