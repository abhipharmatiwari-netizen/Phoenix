#!/usr/bin/env python3
"""Build an OI/ML option-chain data-quality approval report."""

from __future__ import annotations

import argparse
from datetime import date
import os
import sys

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from app.data.option_chain_backfill import iter_option_quotes_from_file  # noqa: E402
from app.data.option_chain_quality_gate import (  # noqa: E402
    OptionChainProviderDecision,
    build_option_chain_quality_report,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a one-day OI/ML option-chain quality gate report"
    )
    parser.add_argument("--input", required=True, help="CSV/JSONL option-chain sample")
    parser.add_argument("--provider", required=True)
    parser.add_argument("--underlying", default="NIFTY")
    parser.add_argument("--session-date", required=True, help="YYYY-MM-DD")
    parser.add_argument(
        "--candidate-strike",
        action="append",
        type=int,
        required=True,
        help="Candidate strike to include in hard-field completeness checks",
    )
    parser.add_argument("--live-source", default="Angel One FULL quote")
    parser.add_argument("--historical-source", default="approved vendor backfill")
    parser.add_argument("--retention-months", type=int, default=18)
    parser.add_argument("--expired-weeklies-available", action="store_true")
    parser.add_argument(
        "--reconciliation-plan",
        action="append",
        default=[],
        help="Random-day reconciliation step against broker terminal/vendor reference",
    )
    parser.add_argument(
        "--stress-backfill-decision",
        default="Extend backfill beyond 18 months for 2024-06-04 and March 2020 stress windows.",
    )
    parser.add_argument("--output", help="Optional JSON output path")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    quotes = list(
        iter_option_quotes_from_file(
            args.input,
            default_provider=args.provider,
            default_underlying=args.underlying,
        )
    )
    provider_decision = OptionChainProviderDecision(
        provider=args.provider,
        live_source=args.live_source,
        historical_source=args.historical_source,
        retention_months=args.retention_months,
        production_feed_allowed=True,
        expired_weeklies_available=bool(args.expired_weeklies_available),
        notes=(
            "NSE web data is validation-only and must not be used as the production feed.",
            "Synthetic OI backfill is prohibited.",
        ),
    )
    report = build_option_chain_quality_report(
        quotes,
        provider_decision=provider_decision,
        underlying=args.underlying,
        session_date=date.fromisoformat(args.session_date),
        candidate_strikes=args.candidate_strike,
        reconciliation_plan=args.reconciliation_plan,
        stress_backfill_decision=args.stress_backfill_decision,
    )
    payload = report.to_json()
    if args.output:
        with open(args.output, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(payload)
            fh.write("\n")
    print(payload)
    return 0 if report.passed else 4


if __name__ == "__main__":
    raise SystemExit(main())
