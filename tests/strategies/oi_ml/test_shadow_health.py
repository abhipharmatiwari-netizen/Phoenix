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
    assert status["dry_run_only"] is True
    assert status["live_order_path_enabled"] is False
    assert opened is False


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
    assert status["option_chain"]["today_row_count"] == 0
    assert status["validation_reports"]["today_report_count"] == 0
    assert status["shadow_intents"]["today_intent_count"] == 0
    assert status["dry_run_only"] is True
    assert status["live_order_path_enabled"] is False
