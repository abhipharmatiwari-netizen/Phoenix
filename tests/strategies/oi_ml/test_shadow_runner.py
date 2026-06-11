from __future__ import annotations

from datetime import date, datetime
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.strategies.oi_ml import shadow_runner
from app.strategies.oi_ml.decision import OiMlEntryAction
from app.strategies.oi_ml.shadow_runner import (
    OiMlShadowRunnerConfig,
    load_shadow_runner_config,
    run_shadow_once,
)


IST = ZoneInfo("Asia/Kolkata")


def test_load_shadow_runner_config_defaults_to_fail_closed_missing_scorer():
    cfg = load_shadow_runner_config(
        env={
            "OI_ML_SHADOW_ENABLED": "true",
            "OI_ML_SHADOW_EXPIRY": "2026-05-21",
            "HUB_DEFAULT_TENANT_ID": "tenant-a",
            "HUB_DEFAULT_BROKER_ACCOUNT_ID": "acct-a",
        }
    )

    assert cfg.enabled is True
    assert cfg.expiry == date(2026, 5, 21)
    assert cfg.scorer_mode == "missing"
    assert cfg.tenant_id == "tenant-a"
    assert cfg.broker_account_id == "acct-a"


def test_load_shadow_runner_config_reads_lightgbm_artifacts():
    cfg = load_shadow_runner_config(
        env={
            "OI_ML_SHADOW_ENABLED": "true",
            "OI_ML_SHADOW_SCORER": "lightgbm",
            "OI_ML_SHADOW_LIGHTGBM_CLASSIFIER_PATH": "/models/classifier.txt",
            "OI_ML_SHADOW_LIGHTGBM_FEATURE_NAMES_PATH": "/models/features.json",
            "OI_ML_SHADOW_LIGHTGBM_MAE_MODEL_PATH": "/models/mae.txt",
            "OI_ML_SHADOW_LIGHTGBM_DEFAULT_MAE_PREMIUM": "27.5",
            "OI_ML_SHADOW_MODEL_VALIDATION_REPORT_PATH": "/models/report.json",
        }
    )

    assert cfg.scorer_mode == "lightgbm"
    assert cfg.lightgbm_classifier_path == "/models/classifier.txt"
    assert cfg.lightgbm_feature_names_path == "/models/features.json"
    assert cfg.lightgbm_mae_model_path == "/models/mae.txt"
    assert cfg.lightgbm_default_mae_premium == 27.5
    assert cfg.model_validation_report_path == "/models/report.json"


def test_load_shadow_runner_config_accepts_shared_model_aliases():
    cfg = load_shadow_runner_config(
        env={
            "OI_ML_SHADOW_ENABLED": "true",
            "OI_ML_SHADOW_SCORER": "lgbm",
            "OI_ML_CLASSIFIER_MODEL_PATH": "/shared/classifier.txt",
            "OI_ML_FEATURE_NAMES_PATH": "/shared/features.json",
            "OI_ML_MAE_MODEL_PATH": "/shared/mae.txt",
            "OI_ML_DEFAULT_MAE_PREMIUM": "31",
        }
    )

    assert cfg.scorer_mode == "lgbm"
    assert cfg.lightgbm_classifier_path == "/shared/classifier.txt"
    assert cfg.lightgbm_feature_names_path == "/shared/features.json"
    assert cfg.lightgbm_mae_model_path == "/shared/mae.txt"
    assert cfg.lightgbm_default_mae_premium == 31.0


def test_load_shadow_runner_config_marks_explicit_expiry():
    cfg = load_shadow_runner_config(
        env={
            "OI_ML_SHADOW_ENABLED": "true",
            "OI_ML_SHADOW_EXPIRY": "2026-05-19",
        }
    )

    assert cfg.expiry == date(2026, 5, 19)
    assert cfg.expiry_is_explicit is True


def test_load_shadow_runner_config_reads_snapshot_window():
    cfg = load_shadow_runner_config(
        env={
            "OI_ML_SHADOW_ENABLED": "true",
            "OI_ML_SHADOW_SNAPSHOT_START_TIME": "09:20",
            "OI_ML_SHADOW_SNAPSHOT_END_TIME": "15:25",
        }
    )

    assert cfg.snapshot_start_time.hour == 9
    assert cfg.snapshot_start_time.minute == 20
    assert cfg.snapshot_end_time.hour == 15
    assert cfg.snapshot_end_time.minute == 25


