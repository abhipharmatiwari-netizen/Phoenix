"""Reporting module for replay results and review artifacts."""

from __future__ import annotations

import csv
import json
import os
from typing import Any, Mapping, Optional, Sequence

from scripts.replay.optimizer import OptimizationResult, ParameterRecommendation
from scripts.replay.pnl_tracker import PnLTracker, StrategyMetrics
from scripts.replay.recommendations import build_yaml_patch_suggestions, to_jsonable
from scripts.replay.replay_engine import INVALID_COMBINATIONS_REASONS


def format_metrics_table(metrics: StrategyMetrics) -> str:
    lines = [
        f"{'=' * 60}",
        f"  {metrics.strategy_id} / {metrics.underlying}",
        f"{'=' * 60}",
        f"  Period:              {metrics.first_date} to {metrics.last_date}",
        f"  Trading days:        {metrics.trading_days}",
        f"  Total trades:        {metrics.total_trades}",
        f"  Winning / Losing:    {metrics.winning_trades} / {metrics.losing_trades}",
        f"  Win rate:            {metrics.win_rate:.1%}",
        f"  Gross P&L:           {metrics.gross_pnl:>12,.2f}",
        f"  Fees:                {metrics.total_fees:>12,.2f}",
        f"  Net P&L:             {metrics.net_pnl:>12,.2f}",
        f"  Profit factor:       {metrics.profit_factor:.2f}",
        f"  Avg win:             {metrics.avg_win:>12,.2f}",
        f"  Avg loss:            {metrics.avg_loss:>12,.2f}",
        f"  Max drawdown:        {metrics.max_drawdown:>12,.2f}",
        f"  Max consec losses:   {metrics.max_consecutive_losses}",
        f"  Avg duration (s):    {metrics.avg_trade_duration_seconds:.0f}",
        f"  Expectancy:          {metrics.expectancy:>12,.2f}",
    ]
    if metrics.sharpe_ratio is not None:
        lines.append(f"  Sharpe (ann.):       {metrics.sharpe_ratio:.2f}")
    if metrics.avg_r_multiple is not None:
        lines.append(f"  Avg R-multiple:      {metrics.avg_r_multiple:.3f}")
    lines.append(f"{'=' * 60}")
    return "\n".join(lines)


def format_recommendations(recs: Sequence[ParameterRecommendation]) -> str:
    if not recs:
        return "  No parameter changes recommended (defaults are currently the least risky choice).\n"

    lines = ["", "  PARAMETER RECOMMENDATIONS", "  " + "=" * 58]
    for item in recs:
        safety = "SAFE" if item.production_safe else "REVIEW"
        lines.extend(
            [
                "",
                f"  [{item.confidence}] [{safety}] {item.parameter}",
                f"    Current:     {item.current_value}",
                f"    Recommended: {item.recommended_value}",
                f"    Improvement: +{item.improvement_pct:.1f}%",
                f"    Evidence:    {item.evidence}",
            ]
        )
        for warning in item.warnings:
            lines.append(f"    Warning:     {warning}")
    lines.append("")
    return "\n".join(lines)


def format_invalid_combinations() -> str:
    lines = ["", "  INVALID STRATEGY-UNDERLYING COMBINATIONS", "  " + "=" * 58]
    for (strategy_id, underlying), reason in sorted(INVALID_COMBINATIONS_REASONS.items()):
        lines.extend(["", f"  {strategy_id} + {underlying}:", f"    {reason}"])
    lines.append("")
    return "\n".join(lines)


