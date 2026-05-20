"""Health evidence for the OI/ML shadow sidecar.

The sidecar is dry-run only, but operators still need to know when ingestion is
effectively down. This module summarizes persisted option-chain, validation,
and shadow-intent freshness without enabling any live order path.
"""

from __future__ import annotations

import os
import json
from datetime import datetime, time, timedelta, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from app.strategies.oi_ml.shadow_runner import load_shadow_runner_config


IST = ZoneInfo("Asia/Kolkata")
DEFAULT_MAX_STALE_SECONDS = 180

_OPTION_CHAIN_STATUS_SQL = """
SELECT COUNT(*) AS today_row_count,
       MAX(snapshot_ts) AS latest_snapshot_ts,
       MAX(source_ts) AS latest_source_ts,
       MAX(ingested_at) AS latest_ingested_at
FROM public.option_chain_1m
WHERE provider = %(provider)s
  AND underlying = %(underlying)s
  AND ingested_at >= %(day_start)s
  AND ingested_at < %(day_end)s
"""

_VALIDATION_STATUS_SQL = """
SELECT COUNT(*) AS today_report_count,
       MAX(validation_ts) AS latest_validation_ts
FROM public.option_chain_validation_reports
WHERE underlying = %(underlying)s
  AND validation_ts >= %(day_start)s
  AND validation_ts < %(day_end)s
"""

_LATEST_VALIDATION_SQL = """
SELECT validation_ts, status, severity, primary_quote_count, reference_quote_count
FROM public.option_chain_validation_reports
WHERE underlying = %(underlying)s
  AND validation_ts >= %(day_start)s
  AND validation_ts < %(day_end)s
ORDER BY validation_ts DESC
LIMIT 1
"""

_SHADOW_INTENT_STATUS_SQL = """
SELECT COUNT(*) AS today_intent_count,
       MAX(created_at) AS latest_intent_created_at
FROM public.oi_ml_shadow_order_intents
WHERE created_at >= %(day_start)s
  AND created_at < %(day_end)s
"""


