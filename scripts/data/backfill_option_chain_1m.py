#!/usr/bin/env python3
"""Backfill option_chain_1m from vendor CSV or JSONL files."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from app.data.option_chain_backfill import iter_option_quotes_from_file
from app.data.option_chain_store import OptionChainStore
from app.data.postgres import connect_with_retry, get_control_plane_dsn


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill option_chain_1m rows")
    parser.add_argument("--input", required=True, help="CSV, JSONL, or NDJSON file")
    parser.add_argument("--provider", required=True, help="Vendor/source id, e.g. truedata")
    parser.add_argument("--underlying", help="Default underlying when missing in file")
    parser.add_argument("--exchange", default="NFO", help="Default exchange when missing")
    parser.add_argument(
        "--timestamp-timezone",
        default="Asia/Kolkata",
        help="Timezone for naive vendor timestamps",
    )
    parser.add_argument("--dsn", default=os.environ.get("OPTION_CHAIN_PG_DSN", ""))
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    input_path = Path(args.input)
    quotes = list(
        iter_option_quotes_from_file(
            input_path,
            default_provider=args.provider,
            default_underlying=args.underlying,
            default_exchange=args.exchange,
            timestamp_timezone=args.timestamp_timezone,
        )
    )
    if args.dry_run:
        print(f"Parsed {len(quotes)} option-chain rows from {input_path}")
        return 0
    dsn = args.dsn or get_control_plane_dsn()
    with connect_with_retry(dsn, autocommit=False) as conn:
        stored = OptionChainStore(conn, commit=True).upsert_quotes(quotes)
    print(f"Backfilled {stored} option_chain_1m rows from {input_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