def format_assumptions(execution_summary: Optional[Mapping[str, Any]] = None) -> str:
    execution = dict(execution_summary or {})
    fill_mode = execution.get("fill_mode", "bar_close_fill")
    tick_model = execution.get("tick_model", "ohlc")
    slippage_bps = float(execution.get("slippage_bps", 0.0) or 0.0)
    fixed_slippage = float(execution.get("fixed_slippage", 0.0) or 0.0)
    spread_bps = float(execution.get("spread_bps", 0.0) or 0.0)
    return f"""
  REPLAY ASSUMPTIONS
  ==========================================================

  1. DATA SOURCE: Replay reads indicator_bars only. The table is treated as
     read-only and replay never writes to broker, hub, or production trading
     control tables.

  2. EVENT ORDERING: Bars are loaded ordered by ts_start ASC, then replayed by
     effective bar-close time. When multiple bars close at the same timestamp,
     higher timeframes dispatch first so lower timeframes can see completed
     multi-timeframe context without future leakage.

  3. TICK MODEL: Replay uses synthetic {tick_model} ticks built from OHLC.
     This improves stop/target realism versus one close-only tick, but it still
     approximates unknown intra-bar sequencing.

  4. EXECUTION MODEL: Replay is configured with fill_mode={fill_mode},
     slippage_bps={slippage_bps:.3f}, fixed_slippage={fixed_slippage:.3f},
     spread_bps={spread_bps:.3f}. Orders submitted during on_bar may fill at
     current close or next-bar open depending on this configuration.

  5. OPTION PRICING: Historical option chains are unavailable in indicator_bars.
     Replay uses a deterministic underlying-driven option proxy for ATM and
     OTM CE/PE marks and fills. Credit-spread legs are approximated from strike
     distance, intrinsic value, ATR, and spot; this is still an approximation
     and ignores Greeks, IV, and exact strike history.

  6. PRIVATE EMA SUPPORT: Replay reads exclusive_nifty_ce_buy_ema20_30s when
     present and falls back to deterministic load-time 30s EMA calculations
     when absent.

  7. SESSION RESETS / FINALIZATION: Behaviour depends on --end-policy:
     * force_exit (legacy): replay closes any open position at every session
       boundary AND at window end. Inflates losses on multi-bar-hold strategies.
     * carry_over (default, issue #216): positions carry across days driven by
       the strategy's own logic (TP / SL / EOD square-off). The replay window
       end records an unrealised REPLAY_WINDOW_END_FORCED mark only; it does
       not submit an EXIT fill and is not included in realized trade metrics.
     * daily_mtm: same carry-over behaviour plus a daily_mtm_snapshot session
       event at each session close. Window-end marks remain unrealised and are
       excluded from realized net PnL and win/loss stats.

  8. TIME ZONES: Bars without timezone info are treated as Asia/Kolkata (IST).
     Replay time, gate diagnostics, and reporting use explicit timezone-aware
     timestamps for determinism.

  9. VOLUME: indicator_bars does not provide actual traded volume. Volume-gated
     logic remains approximate and is called out explicitly in diagnostics.

  10. LIVE SAFETY: place_order_via_bridge short-circuits into replay-local
      execution only. If replay isolation is enabled without a replay sink, the
      bridge fails fast instead of touching live hub runtime.

  ==========================================================
"""


def format_data_gaps_report(bars_loaded: Mapping[str, int], combo_analyses: Optional[Mapping[str, Mapping[str, Any]]] = None) -> str:
    lines = ["", "  DATA AVAILABILITY REPORT", "  " + "=" * 58]
    for key, count in sorted(bars_loaded.items()):
        lines.append(f"  {key}: {count} DB bars loaded")
        if combo_analyses and key in combo_analyses:
            data_profile = dict(combo_analyses[key].get("data_profile") or {})
            missing_by_tf = data_profile.get("missing_optional_columns_by_timeframe") or {}
            null_by_tf = data_profile.get("null_indicator_counts_by_timeframe") or {}
            synthetic_by_tf = data_profile.get("synthetic_indicator_counts_by_timeframe") or {}
            for timeframe, columns in sorted(missing_by_tf.items()):
                if columns:
                    lines.append(f"    tf={timeframe}s missing optional columns: {', '.join(columns)}")
            for timeframe, counts in sorted(null_by_tf.items()):
                if counts:
                    counts_text = ", ".join(f"{name}={count}" for name, count in sorted(counts.items()))
                    lines.append(f"    tf={timeframe}s null indicators: {counts_text}")
            for timeframe, counts in sorted(synthetic_by_tf.items()):
                if counts:
                    counts_text = ", ".join(f"{name}={count}" for name, count in sorted(counts.items()))
                    lines.append(f"    tf={timeframe}s synthetic replay fields: {counts_text}")
    lines.append("")
    return "\n".join(lines)


