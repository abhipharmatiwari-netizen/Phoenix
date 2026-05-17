#!/usr/bin/env python3
"""Train OI/ML CE-seller models from JSONL dataset artifacts."""

from __future__ import annotations

import argparse
import os
import sys

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from app.strategies.oi_ml.training import (
    DEFAULT_BINARY_LABEL,
    DEFAULT_REGRESSION_LABEL,
    LightGbmUnavailableError,
    load_training_jsonl,
    save_lightgbm_model,
    train_lightgbm,
    validate_training_records,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train OI/ML CE-seller LightGBM model")
    parser.add_argument("--input", required=True, help="Training JSONL artifact")
    parser.add_argument("--output", help="Output model path")
    parser.add_argument(
        "--task",
        choices=["binary", "mae"],
        default="binary",
        help="binary trains primary_label classifier; mae trains MAE regressor",
    )
    parser.add_argument(
        "--label",
        help="Override label column. Defaults to primary_label for binary, mae_premium for mae.",
    )
    parser.add_argument("--num-boost-round", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true", help="Validate artifact only")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    label_name = args.label or (
        DEFAULT_REGRESSION_LABEL if args.task == "mae" else DEFAULT_BINARY_LABEL
    )
    records = load_training_jsonl(args.input)
    dataset = validate_training_records(records, label_name=label_name)
    if args.dry_run:
        print(
            f"Validated {dataset.row_count} rows with {len(dataset.feature_names)} "
            f"features label={dataset.label_name}"
        )
        return 0
    if not args.output:
        print("ERROR: --output is required unless --dry-run is set", file=sys.stderr)
        return 2
    objective = "regression" if args.task == "mae" else "binary"
    try:
        result = train_lightgbm(
            records,
            label_name=label_name,
            objective=objective,
            num_boost_round=args.num_boost_round,
        )
    except LightGbmUnavailableError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    save_lightgbm_model(result, args.output)
    print(
        f"Trained {result.objective} model rows={result.row_count} "
        f"features={len(result.feature_names)} output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
