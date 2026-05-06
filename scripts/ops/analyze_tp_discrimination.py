"""Analyze whether tp_pct values discriminate in the NIFTY sweep results.

Reads the optimization CSV and groups by (ema_period, sl_pct, require_rsi_falling,
use_adx_filter, min_adx) — within each group, we should see different net_pnl
values across tp_pct ∈ {0.15...0.50} if the parameter is meaningfully tunable.
If all rows in a group have identical net_pnl, tp_pct does not discriminate.
"""
from __future__ import annotations

import csv
import sys
from collections import defaultdict
from pathlib import Path


def analyze(path: Path) -> None:
    by_group = defaultdict(dict)
    with path.open(encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            key = (
                row["ema_period"],
                row["sl_pct"],
                row["require_rsi_falling"],
                row["use_adx_filter"],
                row["min_adx"],
            )
            by_group[key][row["tp_pct"]] = (row["net_pnl"], row["total_trades"])

    underlying = path.stem.replace("optimization_ema20_strategy_", "")
    print(f"\n=== {underlying}: tp_pct discrimination ===")
    print(f"  groups: {len(by_group)}")

    differentiated = 0
    identical = 0
    for key, tp_results in by_group.items():
        pnl_values = {pnl for pnl, _ in tp_results.values()}
        if len(pnl_values) > 1:
            differentiated += 1
        else:
            identical += 1

    print(f"  groups where tp_pct discriminated: {differentiated} / {len(by_group)}")
    print(f"  groups where ALL tp_pct values gave identical net_pnl: {identical}")
    if identical == len(by_group):
        print(f"  CONCLUSION: tp_pct in [0.15, 0.50] CANNOT be tuned with this replay's option-pricing proxy.")

    # Show pnl summary per tp_pct
    print(f"\n  net_pnl distribution by tp_pct (median across groups):")
    pnl_by_tp = defaultdict(list)
    for key, tp_results in by_group.items():
        for tp_pct, (pnl, _) in tp_results.items():
            pnl_by_tp[tp_pct].append(float(pnl))

    for tp_pct in sorted(pnl_by_tp, key=float):
        values = sorted(pnl_by_tp[tp_pct])
        median = values[len(values) // 2]
        print(f"    tp_pct={tp_pct}: n={len(values)} median_pnl={median:.2f}")


if __name__ == "__main__":
    for path in sys.argv[1:]:
        analyze(Path(path))
