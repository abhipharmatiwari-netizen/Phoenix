"""Quality-gate reporting for OI/ML option-chain data approval."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
import json
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from app.data.option_chain_provider import OptionQuote


IST = ZoneInfo("Asia/Kolkata")

REQUIRED_APPROVAL_FIELDS: tuple[str, ...] = (
    "source_ts",
    "expiry",
    "strike",
    "option_type",
    "trading_symbol",
    "exchange",
    "symbol_token",
    "oi",
    "volume",
    "iv",
    "bid",
    "ask",
    "ltp",
    "underlying_ltp",
    "vix",
)


@dataclass(frozen=True)
class OptionChainProviderDecision:
    provider: str
    live_source: str
    historical_source: str
    retention_months: int
    production_feed_allowed: bool
    expired_weeklies_available: bool
    notes: tuple[str, ...] = ()

    @property
    def approved(self) -> bool:
        return (
            bool(self.provider)
            and bool(self.live_source)
            and bool(self.historical_source)
            and int(self.retention_months) >= 18
            and self.production_feed_allowed
            and self.expired_weeklies_available
        )


@dataclass(frozen=True)
class OptionChainQualityReport:
    provider_decision: OptionChainProviderDecision
    underlying: str
    session_date: date
    expected_minutes: int
    covered_minutes: int
    trading_minute_coverage: float
    candidate_required_values: int
    candidate_present_values: int
    candidate_field_completeness: float
    missing_by_field: Mapping[str, int] = field(default_factory=dict)
    candidate_strikes: tuple[int, ...] = ()
    reconciliation_plan: tuple[str, ...] = ()
    stress_backfill_decision: str = ""
    passed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_decision": {
                "provider": self.provider_decision.provider,
                "live_source": self.provider_decision.live_source,
                "historical_source": self.provider_decision.historical_source,
                "retention_months": self.provider_decision.retention_months,
                "production_feed_allowed": self.provider_decision.production_feed_allowed,
                "expired_weeklies_available": self.provider_decision.expired_weeklies_available,
                "approved": self.provider_decision.approved,
                "notes": list(self.provider_decision.notes),
            },
            "underlying": self.underlying,
            "session_date": self.session_date.isoformat(),
            "expected_minutes": self.expected_minutes,
            "covered_minutes": self.covered_minutes,
            "trading_minute_coverage": self.trading_minute_coverage,
            "candidate_required_values": self.candidate_required_values,
            "candidate_present_values": self.candidate_present_values,
            "candidate_field_completeness": self.candidate_field_completeness,
            "missing_by_field": dict(self.missing_by_field),
            "candidate_strikes": list(self.candidate_strikes),
            "reconciliation_plan": list(self.reconciliation_plan),
            "stress_backfill_decision": self.stress_backfill_decision,
            "passed": self.passed,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


def build_option_chain_quality_report(
    quotes: Iterable[OptionQuote],
    *,
    provider_decision: OptionChainProviderDecision,
    underlying: str,
    session_date: date,
    candidate_strikes: Sequence[int],
    session_start: time = time(9, 15),
    session_end: time = time(15, 30),
    min_trading_minute_coverage: float = 0.95,
    min_candidate_field_completeness: float = 0.98,
    reconciliation_plan: Sequence[str] = (),
    stress_backfill_decision: str = "",
) -> OptionChainQualityReport:
    rows = [quote.normalized() for quote in quotes]
    target_underlying = str(underlying or "").strip().upper()
    target_strikes = tuple(sorted({int(strike) for strike in candidate_strikes}))
    expected_minutes = _expected_session_minutes(
        session_date=session_date,
        session_start=session_start,
        session_end=session_end,
    )
    covered_minutes = len(
        {
            _minute_bucket(row.snapshot_ts)
            for row in rows
            if row.underlying == target_underlying
            and row.snapshot_ts.astimezone(IST).date() == session_date
            and _inside_window(row.snapshot_ts, session_start, session_end)
        }
    )
    coverage = _ratio(covered_minutes, expected_minutes)

    candidate_rows = [
        row
        for row in rows
        if row.underlying == target_underlying
        and row.strike in target_strikes
        and row.snapshot_ts.astimezone(IST).date() == session_date
        and _inside_window(row.snapshot_ts, session_start, session_end)
    ]
    required_values = len(candidate_rows) * len(REQUIRED_APPROVAL_FIELDS)
    present_values = 0
    missing_by_field: dict[str, int] = {field_name: 0 for field_name in REQUIRED_APPROVAL_FIELDS}
    for row in candidate_rows:
        for field_name in REQUIRED_APPROVAL_FIELDS:
            if _field_present(getattr(row, field_name)):
                present_values += 1
            else:
                missing_by_field[field_name] += 1
    missing_by_field = {k: v for k, v in missing_by_field.items() if v}
    completeness = _ratio(present_values, required_values)

    passed = (
        provider_decision.approved
        and coverage >= float(min_trading_minute_coverage)
        and completeness >= float(min_candidate_field_completeness)
        and bool(reconciliation_plan)
        and bool(str(stress_backfill_decision).strip())
    )
    return OptionChainQualityReport(
        provider_decision=provider_decision,
        underlying=target_underlying,
        session_date=session_date,
        expected_minutes=expected_minutes,
        covered_minutes=covered_minutes,
        trading_minute_coverage=coverage,
        candidate_required_values=required_values,
        candidate_present_values=present_values,
        candidate_field_completeness=completeness,
        missing_by_field=missing_by_field,
        candidate_strikes=target_strikes,
        reconciliation_plan=tuple(str(item) for item in reconciliation_plan if str(item).strip()),
        stress_backfill_decision=str(stress_backfill_decision).strip(),
        passed=passed,
    )


def _expected_session_minutes(
    *,
    session_date: date,
    session_start: time,
    session_end: time,
) -> int:
    start = datetime.combine(session_date, session_start, tzinfo=IST)
    end = datetime.combine(session_date, session_end, tzinfo=IST)
    if end < start:
        return 0
    return int((end - start).total_seconds() // 60) + 1


def _minute_bucket(value: datetime) -> datetime:
    dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).replace(second=0, microsecond=0)


def _inside_window(value: datetime, session_start: time, session_end: time) -> bool:
    tod = _minute_bucket(value).time()
    return session_start <= tod <= session_end


def _field_present(value: Any) -> bool:
    return value not in (None, "")


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return float(numerator) / float(denominator)


__all__ = [
    "IST",
    "OptionChainProviderDecision",
    "OptionChainQualityReport",
    "REQUIRED_APPROVAL_FIELDS",
    "build_option_chain_quality_report",
]