def format_gate_diagnostics_report(gate_summaries: Mapping[str, Sequence[Mapping[str, Any]]]) -> str:
    lines = ["", "  GATE DIAGNOSTICS", "  " + "=" * 58]
    if not gate_summaries:
        lines.extend(["  No gate diagnostics captured.", ""])
        return "\n".join(lines)
    for key, rows in sorted(gate_summaries.items()):
        total = sum(int(row.get("count", 0)) for row in rows)
        candidate_entries = sum(
            int(row.get("count", 0))
            for row in rows
            if bool(row.get("passed")) and str(row.get("reason")) == "candidate_entry"
        )
        lines.extend(
            [
                "",
                f"  {key}:",
                f"    Total gate decisions: {total}",
                f"    Candidate entries:    {candidate_entries}",
            ]
        )
        failed_rows = [dict(row) for row in rows if not bool(row.get("passed"))]
        if not failed_rows:
            lines.append("    No failed gates captured.")
            continue
        lines.append("    Top failed gates:")
        for row in failed_rows[:5]:
            timeframe = row.get("timeframe_seconds")
            regime = row.get("regime") or "unknown"
            lines.append(
                f"      {row.get('reason')} (gate={row.get('gate')}, tf={timeframe}s, regime={regime}): {int(row.get('count', 0))}"
            )
    lines.append("")
    return "\n".join(lines)


def format_regime_pnl_report(combo_analyses: Mapping[str, Mapping[str, Any]]) -> str:
    lines = ["", "  REGIME PNL", "  " + "=" * 58]
    if not combo_analyses:
        lines.extend(["  No regime PnL available.", ""])
        return "\n".join(lines)
    wrote = False
    for key, analysis in sorted(combo_analyses.items()):
        rows = list(analysis.get("regime_summary") or [])
        if not rows:
            continue
        wrote = True
        lines.append(f"  {key}:")
        for row in rows:
            trades = max(1, int(row.get("trades", 0) or 0))
            wins = int(row.get("wins", 0) or 0)
            win_rate = wins / trades if trades else 0.0
            lines.append(
                "    {bucket}: trades={trades} net_pnl={net_pnl} win_rate={win_rate:.1%} avg_pnl={avg}".format(
                    bucket=row.get("bucket") or "unknown",
                    trades=int(row.get("trades", 0) or 0),
                    net_pnl=row.get("net_pnl", 0.0),
                    win_rate=win_rate,
                    avg=row.get("avg_net_pnl", 0.0),
                )
            )
    if not wrote:
        lines.append("  No trades with regime tags.")
    lines.append("")
    return "\n".join(lines)


def format_combo_diagnostics(combo_key: str, analysis: Mapping[str, Any]) -> str:
    lines = ["", f"  DIAGNOSTICS: {combo_key}", "  " + "-" * 58]
    summary = dict(analysis.get("summary") or {})
    if summary:
        lines.append(
            "  Summary: trades={total_trades} net_pnl={net_pnl} profit_factor={profit_factor} max_dd={max_drawdown}".format(
                **summary
            )
        )
    for section_name, label in (
        ("exit_reason_summary", "Exit reasons"),
        ("time_of_day_summary", "Time-of-day"),
        ("regime_summary", "Regime"),
        ("loss_analysis", "Loss buckets"),
    ):
        rows = list(analysis.get(section_name) or [])
        lines.append(f"  {label}:")
        if not rows:
            lines.append("    none")
            continue
        for row in rows[:5]:
            lines.append(
                f"    {row.get('bucket')}: trades={row.get('trades')} net_pnl={row.get('net_pnl')}"
            )
    warnings = list(analysis.get("warnings") or [])
    if warnings:
        lines.append("  Warnings:")
        for warning in warnings:
            lines.append(f"    {warning}")
    lines.append("")
    return "\n".join(lines)


