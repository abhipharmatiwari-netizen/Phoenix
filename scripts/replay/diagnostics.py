"""Replay diagnostics and operator-facing summaries."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Iterable, List, Mapping, Optional

from scripts.replay.execution_models import adx_regime, atr_regime, time_bucket_ist
from scripts.replay.pnl_tracker import ReplayTrade, StrategyMetrics


def build_trade_analysis(
    *,
    combo_key: str,
    metrics: StrategyMetrics,
    trades: Iterable[ReplayTrade],
    gate_rows: Iterable[Mapping[str, Any]],
    data_profile: Optional[Mapping[str, Any]] = None,
    indicator_snapshot: Optional[Mapping[str, Any]] = None,
    finalization_events: Optional[Iterable[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build a JSON-friendly replay diagnostics bundle for one combo."""

    trade_list = list(trades)
    exit_reason_summary = _group_trade_metric(trade_list, lambda trade: trade.exit_reason or "UNKNOWN")
    time_of_day_summary = _group_trade_metric(trade_list, lambda trade: trade.entry_time_bucket or "unknown")
    regime_summary = _group_trade_metric(trade_list, lambda trade: trade.entry_regime or "unknown")
    loss_buckets = _loss_buckets(trade_list)

    failed_gates = [
        dict(row)
        for row in gate_rows
        if not bool(row.get("passed"))
    ]
    warnings: List[str] = []
    if metrics.total_trades < 5:
        warnings.append("Sample size is too small to trust replay conclusions yet.")
    # Issue #216: separate the two failure modes that used to share one warning.
    # REPLAY_SESSION_BOUNDARY only fires under the legacy --end-policy=force_exit;
    # under the new default (carry_over) it never fires and the warning would
    # be misleading. REPLAY_WINDOW_END_FORCED is the new reason for positions
    # still open at the *window* end -- expected/benign behaviour, surfaced as
    # info rather than warning.
    n_session_boundary = sum(
        1 for t in trade_list if str(t.exit_reason) == "REPLAY_SESSION_BOUNDARY"
    )
    finalization_list = [dict(item) for item in (finalization_events or [])]
    n_window_end_forced = sum(
        1
        for item in finalization_list
        if str(item.get("reason")) == "REPLAY_WINDOW_END_FORCED"
        and not bool(item.get("realized", False))
    )
    if n_session_boundary > 0:
        warnings.append(
            f"Replay forced {n_session_boundary} exit(s) at session boundaries "
            f"(--end-policy=force_exit). Net PnL is biased toward losses on "
            f"strategies with multi-bar holds. Re-run with --end-policy=carry_over "
            f"for an unbiased view."
        )
    if n_window_end_forced > 0:
        warnings.append(
            f"{n_window_end_forced} position(s) still open at the replay window "
            f"end and recorded as unrealised last-bar marks. These are excluded "
            f"from realized net PnL and win/loss stats when comparing strategies."
        )

    return {
        "combo_key": combo_key,
        "summary": {
            "strategy_id": metrics.strategy_id,
            "underlying": metrics.underlying,
            "total_trades": metrics.total_trades,
            "net_pnl": round(metrics.net_pnl, 2),
            "profit_factor": round(metrics.profit_factor, 4) if metrics.profit_factor != float("inf") else "inf",
            "max_drawdown": round(metrics.max_drawdown, 2),
            "trading_days": metrics.trading_days,
        },
        "exit_reason_summary": exit_reason_summary,
        "time_of_day_summary": time_of_day_summary,
        "regime_summary": regime_summary,
        "loss_analysis": loss_buckets,
        "top_failed_gates": failed_gates[:10],
        "data_profile": dict(data_profile or {}),
        "indicator_snapshot": dict(indicator_snapshot or {}),
        "finalization_events": finalization_list,
        "warnings": warnings,
    }


def infer_trade_enrichment(trade: ReplayTrade) -> ReplayTrade:
    """Populate derived replay fields from strategy_context if available."""

    replay = {}
    if isinstance(trade.strategy_context, dict):
        replay = dict(trade.strategy_context.get("_replay") or {})

    if not trade.entry_time_bucket:
        trade.entry_time_bucket = replay.get("time_bucket") or time_bucket_ist(trade.entry_time)
    if not trade.entry_regime:
        trade.entry_regime = replay.get("regime")
    if not trade.entry_regime:
        trade.entry_regime = "|".join(
            [
                adx_regime(_safe_float(replay.get("adx"))),
                atr_regime(_safe_float(replay.get("atr")), _safe_float(replay.get("close"))),
            ]
        )
    if not trade.execution_mode:
        trade.execution_mode = str(replay.get("fill_mode") or replay.get("phase") or "")
    if not trade.entry_time_of_day:
        trade.entry_time_of_day = str(replay.get("time_of_day") or "")
    return trade


def _group_trade_metric(
    trades: Iterable[ReplayTrade],
    key_fn,
) -> List[Dict[str, Any]]:
    buckets: Dict[str, Dict[str, Any]] = {}
    for trade in trades:
        key = str(key_fn(trade) or "unknown")
        bucket = buckets.setdefault(
            key,
            {
                "bucket": key,
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "net_pnl": 0.0,
                "avg_net_pnl": 0.0,
            },
        )
        bucket["trades"] += 1
        bucket["wins"] += 1 if trade.net_pnl > 0 else 0
        bucket["losses"] += 1 if trade.net_pnl <= 0 else 0
        bucket["net_pnl"] += float(trade.net_pnl)

    rows = list(buckets.values())
    for row in rows:
        trades_count = max(1, int(row["trades"]))
        row["net_pnl"] = round(float(row["net_pnl"]), 2)
        row["avg_net_pnl"] = round(float(row["net_pnl"]) / trades_count, 2)
    rows.sort(key=lambda row: (-int(row["trades"]), str(row["bucket"])))
    return rows


def _loss_buckets(trades: Iterable[ReplayTrade]) -> List[Dict[str, Any]]:
    losers = [trade for trade in trades if trade.net_pnl <= 0]
    buckets: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"bucket": "", "trades": 0, "net_pnl": 0.0}
    )
    for trade in losers:
        bucket_name = f"{trade.exit_reason or 'UNKNOWN'} | {trade.entry_time_bucket or 'unknown'} | {trade.entry_regime or 'unknown'}"
        bucket = buckets[bucket_name]
        bucket["bucket"] = bucket_name
        bucket["trades"] += 1
        bucket["net_pnl"] += float(trade.net_pnl)

    rows = list(buckets.values())
    for row in rows:
        row["net_pnl"] = round(float(row["net_pnl"]), 2)
    rows.sort(key=lambda row: (float(row["net_pnl"]), -int(row["trades"]), str(row["bucket"])))
    return rows[:10]


def _safe_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "build_trade_analysis",
    "infer_trade_enrichment",
]
