"""Runtime snapshotter for option-chain OI data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
import logging
import time as time_module
from typing import Callable, Protocol, Sequence
from zoneinfo import ZoneInfo

from app.data.option_chain_provider import (
    OptionChainProvider,
    OptionQuote,
    is_quote_usable_for_live_entry,
)

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class SnapshotResult:
    provider: str
    underlying: str
    expiry: date
    snapshot_ts: datetime
    fetched_count: int
    stored_count: int
    unusable_for_live_count: int
    validation_status: str | None = None
    validation_report_id: int | None = None
    validation_mismatch_count: int | None = None


class OptionChainSnapshotValidator(Protocol):
    def validate(
        self,
        *,
        primary_quotes: Sequence[OptionQuote],
        underlying: str,
        expiry: date,
        snapshot_ts: datetime,
    ) -> object | None:
        ...


class OiSnapshotter:
    """Fetch one-minute option-chain snapshots and persist them."""

    def __init__(
        self,
        *,
        provider: OptionChainProvider,
        store: object,
        validator: OptionChainSnapshotValidator | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.provider = provider
        self.store = store
        self.validator = validator
        self.clock = clock or (lambda: datetime.now(IST))
        self.sleep = sleep or time_module.sleep

    def capture_once(
        self,
        *,
        underlying: str,
        expiry: date,
        snapshot_ts: datetime | None = None,
    ) -> SnapshotResult:
        snapshot = _minute_bucket(snapshot_ts or self.clock())
        quotes = list(
            self.provider.fetch_chain(
                underlying=underlying,
                expiry=expiry,
                snapshot_ts=snapshot,
            )
        )
        stored_count = int(self.store.upsert_quotes(quotes))
        unusable_count = sum(1 for quote in quotes if not is_quote_usable_for_live_entry(quote))
        validation_status = None
        validation_report_id = None
        validation_mismatch_count = None
        if self.validator is not None:
            try:
                validation = self.validator.validate(
                    primary_quotes=quotes,
                    underlying=underlying,
                    expiry=expiry,
                    snapshot_ts=snapshot,
                )
                validation_status = getattr(validation, "status", None)
                validation_report_id = getattr(validation, "report_id", None)
                validation_mismatch_count = getattr(validation, "mismatch_count", None)
            except Exception as exc:
                logger.exception(
                    "oi_snapshot validation failed provider=%s underlying=%s expiry=%s snapshot_ts=%s error=%s",
                    getattr(self.provider, "provider_name", "unknown"),
                    str(underlying).strip().upper(),
                    expiry.isoformat(),
                    snapshot.isoformat(),
                    exc,
                )
                validator_config = getattr(self.validator, "config", None)
                if getattr(validator_config, "fail_on_error", False):
                    raise
        result = SnapshotResult(
            provider=str(getattr(self.provider, "provider_name", "unknown")),
            underlying=str(underlying).strip().upper(),
            expiry=expiry,
            snapshot_ts=snapshot,
            fetched_count=len(quotes),
            stored_count=stored_count,
            unusable_for_live_count=unusable_count,
            validation_status=validation_status,
            validation_report_id=validation_report_id,
            validation_mismatch_count=validation_mismatch_count,
        )
        logger.info(
            "oi_snapshot stored provider=%s underlying=%s expiry=%s fetched=%d stored=%d unusable=%d validation_status=%s validation_report_id=%s validation_mismatches=%s",
            result.provider,
            result.underlying,
            result.expiry.isoformat(),
            result.fetched_count,
            result.stored_count,
            result.unusable_for_live_count,
            result.validation_status,
            result.validation_report_id,
            result.validation_mismatch_count,
        )
        return result

    def run_session(
        self,
        *,
        underlying: str,
        expiry: date,
        start_time: time = time(9, 15),
        end_time: time = time(15, 30),
        cadence_seconds: int = 60,
        max_snapshots: int | None = None,
    ) -> list[SnapshotResult]:
        """Run a bounded market-session loop.

        ``max_snapshots`` is primarily for tests and supervised one-shot jobs.
        Production services should set process-level supervision and shutdown.
        """
        results: list[SnapshotResult] = []
        cadence = max(1, int(cadence_seconds))
        while True:
            now = self.clock().astimezone(IST)
            if now.time() > end_time:
                break
            if now.time() >= start_time:
                results.append(
                    self.capture_once(
                        underlying=underlying,
                        expiry=expiry,
                        snapshot_ts=now,
                    )
                )
                if max_snapshots is not None and len(results) >= int(max_snapshots):
                    break
            self.sleep(_seconds_to_next_bucket(now, cadence))
        return results


def _minute_bucket(value: datetime) -> datetime:
    ts = value if value.tzinfo is not None else value.replace(tzinfo=IST)
    return ts.astimezone(IST).replace(second=0, microsecond=0)


def _seconds_to_next_bucket(now: datetime, cadence_seconds: int) -> float:
    elapsed = (now.minute * 60 + now.second) % max(1, int(cadence_seconds))
    wait = max(1, int(cadence_seconds) - elapsed)
    if now.microsecond:
        wait = max(1, wait - 1)
    return float(wait)


__all__ = [
    "IST",
    "OptionChainSnapshotValidator",
    "OiSnapshotter",
    "SnapshotResult",
]
