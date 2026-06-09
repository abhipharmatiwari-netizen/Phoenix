from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.strategies.oi_ml.shadow_health import collect_shadow_ingestion_status


IST = ZoneInfo("Asia/Kolkata")


class _Cursor:
    def __init__(self, rows):
        self._rows = list(rows)
        self.description = None

    def execute(self, _sql, _params=None):
        return None

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _Conn:
    def __init__(self, rows):
        self._cursor = _Cursor(rows)

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def test_shadow_ingestion_disabled_does_not_open_db():
    opened = False

    def _factory():
        nonlocal opened
        opened = True
        return _Conn([])

    status = collect_shadow_ingestion_status(
        env={"OI_ML_SHADOW_ENABLED": "false"},
        conn_factory=_factory,
    )

    assert status["status"] == "disabled"
    assert status["reason"] == "shadow_runner_disabled"
    assert status["runner_enabled"] is False
    assert status["health_enabled"] is False
    assert status["dry_run_only"] is True
    assert status["live_order_path_enabled"] is False
    assert opened is False


def test_shadow_health_override_observes_external_sidecar():
    status = collect_shadow_ingestion_status(
        now=datetime(2026, 5, 20, 10, 0, tzinfo=IST),
        env={
            "OI_ML_SHADOW_ENABLED": "false",
            "OI_ML_SHADOW_HEALTH_ENABLED": "true",
            "OI_ML_SHADOW_PROVIDER": "angel",
            "OI_ML_SHADOW_UNDERLYING": "NIFTY",
        },
        conn_factory=lambda: _Conn([
            {"today_row_count": 0},
            {"today_report_count": 0},
            None,
            {"today_intent_count": 0},
        ]),
    )

    assert status["enabled"] is True
    assert status["runner_enabled"] is False
    assert status["health_enabled"] is True
    assert status["status"] == "degraded"
    assert status["reason"] == "option_chain_rows_missing"
    assert status["live_order_path_enabled"] is False


def test_shadow_ingestion_missing_rows_is_visible_degraded():
    status = collect_shadow_ingestion_status(
        now=datetime(2026, 5, 20, 10, 0, tzinfo=IST),
        env={
            "OI_ML_SHADOW_ENABLED": "true",
            "OI_ML_SHADOW_PROVIDER": "angel",
            "OI_ML_SHADOW_UNDERLYING": "NIFTY",
            "OI_ML_SHADOW_VALIDATION_REQUIRED": "true",
        },
        conn_factory=lambda: _Conn([
            {"today_row_count": 0},
            {"today_report_count": 0},
            None,
            {"today_intent_count": 0},
        ]),
    )

    assert status["status"] == "degraded"
    assert "option_chain_rows_missing" in status["reason"]
    assert "validation_reports_missing" in status["reason"]
    assert "Angel login/provider timeout" in status["operator_hint"]
    assert status["option_chain"]["today_row_count"] == 0
    assert status["validation_reports"]["today_report_count"] == 0
    assert status["shadow_intents"]["today_intent_count"] == 0
    assert status["dry_run_only"] is True
    assert status["live_order_path_enabled"] is False


def test_shadow_ingestion_degrades_on_validation_error_and_future_source_ts():
    status = collect_shadow_ingestion_status(
        now=datetime(2026, 5, 20, 10, 0, tzinfo=IST),
        env={
            "OI_ML_SHADOW_ENABLED": "true",
            "OI_ML_SHADOW_PROVIDER": "angel",
            "OI_ML_SHADOW_UNDERLYING": "NIFTY",
        },
        conn_factory=lambda: _Conn([
            {
                "today_row_count": 220,
                "latest_ingested_at": datetime(2026, 5, 20, 9, 59, tzinfo=IST),
                "latest_source_ts": datetime(2026, 5, 20, 15, 29, tzinfo=IST),
            },
            {
                "today_report_count": 2,
                "latest_validation_ts": datetime(2026, 5, 20, 9, 59, tzinfo=IST),
            },
            {
                "status": "ERROR",
                "severity": "ERROR",
                "primary_quote_count": 202,
                "reference_quote_count": 0,
            },
            {"today_intent_count": 0},
        ]),
    )

    assert status["status"] == "degraded"
    assert "option_chain_future_source_ts" in status["reason"]
    assert "validation_latest_error" in status["reason"]
    assert status["option_chain"]["latest_source_future_seconds"] > 0
    assert status["validation_reports"]["latest_status"] == "ERROR"
    assert status["validation_reports"]["latest_reference_quote_count"] == 0
    assert "timestamp parsing" in status["operator_hint"]


def test_shadow_ingestion_after_window_accepts_today_close_snapshot():
    status = collect_shadow_ingestion_status(
        now=datetime(2026, 5, 20, 21, 0, tzinfo=IST),
        env={
            "OI_ML_SHADOW_ENABLED": "true",
            "OI_ML_SHADOW_PROVIDER": "angel",
            "OI_ML_SHADOW_UNDERLYING": "NIFTY",
            "OI_ML_SHADOW_VALIDATION_REQUIRED": "true",
            "OI_ML_SHADOW_MAX_STALE_SECONDS": "180",
        },
        conn_factory=lambda: _Conn([
            {
                "today_row_count": 220,
                "latest_ingested_at": datetime(2026, 5, 20, 15, 30, tzinfo=IST),
            },
            {
                "today_report_count": 1,
                "latest_validation_ts": datetime(2026, 5, 20, 15, 31, tzinfo=IST),
            },
            {"status": "OK", "severity": "INFO", "primary_quote_count": 220},
            {"today_intent_count": 0},
        ]),
    )

    assert status["status"] == "ok"
    assert status["reason"] == "after_shadow_snapshot_window"
    assert status["snapshot_expected"] is True
    assert status["snapshot_window_active"] is False
    assert status["option_chain"]["today_row_count"] == 220