def test_load_shadow_runner_config_reads_dry_run_spread_risk_overrides():
    cfg = load_shadow_runner_config(
        env={
            "OI_ML_SHADOW_ENABLED": "true",
            "OI_ML_SHADOW_SPREAD_WIDTH_POINTS": "180",
            "OI_ML_SHADOW_MAX_SPREAD_LOSS_RUPEES": "5500",
            "OI_ML_SHADOW_MAX_OPEN_SPREADS": "1",
            "OI_ML_SHADOW_TARGET_ABS_DELTA": "0.18",
            "OI_ML_SHADOW_MAX_ABS_GAMMA": "0.0025",
            "OI_ML_SHADOW_SIZE_DOWN_LOT_MULTIPLIER": "0.4",
        }
    )

    assert cfg.spread_width_points == 180
    assert cfg.max_spread_loss_rupees == 5500.0
    assert cfg.max_open_spreads == 1
    assert cfg.greek_risk_config.target_abs_delta == 0.18
    assert cfg.greek_risk_config.max_abs_gamma == 0.0025
    assert cfg.greek_risk_config.size_down_lot_multiplier == 0.4


def test_resolve_listed_expiry_uses_provider_calendar(monkeypatch):
    monkeypatch.setattr(
        shadow_runner,
        "_load_scrip_master",
        lambda: [
            {
                "symbol": "NIFTY19MAY2625200CE",
                "expiry": "19MAY2026",
                "strike": "2520000",
                "exch_seg": "NFO",
                "token": "111",
            },
            {
                "symbol": "NIFTY26MAY2625200CE",
                "expiry": "26MAY2026",
                "strike": "2520000",
                "exch_seg": "NFO",
                "token": "222",
            },
        ],
    )
    cfg = OiMlShadowRunnerConfig(
        enabled=True,
        underlying="NIFTY",
        expiry=date(2026, 5, 21),
        expiry_is_explicit=False,
        provider="angel",
    )

    resolved = shadow_runner._resolve_listed_expiry(
        cfg,
        today=date(2026, 5, 17),
    )

    assert resolved.expiry == date(2026, 5, 19)


def test_resolve_listed_expiry_rejects_explicit_unlisted_expiry(monkeypatch):
    monkeypatch.setattr(
        shadow_runner,
        "_load_scrip_master",
        lambda: [
            {
                "symbol": "NIFTY19MAY2625200CE",
                "expiry": "19MAY2026",
                "strike": "2520000",
                "exch_seg": "NFO",
                "token": "111",
            },
        ],
    )
    cfg = OiMlShadowRunnerConfig(
        enabled=True,
        underlying="NIFTY",
        expiry=date(2026, 5, 21),
        expiry_is_explicit=True,
        provider="angel",
    )

    with pytest.raises(RuntimeError, match="not listed"):
        shadow_runner._resolve_listed_expiry(
            cfg,
            today=date(2026, 5, 17),
        )


def test_refresh_listed_expiry_rolls_implicit_config_after_date_change(monkeypatch):
    monkeypatch.setattr(
        shadow_runner,
        "_load_scrip_master",
        lambda: [
            {
                "symbol": "NIFTY26MAY2625200CE",
                "expiry": "26MAY2026",
                "strike": "2520000",
                "exch_seg": "NFO",
                "token": "222",
            },
            {
                "symbol": "NIFTY02JUN2625200CE",
                "expiry": "02JUN2026",
                "strike": "2520000",
                "exch_seg": "NFO",
                "token": "333",
            },
        ],
    )
    cfg = OiMlShadowRunnerConfig(
        enabled=True,
        underlying="NIFTY",
        expiry=date(2026, 5, 26),
        expiry_is_explicit=False,
        provider="angel",
    )

    refreshed, resolved_for_day = shadow_runner._refresh_listed_expiry_for_day(
        cfg,
        resolved_for_day=date(2026, 5, 26),
        today=date(2026, 5, 27),
    )

    assert refreshed.expiry == date(2026, 6, 2)
    assert refreshed.expiry_is_explicit is False
    assert resolved_for_day == date(2026, 5, 27)