def write_full_report(
    output_dir: str,
    all_metrics: Mapping[str, StrategyMetrics],
    all_trades: Mapping[str, PnLTracker],
    optimization_results: Mapping[str, Sequence[OptimizationResult]],
    recommendations: Mapping[str, Sequence[ParameterRecommendation]],
    bars_loaded: Mapping[str, int],
    gate_summaries: Mapping[str, Sequence[Mapping[str, Any]]],
    missing_indicators: Mapping[str, Mapping[str, int]],
    *,
    combo_analyses: Optional[Mapping[str, Mapping[str, Any]]] = None,
    parameter_sensitivity: Optional[Mapping[str, Mapping[str, Any]]] = None,
    recommendation_payloads: Optional[Mapping[str, Mapping[str, Any]]] = None,
    strategy_env_path: str = "app/config/strategy_env.yaml",
    execution_summary: Optional[Mapping[str, Any]] = None,
) -> str:
    """Write the full replay report to files and return the summary path."""

    del missing_indicators
    os.makedirs(output_dir, exist_ok=True)
    summary_path = os.path.join(output_dir, "replay_summary.txt")
    markdown_path = os.path.join(output_dir, "replay_report.md")
    json_path = os.path.join(output_dir, "replay_results.json")
    yaml_path = os.path.join(output_dir, "recommendations.yaml")
    recommendation_json_path = os.path.join(output_dir, "recommendation_summary.json")

    with open(summary_path, "w", encoding="utf-8") as handle:
        handle.write("PHOENIX v9 STRATEGY REPLAY REPORT\n")
        handle.write("=" * 60 + "\n\n")
        for key, metrics in sorted(all_metrics.items()):
            handle.write(format_metrics_table(metrics) + "\n\n")
        for key, analysis in sorted((combo_analyses or {}).items()):
            handle.write(format_combo_diagnostics(key, analysis))
        handle.write(format_regime_pnl_report(combo_analyses or {}))
        handle.write(format_gate_diagnostics_report(gate_summaries))
        handle.write(format_data_gaps_report(bars_loaded, combo_analyses))
        handle.write("\n" + "=" * 60 + "\n")
        handle.write("OPTIMIZATION RESULTS\n")
        handle.write("=" * 60 + "\n")
        for key, recs in sorted(recommendations.items()):
            handle.write(f"\n--- {key} ---\n")
            handle.write(format_recommendations(recs))
        handle.write(format_invalid_combinations())
        handle.write(format_assumptions(execution_summary))

    with open(markdown_path, "w", encoding="utf-8") as handle:
        handle.write("# Phoenix v9 Replay Report\n\n")
        for key, metrics in sorted(all_metrics.items()):
            handle.write(f"## {key}\n\n")
            handle.write("```text\n")
            handle.write(format_metrics_table(metrics))
            handle.write("\n```\n\n")
            if combo_analyses and key in combo_analyses:
                handle.write("### Diagnostics\n\n")
                handle.write("```text\n")
                handle.write(format_combo_diagnostics(key, combo_analyses[key]))
                handle.write("\n```\n\n")
        handle.write("## Recommendations\n\n")
        for key, recs in sorted(recommendations.items()):
            handle.write(f"### {key}\n\n```text\n{format_recommendations(recs)}\n```\n\n")
        handle.write("## Assumptions\n\n```text\n")
        handle.write(format_assumptions(execution_summary))
        handle.write("\n```\n")

    payload = {
        "metrics": {key: to_jsonable(value) for key, value in all_metrics.items()},
        "combo_analyses": {key: to_jsonable(value) for key, value in (combo_analyses or {}).items()},
        "optimization_results": {
            key: [to_jsonable(item) for item in value]
            for key, value in optimization_results.items()
        },
        "recommendations": {
            key: [to_jsonable(item) for item in value]
            for key, value in recommendations.items()
        },
        "parameter_sensitivity": to_jsonable(parameter_sensitivity or {}),
        "execution_summary": to_jsonable(execution_summary or {}),
        "strategy_env_path": strategy_env_path,
    }
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)

    with open(recommendation_json_path, "w", encoding="utf-8") as handle:
        json.dump(to_jsonable(recommendation_payloads or {}), handle, indent=2, sort_keys=True, default=str)

    with open(yaml_path, "w", encoding="utf-8") as handle:
        handle.write(
            build_yaml_patch_suggestions(
                strategy_env_path=strategy_env_path,
                recommendation_payloads=recommendation_payloads or {},
            )
        )

    _write_trade_artifacts(output_dir, all_trades)
    _write_gate_artifacts(output_dir, gate_summaries)
    _write_optimization_artifacts(output_dir, optimization_results)
    _write_parameter_sensitivity(output_dir, parameter_sensitivity or {})
    return summary_path


