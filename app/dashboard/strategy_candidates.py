"""Admin review helpers for optimizer strategy_config_candidates."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional

from fastapi import HTTPException, status

from app.core.audit_log import emit_audit_event
from app.data.postgres import connect_with_retry, get_control_plane_dsn

_CANDIDATE_SELECT_COLUMNS = (
    "candidate_id",
    "strategy_config_id",
    "candidate_params",
    "metrics",
    "backtest_window",
    "optimizer_version",
    "created_at",
    "reviewed_at",
    "reviewed_by",
    "status",
    "tenant_id",
    "broker_account_id",
    "strategy_id",
    "enabled",
    "current_params",
    "strategy_updated_at",
)

_REPLAY_DEFAULT_PARAM_KEYS: Mapping[str, set[str]] = {
    "ema20_strategy": {
        "sl_pct",
        "tp_pct",
        "ema_period",
        "require_rsi_falling",
        "use_adx_filter",
        "min_adx",
        "signal_timeframe",
        "tp1_pct",
        "tp1_qty_pct",
        "giveback_pct",
        "giveback_arm_pct",
        "decay_tighten_minutes_before_eod",
        "decay_tp_multiplier",
        "decay_trail_buffer_multiplier",
    },
    "put_momentum_scalper": {
        "option_sl_pct",
        "partial_tp_r",
        "final_tp_r",
        "rsi_min",
        "rsi_max",
        "min_atr_ratio",
        "max_bars_in_trade",
        "lookback_breakdown_bars",
        "rsi_falling_bars_required",
    },
    "exclusive_nifty_ce_buy": {
        "sl_atr",
        "tp_atr",
        "rsi_min",
        "rsi_max",
        "ema_atr_buffer",
        "macd_hist_min",
        "min_adx",
        "min_di_spread",
        "vol_quantile",
        "ema_fail_bars",
        "timeframe_seconds",
    },
}

_APP_OPTIMIZER_PARAM_KEYS: Mapping[str, set[str]] = {
    "ema20_strategy": {
        "ema_period",
        "signal_timeframe",
        "sl_pct",
        "tp_pct",
        "min_atr",
        "require_rsi_falling",
        "use_adx_filter",
        "min_adx",
        "min_di_spread",
    },
    "exclusive_nifty_ce_buy": {
        "timeframe_seconds",
        "rsi_min",
        "rsi_max",
        "macd_hist_min",
        "allow_near_macd",
        "macd_near",
        "ema_atr_buffer",
        "min_adx",
        "min_di_spread",
        "sl_atr",
        "tp_atr",
        "ema_fail_buffer_atr",
        "ema_fail_bars",
        "session_start",
        "last_entry_time",
        "squareoff_time",
        "late_start",
        "max_trades_per_day",
        "cooldown_bars",
        "late_tp_cap_atr",
        "trail_active_atr",
        "trail_cushion_atr",
        "late_trail_active_atr",
        "late_trail_cushion",
    },
    "put_momentum_scalper": {
        "rsi_min",
        "rsi_max",
        "min_atr_ratio",
        "option_sl_pct",
        "final_tp_r",
        "rsi_falling_bars_required",
        "lookback_breakdown_bars",
        "max_bars_in_trade",
        "entry_start",
        "entry_end",
    },
}

ALLOWED_CANDIDATE_PARAM_KEYS: Mapping[str, frozenset[str]] = {
    strategy_id: frozenset(
        _REPLAY_DEFAULT_PARAM_KEYS.get(strategy_id, set())
        | _APP_OPTIMIZER_PARAM_KEYS.get(strategy_id, set())
    )
    for strategy_id in (
        set(_REPLAY_DEFAULT_PARAM_KEYS) | set(_APP_OPTIMIZER_PARAM_KEYS)
    )
}


def _row_value(row: Any, index: int, key: str) -> Any:
    if isinstance(row, Mapping):
        return row[key]
    return row[index]


def _row_to_mapping(row: Any) -> dict[str, Any]:
    return {
        key: _row_value(row, index, key)
        for index, key in enumerate(_CANDIDATE_SELECT_COLUMNS)
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, str):
        return value
    if hasattr(value, "lower") and hasattr(value, "upper"):
        return {
            "lower": _jsonable(getattr(value, "lower")),
            "upper": _jsonable(getattr(value, "upper")),
            "bounds": getattr(value, "bounds", None),
        }
    if isinstance(value, Mapping):
        return {str(key): _jsonable(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, Mapping):
            return dict(parsed)
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=f"Expected JSON object from database, got {type(value).__name__}",
    )


def _candidate_sql(*, where: str = "", suffix: str = "") -> str:
    where_clause = f"WHERE {where}" if where else ""
    return f"""
        SELECT
            c.candidate_id,
            c.strategy_config_id,
            c.params AS candidate_params,
            c.metrics,
            c.backtest_window,
            c.optimizer_version,
            c.created_at,
            c.reviewed_at,
            c.reviewed_by,
            c.status,
            s.tenant_id,
            s.broker_account_id,
            s.strategy_id,
            s.enabled,
            s.params AS current_params,
            s.updated_at AS strategy_updated_at
        FROM public.strategy_config_candidates c
        JOIN public.strategy_configs s
          ON s.strategy_config_id = c.strategy_config_id
        {where_clause}
        {suffix}
    """


def _param_diff(
    current_params: Mapping[str, Any],
    candidate_params: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    diff: dict[str, dict[str, Any]] = {}
    for key in sorted(set(current_params) | set(candidate_params)):
        current = current_params.get(key)
        candidate = candidate_params.get(key)
        if current != candidate:
            diff[key] = {"current": _jsonable(current), "candidate": _jsonable(candidate)}
    return diff


def _candidate_payload(row: Any) -> dict[str, Any]:
    item = _row_to_mapping(row)
    current_params = _as_dict(item["current_params"])
    candidate_params = _as_dict(item["candidate_params"])
    return {
        "candidate_id": str(item["candidate_id"]),
        "strategy_config_id": str(item["strategy_config_id"]),
        "tenant_id": str(item["tenant_id"]),
        "broker_account_id": str(item["broker_account_id"]),
        "strategy_id": str(item["strategy_id"]),
        "enabled": bool(item["enabled"]),
        "status": str(item["status"]),
        "params": _jsonable(candidate_params),
        "current_params": _jsonable(current_params),
        "param_diff": _param_diff(current_params, candidate_params),
        "metrics": _jsonable(_as_dict(item["metrics"])),
        "backtest_window": _jsonable(item["backtest_window"]),
        "optimizer_version": str(item["optimizer_version"]),
        "created_at": _jsonable(item["created_at"]),
        "reviewed_at": _jsonable(item["reviewed_at"]),
        "reviewed_by": item["reviewed_by"],
        "strategy_updated_at": _jsonable(item["strategy_updated_at"]),
    }


def _cursor_execute_fetchall(conn: Any, sql: str, params: Mapping[str, Any]) -> list[Any]:
    with conn.cursor() as cur:
        cur.execute(sql, dict(params))
        return list(cur.fetchall() or [])


def _cursor_execute_fetchone(conn: Any, sql: str, params: Mapping[str, Any]) -> Any:
    with conn.cursor() as cur:
        cur.execute(sql, dict(params))
        return cur.fetchone()


def _connect(*, autocommit: bool) -> Any:
    return connect_with_retry(get_control_plane_dsn(), autocommit=autocommit)


def list_strategy_candidates(
    *,
    candidate_status: Optional[str],
    limit: int,
) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": int(limit)}
    where = ""
    if candidate_status:
        where = "c.status = %(status)s"
        params["status"] = candidate_status
    sql = _candidate_sql(
        where=where,
        suffix="ORDER BY c.created_at DESC LIMIT %(limit)s",
    )
    with _connect(autocommit=True) as conn:
        rows = _cursor_execute_fetchall(conn, sql, params)
    candidates = [_candidate_payload(row) for row in rows]
    return {"count": len(candidates), "candidates": candidates}


def get_strategy_candidate(candidate_id: str) -> dict[str, Any]:
    with _connect(autocommit=True) as conn:
        row = _cursor_execute_fetchone(
            conn,
            _candidate_sql(where="c.candidate_id = %(candidate_id)s"),
            {"candidate_id": candidate_id},
        )
    if row is None:
        raise HTTPException(status_code=404, detail="strategy candidate not found")
    return _candidate_payload(row)


def _validate_candidate_params(
    *,
    strategy_id: str,
    candidate_params: Mapping[str, Any],
) -> None:
    allowed = ALLOWED_CANDIDATE_PARAM_KEYS.get(strategy_id)
    if not allowed:
        raise HTTPException(
            status_code=422,
            detail=f"No candidate parameter schema registered for strategy_id={strategy_id!r}",
        )
    unknown = sorted(set(candidate_params) - set(allowed))
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=(
                f"candidate.params contains unknown key(s) for {strategy_id}: "
                + ", ".join(unknown)
            ),
        )


def _require_reviewable(row: Any, *, now: datetime) -> dict[str, Any]:
    if row is None:
        raise HTTPException(status_code=404, detail="strategy candidate not found")
    item = _row_to_mapping(row)
    if item["reviewed_at"] is not None:
        raise HTTPException(status_code=409, detail="candidate already reviewed")
    if str(item["status"]) != "pending":
        raise HTTPException(status_code=409, detail="candidate is not pending")
    created_at = item["created_at"]
    if isinstance(created_at, datetime):
        created_utc = created_at.astimezone(timezone.utc)
        if now.astimezone(timezone.utc) - created_utc > timedelta(days=7):
            raise HTTPException(status_code=409, detail="candidate is older than 7 days")
    return item


def _review_candidate(
    *,
    candidate_id: str,
    actor: str,
    action: str,
    target_status: str,
    reason: Optional[str],
    request_id: Optional[str],
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    resolved_now = now or datetime.now(timezone.utc)
    with _connect(autocommit=False) as conn:
        try:
            row = _cursor_execute_fetchone(
                conn,
                _candidate_sql(
                    where="c.candidate_id = %(candidate_id)s",
                    suffix="FOR UPDATE OF c, s",
                ),
                {"candidate_id": candidate_id},
            )
            item = _require_reviewable(row, now=resolved_now)
            strategy_id = str(item["strategy_id"])
            current_params = _as_dict(item["current_params"])
            candidate_params = _as_dict(item["candidate_params"])
            merged_params = dict(current_params)
            merged_params.update(candidate_params)

            if target_status == "promoted":
                _validate_candidate_params(
                    strategy_id=strategy_id,
                    candidate_params=candidate_params,
                )
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE public.strategy_configs
                           SET params = %(params)s::jsonb,
                               updated_at = NOW()
                         WHERE strategy_config_id = %(strategy_config_id)s
                        """,
                        {
                            "params": json.dumps(merged_params, sort_keys=True),
                            "strategy_config_id": item["strategy_config_id"],
                        },
                    )

            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE public.strategy_config_candidates
                       SET status = %(status)s,
                           reviewed_at = NOW(),
                           reviewed_by = %(reviewed_by)s
                     WHERE candidate_id = %(candidate_id)s
                       AND reviewed_at IS NULL
                    """,
                    {
                        "status": target_status,
                        "reviewed_by": actor,
                        "candidate_id": candidate_id,
                    },
                )
                if getattr(cur, "rowcount", 1) != 1:
                    raise HTTPException(status_code=409, detail="candidate already reviewed")
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise

    after = get_strategy_candidate(candidate_id)
    emit_audit_event(
        actor=actor,
        action=action,
        resource_type="strategy_config_candidate",
        resource_id=candidate_id,
        before={
            "status": item["status"],
            "strategy_config_id": item["strategy_config_id"],
            "params": _jsonable(current_params),
        },
        after={
            "status": target_status,
            "strategy_config_id": item["strategy_config_id"],
            "params": _jsonable(merged_params if target_status == "promoted" else current_params),
        },
        metadata={"reason": reason or ""},
        request_id=request_id,
    )
    return after


def approve_strategy_candidate(
    *,
    candidate_id: str,
    actor: str,
    reason: Optional[str],
    request_id: Optional[str],
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    return _review_candidate(
        candidate_id=candidate_id,
        actor=actor,
        action="approve_strategy_candidate",
        target_status="promoted",
        reason=reason,
        request_id=request_id,
        now=now,
    )


def reject_strategy_candidate(
    *,
    candidate_id: str,
    actor: str,
    reason: Optional[str],
    request_id: Optional[str],
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    return _review_candidate(
        candidate_id=candidate_id,
        actor=actor,
        action="reject_strategy_candidate",
        target_status="rejected",
        reason=reason,
        request_id=request_id,
        now=now,
    )


def replay_default_param_keys() -> Mapping[str, set[str]]:
    return _REPLAY_DEFAULT_PARAM_KEYS


def iter_allowed_param_keys() -> Iterable[tuple[str, frozenset[str]]]:
    return ALLOWED_CANDIDATE_PARAM_KEYS.items()
