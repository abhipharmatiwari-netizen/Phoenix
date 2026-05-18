"""Persistence for cross-provider option-chain validation reports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import json
from typing import Any, Mapping


INSERT_VALIDATION_REPORT_SQL = """
INSERT INTO public.option_chain_validation_reports (
    validation_ts, snapshot_ts, underlying, expiry,
    primary_provider, reference_provider,
    status, severity,
    compared_contracts, primary_quote_count, reference_quote_count,
    mismatch_count, primary_only_count, reference_only_count,
    missing_primary_iv, missing_reference_iv,
    report_payload
)
VALUES (
    %(validation_ts)s, %(snapshot_ts)s, %(underlying)s, %(expiry)s,
    %(primary_provider)s, %(reference_provider)s,
    %(status)s, %(severity)s,
    %(compared_contracts)s, %(primary_quote_count)s, %(reference_quote_count)s,
    %(mismatch_count)s, %(primary_only_count)s, %(reference_only_count)s,
    %(missing_primary_iv)s, %(missing_reference_iv)s,
    %(report_payload)s::jsonb
)
RETURNING id;
"""


@dataclass(frozen=True)
class StoredOptionChainValidationReport:
    report_id: int | None
    status: str
    severity: str
    mismatch_count: int
    primary_only_count: int
    reference_only_count: int


@dataclass(frozen=True)
class OptionChainValidationReportStore:
    """Small adapter around an existing Postgres connection."""

    conn: Any
    commit: bool = False

    def insert_report(
        self,
        *,
        payload: Mapping[str, Any],
        validation_ts: datetime,
        snapshot_ts: datetime | None,
        underlying: str,
        expiry: date,
        primary_provider: str,
        reference_provider: str,
        status: str,
        severity: str,
        primary_quote_count: int,
        reference_quote_count: int,
    ) -> StoredOptionChainValidationReport:
        row = _report_to_row(
            payload=payload,
            validation_ts=validation_ts,
            snapshot_ts=snapshot_ts,
            underlying=underlying,
            expiry=expiry,
            primary_provider=primary_provider,
            reference_provider=reference_provider,
            status=status,
            severity=severity,
            primary_quote_count=primary_quote_count,
            reference_quote_count=reference_quote_count,
        )
        with self.conn.cursor() as cur:
            cur.execute(INSERT_VALIDATION_REPORT_SQL, row)
            fetched = cur.fetchone() if hasattr(cur, "fetchone") else None
        if self.commit and hasattr(self.conn, "commit"):
            self.conn.commit()
        return StoredOptionChainValidationReport(
            report_id=_report_id(fetched),
            status=row["status"],
            severity=row["severity"],
            mismatch_count=row["mismatch_count"],
            primary_only_count=row["primary_only_count"],
            reference_only_count=row["reference_only_count"],
        )


def _report_to_row(
    *,
    payload: Mapping[str, Any],
    validation_ts: datetime,
    snapshot_ts: datetime | None,
    underlying: str,
    expiry: date,
    primary_provider: str,
    reference_provider: str,
    status: str,
    severity: str,
    primary_quote_count: int,
    reference_quote_count: int,
) -> dict[str, Any]:
    return {
        "validation_ts": validation_ts,
        "snapshot_ts": snapshot_ts,
        "underlying": str(underlying or "").strip().upper(),
        "expiry": expiry,
        "primary_provider": str(primary_provider or "").strip().lower(),
        "reference_provider": str(reference_provider or "").strip().lower(),
        "status": str(status or "").strip().upper(),
        "severity": str(severity or "").strip().upper(),
        "compared_contracts": int(payload.get("compared_contracts") or 0),
        "primary_quote_count": int(primary_quote_count),
        "reference_quote_count": int(reference_quote_count),
        "mismatch_count": len(payload.get("mismatches") or []),
        "primary_only_count": len(payload.get("angel_only_contracts") or []),
        "reference_only_count": len(payload.get("nse_only_contracts") or []),
        "missing_primary_iv": int(payload.get("missing_angel_iv") or 0),
        "missing_reference_iv": int(payload.get("missing_nse_iv") or 0),
        "report_payload": json.dumps(_json_safe(dict(payload)), sort_keys=True),
    }


def _report_id(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        raw = value.get("id")
    elif isinstance(value, (tuple, list)) and value:
        raw = value[0]
    else:
        raw = value
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


__all__ = [
    "INSERT_VALIDATION_REPORT_SQL",
    "OptionChainValidationReportStore",
    "StoredOptionChainValidationReport",
]
