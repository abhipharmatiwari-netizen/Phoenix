#!/usr/bin/env python3
"""Run the opt-in OI option-chain snapshotter."""

from __future__ import annotations

import argparse
import os
import sys

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from app.data.oi_snapshotter_runtime import load_runtime_config, run_runtime


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run OI option-chain snapshotter")
    parser.add_argument("--enable", action="store_true", default=None, help="Required opt-in gate")
    parser.add_argument("--provider", default=None, help="Provider id; currently angel")
    parser.add_argument("--underlying", default=None, help="Underlying, e.g. NIFTY")
    parser.add_argument("--expiry", default=None, help="Option expiry date YYYY-MM-DD")
    parser.add_argument("--dsn", default=None, help="Postgres DSN override")
    parser.add_argument("--once", action="store_true", default=None, help="Capture one snapshot and exit")
    parser.add_argument("--max-snapshots", type=int, help="Bound session loop")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_runtime_config(
        enabled=args.enable,
        provider=args.provider,
        underlying=args.underlying,
        expiry=args.expiry,
        dsn=args.dsn,
        once=args.once,
        max_snapshots=args.max_snapshots,
    )
    if not config.enabled:
        print("OI snapshotter disabled; pass --enable or set OI_SNAPSHOTTER_ENABLED=true")
        return 0
    results = run_runtime(config)
    stored = sum(result.stored_count for result in results)
    print(f"OI snapshotter complete: snapshots={len(results)} stored_rows={stored}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
