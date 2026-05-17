from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

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
    )

    result = run_shadow_once(
        cfg,
        now=datetime(2026, 5, 19, 15, 1, tzinfo=IST),
    )

    assert result.decision_action == "NO_TRADE"
    assert result.reason == "outside_shadow_window"


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def commit(self):
        self.committed = True


class FakeDecisionEngine:
    def __init__(self, *args, **kwargs):
        pass

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

    cfg = OiMlShadowRunnerConfig(
        enabled=True,
        expiry=date(2026, 5, 21),
        market_window_only=False,
    )

    result = run_shadow_once(cfg, now=datetime(2026, 5, 19, 10, 0, tzinfo=IST))

    assert result.decision_action == "NO_TRADE"
    assert result.reason == "no_fresh_option_snapshot"
    assert result.snapshot_stored_rows == 12


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
    def __init__(self, conn):
        self.conn = conn

    def record_intent(self, intent, **kwargs):
        return SimpleNamespace(record_id=99)


def test_shadow_once_records_staged_intent(monkeypatch):
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
            intent=SimpleNamespace(intent_id="intent-1"),
            reasons=(),
        ),
    )
    cfg = OiMlShadowRunnerConfig(
        enabled=True,
        expiry=date(2026, 5, 21),
        market_window_only=False,
    )

    result = run_shadow_once(cfg, now=datetime(2026, 5, 19, 10, 0, tzinfo=IST))

    assert result.decision_action == "STAGE_ENTRY"
    assert result.reason == "candidate_passed_guard"
    assert result.intent_id == "intent-1"
    assert result.shadow_record_id == 99
    assert conn.committed is True