def test_refresh_listed_expiry_preserves_explicit_config_after_date_change(monkeypatch):
    monkeypatch.setattr(
        shadow_runner,
        "_load_scrip_master",
        lambda: pytest.fail("explicit expiry should not refresh from scrip master"),
    )
    cfg = OiMlShadowRunnerConfig(
        enabled=True,
        underlying="NIFTY",
        expiry=date(2026, 5, 26),
        expiry_is_explicit=True,
        provider="angel",
    )

    refreshed, resolved_for_day = shadow_runner._refresh_listed_expiry_for_day(
        cfg,
        resolved_for_day=date(2026, 5, 26),
        today=date(2026, 5, 27),
    )

    assert refreshed is cfg
    assert refreshed.expiry == date(2026, 5, 26)
    assert resolved_for_day == date(2026, 5, 27)


def test_build_lightgbm_scorer_requires_artifact_paths():
    cfg = OiMlShadowRunnerConfig(
        enabled=True,
        scorer_mode="lightgbm",
    )

    with pytest.raises(RuntimeError, match="OI_ML_SHADOW_LIGHTGBM_CLASSIFIER_PATH"):
        shadow_runner._build_scorer(cfg)


def test_build_lightgbm_scorer_loads_artifacts(monkeypatch):
    calls = {}

    class FakeLightGbmScorer:
        @classmethod
        def from_artifacts(cls, **kwargs):
            calls.update(kwargs)
            return "fake-scorer"

    monkeypatch.setattr(shadow_runner, "LightGbmOiMlScorer", FakeLightGbmScorer)
    monkeypatch.setattr(shadow_runner, "_validate_model_report", lambda cfg: None)

    cfg = OiMlShadowRunnerConfig(
        enabled=True,
        scorer_mode="lightgbm",
        lightgbm_classifier_path="/models/classifier.txt",
        lightgbm_feature_names_path="/models/features.json",
        lightgbm_mae_model_path="/models/mae.txt",
        lightgbm_default_mae_premium=45.0,
    )

    scorer = shadow_runner._build_scorer(cfg)

    assert scorer == "fake-scorer"
    assert calls == {
        "classifier_path": "/models/classifier.txt",
        "feature_names_path": "/models/features.json",
        "mae_model_path": "/models/mae.txt",
        "default_mae_premium": 45.0,
    }


def test_build_lightgbm_scorer_requires_passed_validation_report():
    cfg = OiMlShadowRunnerConfig(
        enabled=True,
        scorer_mode="lightgbm",
        lightgbm_classifier_path="/models/classifier.txt",
        lightgbm_feature_names_path="/models/features.json",
    )

    with pytest.raises(RuntimeError, match="validation report"):
        shadow_runner._build_scorer(cfg)


def test_build_constant_scorer_requires_explicit_smoke_override():
    cfg = OiMlShadowRunnerConfig(
        enabled=True,
        scorer_mode="constant",
        constant_probability=0.64,
        constant_mae_premium=40.0,
    )

    with pytest.raises(RuntimeError, match="smoke-only"):
        shadow_runner._build_scorer(cfg)


def test_validate_model_report_accepts_passed_report(tmp_path):
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"promotion": {"passed": True}}), encoding="utf-8")
    cfg = OiMlShadowRunnerConfig(
        enabled=True,
        scorer_mode="lightgbm",
        model_validation_report_path=str(report),
    )

    shadow_runner._validate_model_report(cfg)


def test_next_weekly_expiry_returns_upcoming_thursday():
    assert shadow_runner._next_weekly_expiry(date(2026, 5, 17)) == date(2026, 5, 21)
    assert shadow_runner._next_weekly_expiry(date(2026, 5, 21)) == date(2026, 5, 21)


def test_shadow_once_skips_outside_market_window_without_connecting(monkeypatch):
    def fail_connect(*args, **kwargs):
        raise AssertionError("should not connect outside window")

    monkeypatch.setattr(shadow_runner, "connect_with_retry", fail_connect)
    cfg = OiMlShadowRunnerConfig(
        enabled=True,
        expiry=date(2026, 5, 21),
        market_window_only=True,
        validation_gate_enabled=False,
    )

    result = run_shadow_once(
        cfg,
        now=datetime(2026, 5, 19, 22, 1, tzinfo=IST),
    )

    assert result.decision_action == "NO_TRADE"
    assert result.reason == "outside_shadow_window"