def collect_shadow_ingestion_status(
    *,
    now: datetime | None = None,
    env: Mapping[str, str] | None = None,
    conn_factory: Any | None = None,
) -> dict[str, Any]:
    """Return dashboard-safe OI/ML shadow ingestion evidence."""
    source = env or os.environ
    config = load_shadow_runner_config(env=source)
    current = (now or datetime.now(IST)).astimezone(IST)
    health_override = _env_bool_or_none(source.get("OI_ML_SHADOW_HEALTH_ENABLED"))
    enabled = bool(config.enabled if health_override is None else health_override)
    provider = str(config.provider or "").strip().lower() or "unknown"
    base = {
        "enabled": enabled,
        "status": "disabled" if not enabled else "unknown",
        "reason": _disabled_reason(config.enabled, health_override) if not enabled else None,
        "runner_enabled": bool(config.enabled),
        "health_enabled": enabled,
        "underlying": config.underlying,
        "provider": provider,
        "dry_run_only": True,
        "live_order_path_enabled": False,
        "checked_at": current.isoformat(),
    }
    if not enabled:
        return base

    day_start = datetime.combine(current.date(), time.min, tzinfo=IST)
    day_end = day_start + timedelta(days=1)
    params = {
        "provider": provider,
        "underlying": config.underlying,
        "day_start": day_start,
        "day_end": day_end,
    }

    try:
        if conn_factory is None:
            from app.data.postgres import connect_with_retry, get_control_plane_dsn

            conn_factory = lambda: connect_with_retry(get_control_plane_dsn())
        with conn_factory() as conn:
            option_rows = _fetch_one(conn, _OPTION_CHAIN_STATUS_SQL, params)
            validation_rows = _fetch_one(conn, _VALIDATION_STATUS_SQL, params)
            latest_validation = _fetch_one(conn, _LATEST_VALIDATION_SQL, params)
            intent_rows = _fetch_one(conn, _SHADOW_INTENT_STATUS_SQL, params)
    except Exception as exc:
        return {
            **base,
            "status": "unknown",
            "reason": "shadow_ingestion_evidence_unavailable",
            "error": type(exc).__name__,
        }

    option_count = _row_int(option_rows, "today_row_count")
    validation_count = _row_int(validation_rows, "today_report_count")
    intent_count = _row_int(intent_rows, "today_intent_count")
    latest_ingested_at = _row_value(option_rows, "latest_ingested_at")
    latest_snapshot_ts = _row_value(option_rows, "latest_snapshot_ts")
    latest_source_ts = _row_value(option_rows, "latest_source_ts")
    latest_validation_ts = _row_value(validation_rows, "latest_validation_ts")
    latest_intent_ts = _row_value(intent_rows, "latest_intent_created_at")

    snapshot_expected = _snapshot_expected(current, config.snapshot_start_time)
    snapshot_window_active = _within_window(
        current,
        config.snapshot_start_time,
        config.snapshot_end_time,
    )
    max_stale_seconds = _env_int(
        source.get("OI_ML_SHADOW_MAX_STALE_SECONDS"),
        DEFAULT_MAX_STALE_SECONDS,
        minimum=30,
    )
    validation_required = _env_bool(
        source.get("OI_ML_SHADOW_VALIDATION_REQUIRED"),
        default=False,
    )
    reasons: list[str] = []
    if config.capture_snapshot and snapshot_expected and option_count <= 0:
        reasons.append("option_chain_rows_missing")
    latest_ingested_age = _age_seconds(latest_ingested_at, current)
    if (
        config.capture_snapshot
        and snapshot_window_active
        and option_count > 0
        and latest_ingested_age is not None
        and latest_ingested_age > max_stale_seconds
    ):
        reasons.append("option_chain_stale")
    if validation_required and snapshot_expected and validation_count <= 0:
        reasons.append("validation_reports_missing")

    status = "degraded" if reasons else "ok"
    reason = ",".join(reasons) if reasons else None
    if not snapshot_expected:
        reason = "before_shadow_snapshot_window"
    elif not snapshot_window_active and not reasons:
        reason = "after_shadow_snapshot_window"

    return {
        **base,
        "status": status,
        "reason": reason,
        "snapshot_expected": snapshot_expected,
        "snapshot_window_active": snapshot_window_active,
        "snapshot_window": {
            "start": config.snapshot_start_time.isoformat(timespec="minutes"),
            "end": config.snapshot_end_time.isoformat(timespec="minutes"),
        },
        "max_stale_seconds": max_stale_seconds,
        "option_chain": {
            "today_row_count": option_count,
            "latest_snapshot_ts": _iso_or_none(latest_snapshot_ts),
            "latest_source_ts": _iso_or_none(latest_source_ts),
            "latest_ingested_at": _iso_or_none(latest_ingested_at),
            "latest_ingested_age_seconds": latest_ingested_age,
        },
        "validation_reports": {
            "today_report_count": validation_count,
            "latest_validation_ts": _iso_or_none(latest_validation_ts),
            "latest_status": str(_row_value(latest_validation, "status") or ""),
            "latest_severity": str(_row_value(latest_validation, "severity") or ""),
            "latest_primary_quote_count": _row_int(
                latest_validation,
                "primary_quote_count",
            ),
            "latest_reference_quote_count": _row_int(
                latest_validation,
                "reference_quote_count",
            ),
        },
        "shadow_intents": {
            "today_intent_count": intent_count,
            "latest_created_at": _iso_or_none(latest_intent_ts),
        },
    }


def _snapshot_expected(current: datetime, start_time: time) -> bool:
    return current.astimezone(IST).time() >= start_time


def _within_window(value: datetime, start: time, end: time) -> bool:
    now_time = value.astimezone(IST).time()
    return start <= now_time <= end


def _fetch_one(conn: Any, sql: str, params: dict[str, Any]) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        description = getattr(cur, "description", None)
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    columns = [desc[0] for desc in description or []]
    return dict(zip(columns, row))


def _row_value(row: Mapping[str, Any] | None, key: str) -> Any:
    if not row:
        return None
    return row.get(key)


def _row_int(row: Mapping[str, Any] | None, key: str) -> int:
    try:
        return int(_row_value(row, key) or 0)
    except (TypeError, ValueError):
        return 0


def _iso_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _age_seconds(value: Any, now: datetime) -> int | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        return None
    ts = value
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max(0, int((now.astimezone(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds()))


def _env_int(value: object, default: int, *, minimum: int) -> int:
    try:
        parsed = int(value) if value not in (None, "") else int(default)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(int(minimum), parsed)


def _env_bool(value: object, *, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _env_bool_or_none(value: object) -> bool | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _disabled_reason(runner_enabled: bool, health_override: bool | None) -> str:
    if health_override is False:
        return "shadow_health_disabled"
    if not runner_enabled:
        return "shadow_runner_disabled"
    return "shadow_health_disabled"


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def main() -> int:
    status = collect_shadow_ingestion_status()
    print(json.dumps(status, sort_keys=True, default=_json_default))
    if status.get("enabled") and status.get("status") in {"degraded", "unknown"}:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["collect_shadow_ingestion_status", "main"]
