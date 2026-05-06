"""Analyze PHX#186 exit attribution telemetry to drive PHX#185 tp_pct tuning.

Consumes one or more `exit_attribution.jsonl` files written by EMA20 strategy
exits (see `app/strategies/ema20_strategy.py:_emit_exit_attribution`).

For each (underlying, regime_at_entry) cell, ranks tp_pct candidates by what
the realised P&L *would have been* had that tp_pct fired first. This is
strictly an offline counterfactual on already-observed price paths — it does
not change LIVE behaviour.

Usage:
    python scripts/ops/analyze_exit_attribution.py path/to/exit_attribution.jsonl

Optional:
    --min-trades-per-cell N   (default 10) skip cells with fewer trades
    --candidate-tp 0.15 0.20 0.25 0.30 0.35 0.45 0.50  (default = PHX#185 grid)
    --top-n 3                 (default 3) recommendations per cell
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Iterable


def _iter_records(paths: Iterable[Path]):
    for p in paths:
        with p.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("schema") != "ema20.exit_attribution":
                    continue
                yield rec


def evaluate_tp_counterfactual(records: list[dict], tp_pct: float) -> dict:
    """For each FINAL trade, ask: would tp_pct have caught it?

    A trade "captured" by tp_pct = peak_favorable_pct >= tp_pct.
    Counterfactual outcome:
      - captured: realised profit = tp_pct * entry (approx — we don't have intra-trade ticks here)
      - not captured: realised profit = final_profit_pct (whatever actually happened)

    Caveat: this approximation assumes the trade DID reach `peak_favorable_pct`
    monotonically (i.e., the peak was visited before the final exit). For most
    trades this holds; for a U-shape (peak, retrace, recover) the counterfactual
    overstates capture. PHX#186 telemetry doesn't include a "peak ts" field —
    if/when it does, refine here.
    """
    n = 0
    pnl_sum = 0.0
    pnl_sq = 0.0
    wins = 0
    captured = 0
    for r in records:
        if not r.get("final"):
            continue
        peak = float(r.get("peak_favorable_pct") or 0.0)
        final_pct = float(r.get("final_profit_pct") or 0.0)
        if peak >= tp_pct:
            outcome = tp_pct
            captured += 1
        else:
            outcome = final_pct
        n += 1
        pnl_sum += outcome
        pnl_sq += outcome * outcome
        if outcome > 0:
            wins += 1
    if n == 0:
        return {"n": 0}
    mean = pnl_sum / n
    var = max(0.0, pnl_sq / n - mean * mean)
    stdev = var ** 0.5
    sharpe = (mean / stdev) if stdev > 0 else 0.0
    return {
        "n": n,
        "mean_profit_pct": mean,
        "stdev_profit_pct": stdev,
        "sharpe_per_trade": sharpe,
        "win_rate": wins / n,
        "capture_rate": captured / n,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("paths", nargs="+", help="exit_attribution.jsonl file(s)")
    p.add_argument("--min-trades-per-cell", type=int, default=10)
    p.add_argument(
        "--candidate-tp",
        nargs="+",
        type=float,
        default=[0.15, 0.20, 0.25, 0.30, 0.35, 0.45, 0.50],
    )
    p.add_argument("--top-n", type=int, default=3)
    args = p.parse_args(argv)

    paths = [Path(x) for x in args.paths]
    cells: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for rec in _iter_records(paths):
        underlying = str(rec.get("underlying") or "")
        regime = str(rec.get("regime_at_entry") or "UNKNOWN")
        cells[(underlying, regime)].append(rec)

    print(f"Loaded {sum(len(v) for v in cells.values())} attribution records "
          f"across {len(cells)} (underlying, regime) cells.\n")

    if not cells:
        print("No data — telemetry not yet accumulated. Re-run when "
              "exit_attribution.jsonl has trades.")
        return 0

    underlying_recs: dict[str, list[dict]] = defaultdict(list)
    for (u, _), recs in cells.items():
        underlying_recs[u].extend(recs)

    print("=" * 78)
    print("PER-UNDERLYING (regime-agnostic) tp_pct ranking")
    print("=" * 78)
    for underlying in sorted(underlying_recs):
        recs = underlying_recs[underlying]
        finals = [r for r in recs if r.get("final")]
        if len(finals) < args.min_trades_per_cell:
            print(f"\n{underlying}: only {len(finals)} final trades (< min {args.min_trades_per_cell}) — skipped")
            continue
        print(f"\n{underlying}: {len(finals)} final trades")
        evals = []
        for tp in args.candidate_tp:
            m = evaluate_tp_counterfactual(recs, tp)
            evals.append((tp, m))
        # rank by Sharpe per trade
        evals.sort(key=lambda x: x[1].get("sharpe_per_trade", 0.0), reverse=True)
        print(f"  {'rank':<4} {'tp_pct':<7} {'sharpe':<8} {'mean%':<8} {'win%':<6} {'capt%':<6}")
        for rank, (tp, m) in enumerate(evals[:args.top_n], 1):
            print(f"  {rank:<4} {tp:<7.3f} {m['sharpe_per_trade']:<8.3f} "
                  f"{m['mean_profit_pct']:<8.4f} {m['win_rate']:<6.2%} {m['capture_rate']:<6.2%}")

    print("\n" + "=" * 78)
    print("PER (UNDERLYING, REGIME) tp_pct ranking")
    print("=" * 78)
    for (u, regime) in sorted(cells.keys()):
        recs = cells[(u, regime)]
        finals = [r for r in recs if r.get("final")]
        if len(finals) < args.min_trades_per_cell:
            continue
        print(f"\n{u} | regime={regime} | n={len(finals)}")
        evals = []
        for tp in args.candidate_tp:
            m = evaluate_tp_counterfactual(recs, tp)
            evals.append((tp, m))
        evals.sort(key=lambda x: x[1].get("sharpe_per_trade", 0.0), reverse=True)
        print(f"  {'rank':<4} {'tp_pct':<7} {'sharpe':<8} {'mean%':<8} {'win%':<6} {'capt%':<6}")
        for rank, (tp, m) in enumerate(evals[:args.top_n], 1):
            print(f"  {rank:<4} {tp:<7.3f} {m['sharpe_per_trade']:<8.3f} "
                  f"{m['mean_profit_pct']:<8.4f} {m['win_rate']:<6.2%} {m['capture_rate']:<6.2%}")

    print("\nNOTE: Counterfactual assumes monotone path to peak. Schema v1 lacks")
    print("peak_ts; recommendations are directional. For final tuning, add peak_ts")
    print("and intra-trade tick capture to PHX#186 schema (v2) before locking values.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