def test_shadow_once_captures_snapshot_inside_snapshot_window_before_entry(monkeypatch):
    def fail_decision(*args, **kwargs):
        raise AssertionError("decision engine should not run outside entry window")

    monkeypatch.setattr(shadow_runner, "get_control_plane_dsn", lambda: "dsn")
    monkeypatch.setattr(shadow_runner, "connect_with_retry", lambda *_, **__: FakeConn())
    monkeypatch.setattr(shadow_runner, "_capture_snapshot", lambda cfg: 220)
    monkeypatch.setattr(shadow_runner, "OiMlCeDecisionEngine", fail_decision)
    cfg = OiMlShadowRunnerConfig(
        enabled=True,
        expiry=date(2026, 5, 21),
        market_window_only=True,
        validation_gate_enabled=False,
    )

    result = run_shadow_once(
        cfg,
        now=datetime(2026, 5, 19, 9, 20, tzinfo=IST),
    )

    assert result.decision_action == "NO_TRADE"
    assert result.reason == "outside_entry_window"
    assert result.snapshot_stored_rows == 220


class FakeConn:
    def __init__(self):
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def commit(self):
        self.committed = True


class FakeDecisionEngine:
    last_config = None

    def __init__(self, *args, **kwargs):
        FakeDecisionEngine.last_config = kwargs.get("config")

    def evaluate_entry(self, **kwargs):
        return SimpleNamespace(
            action=OiMlEntryAction.NO_TRADE,
            reason="no_fresh_option_snapshot",
            selected=None,
        )


def test_shadow_once_evaluates_no_trade_without_recording(monkeypatch):
    monkeypatch.setattr(shadow_runner, "get_control_plane_dsn", lambda: "dsn")
    monkeypatch.setattr(shadow_runner, "connect_with_retry", lambda *_, **__: FakeConn())
    monkeypatch.setattr(shadow_runner, "_capture_snapshot", lambda cfg: 12)
    monkeypatch.setattr(shadow_runner, "OiMlCeDecisionEngine", FakeDecisionEngine)
    monkeypatch.setattr(shadow_runner, "PostgresOiMlShadowLifecycleStore", FakeStore)

    cfg = OiMlShadowRunnerConfig(
        enabled=True,
        expiry=date(2026, 5, 21),
        market_window_only=False,
        spread_width_points=180,
        max_spread_loss_rupees=5500.0,
        validation_gate_enabled=False,
    )

    result = run_shadow_once(cfg, now=datetime(2026, 5, 19, 10, 0, tzinfo=IST))

    assert result.decision_action == "NO_TRADE"
    assert result.reason == "no_fresh_option_snapshot"
    assert result.snapshot_stored_rows == 12
    assert FakeDecisionEngine.last_config.spread_width_points == 180.0
    assert FakeDecisionEngine.last_config.guard_config.max_spread_loss_rupees == 5500.0


class FakeStageDecisionEngine:
    def __init__(self, *args, **kwargs):
        pass

    def evaluate_entry(self, **kwargs):
        return SimpleNamespace(
            action=OiMlEntryAction.STAGE_ENTRY,
            reason="candidate_passed_guard",
            selected=SimpleNamespace(),
        )


class FakeStore:
    open_spreads = 0
    virtual_filled = False
    flattened = 0

    def __init__(self, conn):
        self.conn = conn

    def flatten_due_virtual_positions(self, **kwargs):
        return self.flattened

    def count_open_virtual_spreads(self, **kwargs):
        return self.open_spreads

    def record_intent(self, intent, **kwargs):
        return SimpleNamespace(record_id=99)

    def mark_virtual_fill(self, record, **kwargs):
        self.virtual_filled = True
        return record


