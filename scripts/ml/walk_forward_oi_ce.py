#!/usr/bin/env python3
"""Evaluate OI/ML CE-seller walk-forward promotion gates."""

from __future__ import annotations

import argparse
import os
import sys

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from app.strategies.oi_ml.model import (  # noqa: E402
    evaluate_promotion_gates,
    grouped_walk_forward_splits,
    write_rejection_or_promotion_report,
)
from app.strategies.oi_ml.training import load_training_jsonl  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run grouped walk-forward gates for OI/ML CE-seller artifacts"
    )
    parser.add_argument("--input", required=True, help="Training JSONL artifact")
    parser.add_argument("--output", required=True, help="Promotion/rejection report JSON")
    parser.add_argument("--folds", type=int, default=3)
    parser.add_argument("--purge-groups", type=int, default=1)
    parser.add_argument("--embargo-groups", type=int, default=1)
    parser.add_argument("--lookahead-violations", type=int, default=0)
    parser.add_argument("--eod-violations", type=int, default=0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    records = load_training_jsonl(args.input)
    splits = grouped_walk_forward_splits(
        records,
        folds=args.folds,
        purge_groups=args.purge_groups,
        embargo_groups=args.embargo_groups,
    )
    fold_expectancies = []
    for split in splits:
        pnl = [
            float(records[idx].get("pnl_per_lot") or 0.0)
            for idx in split.test_indices
        ]
        if pnl:
            fold_expectancies.append(sum(pnl) / len(pnl))
    report = evaluate_promotion_gates(
        records,
        fold_expectancies=fold_expectancies,
        lookahead_violations=args.lookahead_violations,
        eod_violations=args.eod_violations,
    )
    write_rejection_or_promotion_report(
        report,
        args.output,
        model_artifacts={"walk_forward_folds": len(splits)},
    )
    print(report.to_dict())
    return 0 if report.passed else 4


if __name__ == "__main__":
    raise SystemExit(main())