def _write_trade_artifacts(output_dir: str, all_trades: Mapping[str, PnLTracker]) -> None:
    for key, tracker in all_trades.items():
        strategy_id, underlying = key.split("/", 1)
        with open(os.path.join(output_dir, f"trades_{key.replace('/', '_')}.csv"), "w", encoding="utf-8") as handle:
            handle.write(tracker.trade_log_csv(strategy_id, underlying))
        with open(os.path.join(output_dir, f"fills_{key.replace('/', '_')}.csv"), "w", encoding="utf-8") as handle:
            handle.write(tracker.fill_log_csv(strategy_id, underlying))


def _write_gate_artifacts(output_dir: str, gate_summaries: Mapping[str, Sequence[Mapping[str, Any]]]) -> None:
    for key, rows in gate_summaries.items():
        path = os.path.join(output_dir, f"gate_summary_{key.replace('/', '_')}.csv")
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(["gate", "reason", "passed", "timeframe_seconds", "regime", "count"])
            for row in rows:
                writer.writerow(
                    [
                        row.get("gate", ""),
                        row.get("reason", ""),
                        str(bool(row.get("passed"))).lower(),
                        row.get("timeframe_seconds", ""),
                        row.get("regime", ""),
                        int(row.get("count", 0)),
                    ]
                )


def _write_optimization_artifacts(
    output_dir: str,
    optimization_results: Mapping[str, Sequence[OptimizationResult]],
) -> None:
    for key, results in optimization_results.items():
        if not results:
            continue
        path = os.path.join(output_dir, f"optimization_{key.replace('/', '_')}.csv")
        param_keys = sorted(results[0].params.keys())
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(
                param_keys
                + [
                    "score",
                    "total_trades",
                    "net_pnl",
                    "profit_factor",
                    "max_drawdown",
                    "sample_ok",
                    "walk_forward_score",
                    "stability_score",
                    "warnings",
                ]
            )
            for result in results[:100]:
                writer.writerow(
                    [result.params.get(key, "") for key in param_keys]
                    + [
                        f"{result.score:.2f}",
                        result.metrics.total_trades,
                        f"{result.metrics.net_pnl:.2f}",
                        f"{result.metrics.profit_factor:.2f}",
                        f"{result.metrics.max_drawdown:.2f}",
                        str(bool(result.sample_ok)).lower(),
                        f"{result.walk_forward_score:.2f}" if result.walk_forward_score is not None else "",
                        f"{result.stability_score:.2f}" if result.stability_score is not None else "",
                        " | ".join(result.warnings),
                    ]
                )


def _write_parameter_sensitivity(output_dir: str, parameter_sensitivity: Mapping[str, Mapping[str, Any]]) -> None:
    for key, summary in parameter_sensitivity.items():
        if not summary:
            continue
        path = os.path.join(output_dir, f"parameter_sensitivity_{key.replace('/', '_')}.csv")
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(["parameter", "value", "count", "avg_score", "median_score"])
            for parameter, rows in sorted(summary.items()):
                for row in rows:
                    writer.writerow(
                        [
                            parameter,
                            row.get("value", ""),
                            row.get("count", 0),
                            row.get("avg_score", ""),
                            row.get("median_score", ""),
                        ]
                    )


__all__ = [
    "format_assumptions",
    "format_data_gaps_report",
    "format_gate_diagnostics_report",
    "format_invalid_combinations",
    "format_metrics_table",
    "format_recommendations",
    "format_regime_pnl_report",
    "write_full_report",
]