def test_shadow_once_records_and_virtual_fills_staged_intent(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(shadow_runner, "get_control_plane_dsn", lambda: "dsn")
    monkeypatch.setattr(shadow_runner, "connect_with_retry", lambda *_, **__: conn)
    monkeypatch.setattr(shadow_runner, "_capture_snapshot", lambda cfg: 0)
    monkeypatch.setattr(shadow_runner, "OiMlCeDecisionEngine", FakeStageDecisionEngine)
    monkeypatch.setattr(shadow_runner, "PostgresOiMlShadowLifecycleStore", FakeStore)
    monkeypatch.setattr(
        shadow_runner,
        "build_order_intent_from_candidate",
        lambda *_, **__: SimpleNamespace(
            ok=True,
            intent=SimpleNamespace(
                intent_id="intent-1",
                estimated_net_credit_points=12.5,
            ),
            reasons=(),
        ),
    )
    cfg = OiMlShadowRunnerConfig(
        enabled=True,
        expiry=date(2026, 5, 21),
        market_window_only=False,
        validation_gate_enabled=False,
        allow_constant_scorer=True,
        scorer_mode="constant",
    )

    result = run_shadow_once(cfg, now=datetime(2026, 5, 19, 10, 0, tzinfo=IST))

    assert result.decision_action == "STAGE_ENTRY"
    assert result.reason == "candidate_passed_guard"
    assert result.intent_id == "intent-1"
    assert result.shadow_record_id == 99
    assert result.lifecycle_updates == 1
    assert conn.committed is True


def test_shadow_once_blocks_when_open_virtual_spread_limit_reached(monkeypatch):
    conn = FakeConn()
    FakeStore.open_spreads = 1
    monkeypatch.setattr(shadow_runner, "get_control_plane_dsn", lambda: "dsn")
    monkeypatch.setattr(shadow_runner, "connect_with_retry", lambda *_, **__: conn)
    monkeypatch.setattr(shadow_runner, "_capture_snapshot", lambda cfg: 0)
    monkeypatch.setattr(shadow_runner, "PostgresOiMlShadowLifecycleStore", FakeStore)
    monkeypatch.setattr(
        shadow_runner,
        "OiMlCeDecisionEngine",
        lambda *_, **__: pytest.fail("decision should be blocked by open virtual spread"),
    )
    cfg = OiMlShadowRunnerConfig(
        enabled=True,
        expiry=date(2026, 5, 21),
        market_window_only=False,
        validation_gate_enabled=False,
        max_open_spreads=1,
    )

    result = run_shadow_once(cfg, now=datetime(2026, 5, 19, 10, 0, tzinfo=IST))

    assert result.decision_action == "NO_TRADE"
    assert result.reason == "virtual_open_spread_limit_reached"
    FakeStore.open_spreads = 0


def test_shadow_once_blocks_latest_validation_error(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(shadow_runner, "get_control_plane_dsn", lambda: "dsn")
    monkeypatch.setattr(shadow_runner, "connect_with_retry", lambda *_, **__: conn)
    monkeypatch.setattr(shadow_runner, "_capture_snapshot", lambda cfg: 0)
    monkeypatch.setattr(shadow_runner, "PostgresOiMlShadowLifecycleStore", FakeStore)
    monkeypatch.setattr(
        shadow_runner,
        "_latest_validation_gate",
        lambda *_, **__: shadow_runner._ValidationGateResult(
            allowed=False,
            reason="latest_validation_error",
        ),
    )
    monkeypatch.setattr(
        shadow_runner,
        "OiMlCeDecisionEngine",
        lambda *_, **__: pytest.fail("decision should be blocked by validation"),
    )
    cfg = OiMlShadowRunnerConfig(
        enabled=True,
        expiry=date(2026, 5, 21),
        market_window_only=False,
    )

    result = run_shadow_once(cfg, now=datetime(2026, 5, 19, 10, 0, tzinfo=IST))

    assert result.decision_action == "NO_TRADE"
    assert result.reason == "validation_gate_failed:latest_validation_error"


def test_shadow_once_refreshes_validation_gate_timestamp_after_snapshot(monkeypatch):
    conn = FakeConn()
    start = datetime(2026, 5, 19, 10, 0, tzinfo=IST)
    after_snapshot = datetime(2026, 5, 19, 10, 0, 35, tzinfo=IST)
    clock = iter((start, after_snapshot))
    gate_times = []

    monkeypatch.setattr(shadow_runner, "_current_ist", lambda: next(clock))
    monkeypatch.setattr(shadow_runner, "get_control_plane_dsn", lambda: "dsn")
    monkeypatch.setattr(shadow_runner, "connect_with_retry", lambda *_, **__: conn)
    monkeypatch.setattr(shadow_runner, "_capture_snapshot", lambda cfg: 198)
    monkeypatch.setattr(shadow_runner, "PostgresOiMlShadowLifecycleStore", FakeStore)
    monkeypatch.setattr(shadow_runner, "OiMlCeDecisionEngine", FakeDecisionEngine)

    def fake_validation_gate(conn, *, config, now):
        gate_times.append(now)
        return shadow_runner._ValidationGateResult(allowed=True)

    monkeypatch.setattr(shadow_runner, "_latest_validation_gate", fake_validation_gate)
    cfg = OiMlShadowRunnerConfig(
        enabled=True,
        expiry=date(2026, 5, 21),
        market_window_only=False,
    )

    result = run_shadow_once(cfg)

    assert result.decision_action == "NO_TRADE"
    assert result.reason == "no_fresh_option_snapshot"
    assert result.snapshot_stored_rows == 198
    assert gate_times == [after_snapshot]
