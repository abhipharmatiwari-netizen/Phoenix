#!/usr/bin/env python3
"""Train the Stage-2 OI/ML MAE risk filter."""

from __future__ import annotations

import os
import sys

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from scripts.ml.train_oi_ce_scorer import main as scorer_main  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    forwarded = list(argv or sys.argv[1:])
    if "--task" not in forwarded:
        forwarded = ["--task", "mae", *forwarded]
    return scorer_main(forwarded)


if __name__ == "__main__":
    raise SystemExit(main())
