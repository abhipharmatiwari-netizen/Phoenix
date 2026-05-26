#!/usr/bin/env python3
"""Validate Angel option-chain snapshots against NSE web option-chain data.

This command is validation-only. It never creates strategy candidates, shadow
intents, or live orders. When ``--store-nse`` is provided it persists NSE rows
under provider ``nse_web`` only for audit/comparison.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
import os
import sys
from pathlib import Path
from typing import Any

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from app.data.nse_option_chain_provider import (  # noqa: E402
    NseOptionChainProvider,
    NseWebOptionChainClient,
)
from app.data.option_chain_repository import OptionChainRepository  # noqa: E402
from app.data.option_chain_store import OptionChainStore  # noqa: E402
from app.data.option_chain_validation import (  # noqa: E402
    OptionChainValidationConfig,
    compare_angel_to_nse,
    expected_non_equivalent_reference_fields,
    expected_missing_reference_fields,
    reference_contract_coverage_is_partial,
)
from app.data.postgres import connect_with_retry, get_control_plane_dsn  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare latest Angel option_chain_1m snapshot with NSE web data"
    )
    parser.add_argument("--underlying", default="NIFTY", help="Underlying index symbol")
    parser.add_argument("--expiry", required=True, help="Option expiry date YYYY-MM-DD")
    parser.add_argument(
        "--decision-ts",
        help="Decision timestamp; defaults to current UTC time. Naive values are treated as UTC.",
    )
    parser.add_argument(
        "--lookback-minutes",
        type=int,
        default=15,
        help="How far back to search for the latest Angel snapshot",
    )
    parser.add_argument("--dsn", default=os.environ.get("OPTION_CHAIN_PG_DSN", ""))
    parser.add_argument(
        "--nse-json-input",
        help="Use a saved NSE option-chain JSON payload instead of calling nseindia.com",
    )
    parser.add_argument(
        "--store-nse",
        action="store_true",
        help="Persist normalized NSE rows as provider=nse_web before comparison",
    )
    parser.add_argument("--output-json", help="Write the validation report JSON to this path")
    parser.add_argument("--fail-on-mismatch", action="store_true")
    parser.add_argument("--oi-abs-tolerance", type=int, default=0)
    parser.add_argument("--volume-abs-tolerance", type=int, default=250)
    parser.add_argument("--volume-pct-tolerance", type=float, default=0.05)
    parser.add_argument("--price-abs-tolerance", type=float, default=0.10)
    parser.add_argument("--price-pct-tolerance", type=float, default=0.01)
    parser.add_argument("--iv-abs-tolerance", type=float, default=0.50)
    parser.add_argument("--iv-pct-tolerance", type=float, default=0.05)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    expiry = _parse_date(args.expiry, arg_name="--expiry")
    decision_ts = _parse_decision_ts(args.decision_ts)
    min_snapshot_ts = decision_ts - timedelta(minutes=max(args.lookback_minutes, 0))
    underlying = str(args.underlying or "").strip().upper()
    if not underlying:
        raise ValueError("--underlying is required")

    nse_payload = _load_nse_payload(args.nse_json_input, underlying=underlying)
    nse_quotes = list(
        NseOptionChainProvider(nse_payload).fetch_chain(
            underlying=underlying,
            expiry=expiry,
            snapshot_ts=decision_ts,
        )
    )
    if not nse_quotes:
        payload = _empty_report(
            underlying=underlying,
            expiry=expiry,
            decision_ts=decision_ts,
            error="no_nse_quotes",
        )
        _emit_report(payload, args.output_json)
        return 2

    dsn = args.dsn or get_control_plane_dsn()
    with connect_with_retry(dsn, autocommit=False) as conn:
        stored_nse_rows = 0
        if args.store_nse:
            stored_nse_rows = OptionChainStore(conn, commit=True).upsert_quotes(nse_quotes)
        angel_quotes = OptionChainRepository(conn).fetch_latest_snapshot(
            underlying=underlying,
            expiry=expiry,
            decision_ts=decision_ts,
            min_snapshot_ts=min_snapshot_ts,
            provider="angel",
        )

    metadata = {
        "angel_quotes_count": len(angel_quotes),
        "nse_quotes_count": len(nse_quotes),
        "angel_snapshot_ts": _snapshot_ts(angel_quotes),
        "nse_snapshot_ts": _snapshot_ts(nse_quotes),
        "decision_ts": decision_ts.isoformat(),
        "min_snapshot_ts": min_snapshot_ts.isoformat(),
        "nse_source": "file" if args.nse_json_input else "nse_web",
        "nse_rows_stored": stored_nse_rows,
        "validation_only": True,
    }
    skipped_reference_fields = expected_missing_reference_fields(nse_quotes)
    if skipped_reference_fields:
        metadata["skipped_missing_reference_fields"] = list(skipped_reference_fields)
    skipped_non_equivalent_fields = expected_non_equivalent_reference_fields(nse_quotes)
    if skipped_non_equivalent_fields:
        metadata["skipped_non_equivalent_reference_fields"] = list(
            skipped_non_equivalent_fields
        )
    reference_coverage_partial = reference_contract_coverage_is_partial(nse_quotes)
    if reference_coverage_partial:
        metadata["reference_contract_coverage"] = "partial"
    if not angel_quotes:
        payload = _empty_report(
            underlying=underlying,
            expiry=expiry,
            decision_ts=decision_ts,
            error="no_angel_snapshot",
            metadata=metadata,
        )
        _emit_report(payload, args.output_json)
        return 2

    report = compare_angel_to_nse(
        angel_quotes,
        nse_quotes,
        config=OptionChainValidationConfig(
            oi_abs_tolerance=args.oi_abs_tolerance,
            volume_abs_tolerance=args.volume_abs_tolerance,
            volume_pct_tolerance=args.volume_pct_tolerance,
            price_abs_tolerance=args.price_abs_tolerance,
            price_pct_tolerance=args.price_pct_tolerance,
            iv_abs_tolerance=args.iv_abs_tolerance,
            iv_pct_tolerance=args.iv_pct_tolerance,
            skip_missing_reference_fields=skipped_reference_fields,
            skip_reference_fields=skipped_non_equivalent_fields,
            ignore_primary_only_contracts=reference_coverage_partial,
        ),
        metadata=metadata,
    )
    payload = report.to_dict()
    _emit_report(payload, args.output_json)
    if args.fail_on_mismatch and not report.ok:
        return 1
    return 0


def _load_nse_payload(path: str | None, *, underlying: str) -> dict[str, Any]:
    if path:
        with Path(path).open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError("--nse-json-input must contain a JSON object")
        return payload
    return dict(NseWebOptionChainClient().fetch_option_chain(symbol=underlying))


def _parse_date(value: str, *, arg_name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{arg_name} must be YYYY-MM-DD") from exc


def _parse_decision_ts(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("--decision-ts must be ISO-8601") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _snapshot_ts(quotes: list[Any]) -> str | None:
    if not quotes:
        return None
    return max(quote.normalized().snapshot_ts for quote in quotes).isoformat()


def _empty_report(
    *,
    underlying: str,
    expiry: date,
    decision_ts: datetime,
    error: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    merged_metadata = {
        "decision_ts": decision_ts.isoformat(),
        "validation_only": True,
        "error": error,
    }
    merged_metadata.update(metadata or {})
    return {
        "underlying": underlying,
        "expiry": expiry.isoformat(),
        "ok": False,
        "compared_contracts": 0,
        "angel_only_contracts": [],
        "nse_only_contracts": [],
        "mismatches": [],
        "missing_angel_iv": 0,
        "missing_nse_iv": 0,
        "metadata": merged_metadata,
    }


def _emit_report(payload: dict[str, Any], output_json: str | None) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True, default=str)
    print(text)
    if output_json:
        Path(output_json).write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
