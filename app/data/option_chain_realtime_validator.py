"""Automatic NSE-vs-Angel option-chain validation for shadow ingestion."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
import logging
from typing import Any, Mapping, Protocol, Sequence

from app.data.option_chain_provider import OptionChainProvider, OptionQuote
from app.data.option_chain_validation import (
    OptionChainValidationConfig,
    compare_angel_to_nse,
    expected_non_equivalent_reference_fields,
    expected_missing_reference_fields,
    reference_contract_coverage_is_partial,
)
from app.data.option_chain_validation_store import (
    OptionChainValidationReportStore,
    StoredOptionChainValidationReport,
)


logger = logging.getLogger(__name__)


class OptionQuoteStore(Protocol):
    def upsert_quotes(self, quotes: Sequence[OptionQuote]) -> int:
        ...


@dataclass(frozen=True)
class RealtimeOptionChainValidationConfig:
    enabled: bool = False
    store_reference_quotes: bool = True
    log_all_observations: bool = True
    fail_on_error: bool = False
    source_error_window_size: int = 20
    source_error_rate_warn_threshold: float = 0.25
    validation_config: OptionChainValidationConfig = field(
        default_factory=OptionChainValidationConfig
    )


@dataclass(frozen=True)
class RealtimeOptionChainValidationResult:
    report_id: int | None
    status: str
    severity: str
    compared_contracts: int
    mismatch_count: int
    primary_only_count: int
    reference_only_count: int
    missing_primary_iv: int
    missing_reference_iv: int
    error: str | None = None


@dataclass
class RealtimeOptionChainValidator:
    """Validate each freshly captured Angel snapshot against NSE web data.

    This class is deliberately data-quality only. It does not create strategy
    candidates or order intents.
    """

    reference_provider: OptionChainProvider
    report_store: OptionChainValidationReportStore
    reference_quote_store: OptionQuoteStore | None = None
    config: RealtimeOptionChainValidationConfig = field(
        default_factory=RealtimeOptionChainValidationConfig
    )
    primary_provider_name: str = "angel"
    reference_provider_name: str = "nse_web"
    _source_error_window: deque[bool] = field(default_factory=deque, init=False, repr=False)

    def validate(
        self,
        *,
        primary_quotes: Sequence[OptionQuote],
        underlying: str,
        expiry: date,
        snapshot_ts: datetime,
    ) -> RealtimeOptionChainValidationResult | None:
        if not self.config.enabled:
            return None
        validation_ts = datetime.now(timezone.utc)
        normalized_underlying = str(underlying or "").strip().upper()
        try:
            reference_quotes = list(
                self.reference_provider.fetch_chain(
                    underlying=normalized_underlying,
                    expiry=expiry,
                    snapshot_ts=snapshot_ts,
                )
            )
            if primary_quotes and not reference_quotes:
                error = (
                    "nse_reference_quotes_empty: no comparable NSE rows "
                    f"underlying={normalized_underlying} expiry={expiry.isoformat()}"
                )
                logger.warning(
                    "oi_chain_validation reference feed empty underlying=%s expiry=%s "
                    "snapshot_ts=%s primary_quotes=%d",
                    normalized_underlying,
                    expiry.isoformat(),
                    snapshot_ts.isoformat(),
                    len(primary_quotes),
                )
                if self.config.fail_on_error:
                    raise RuntimeError(error)
                source_metrics = self._record_source_observation(failed=True)
                result = self._record_error(
                    underlying=normalized_underlying,
                    expiry=expiry,
                    snapshot_ts=snapshot_ts,
                    validation_ts=validation_ts,
                    error=error,
                    primary_quote_count=len(primary_quotes),
                    source_metrics=source_metrics,
                    error_classification="reference_quotes_empty",
                )
                return result
            source_metrics = self._record_source_observation(failed=False)
            stored_reference_rows = 0
            if self.config.store_reference_quotes and self.reference_quote_store is not None:
                stored_reference_rows = int(self.reference_quote_store.upsert_quotes(reference_quotes))

            skipped_reference_fields = expected_missing_reference_fields(reference_quotes)
            validation_config = self.config.validation_config
            if skipped_reference_fields:
                validation_config = replace(
                    validation_config,
                    skip_missing_reference_fields=tuple(
                        sorted(
                            set(validation_config.skip_missing_reference_fields)
                            | set(skipped_reference_fields)
                        )
                    ),
                )
            skipped_non_equivalent_fields = expected_non_equivalent_reference_fields(
                reference_quotes
            )
            if skipped_non_equivalent_fields:
                validation_config = replace(
                    validation_config,
                    skip_reference_fields=tuple(
                        sorted(
                            set(validation_config.skip_reference_fields)
                            | set(skipped_non_equivalent_fields)
                        )
                    ),
                )
            reference_coverage_partial = reference_contract_coverage_is_partial(
                reference_quotes
            )
            if reference_coverage_partial:
                validation_config = replace(
                    validation_config,
                    ignore_primary_only_contracts=True,
                )
            metadata = {
                "auto_realtime_validation": True,
                "validation_only": True,
                "snapshot_ts": snapshot_ts.isoformat(),
                "validation_ts": validation_ts.isoformat(),
                "primary_provider": self.primary_provider_name,
                "reference_provider": self.reference_provider_name,
                "primary_quote_count": len(primary_quotes),
                "reference_quote_count": len(reference_quotes),
                "stored_reference_rows": stored_reference_rows,
            }
            metadata.update(source_metrics)
            reference_sources = _reference_sources(reference_quotes)
            if reference_sources:
                metadata["reference_sources"] = reference_sources
            if skipped_reference_fields:
                metadata["skipped_missing_reference_fields"] = list(skipped_reference_fields)
            if skipped_non_equivalent_fields:
                metadata["skipped_non_equivalent_reference_fields"] = list(
                    skipped_non_equivalent_fields
                )
            if reference_coverage_partial:
                metadata["reference_contract_coverage"] = "partial"

            report = compare_angel_to_nse(
                list(primary_quotes),
                reference_quotes,
                config=validation_config,
                metadata=metadata,
            )
            payload = report.to_dict()
            status, severity = _status_and_severity(payload)
            stored = self.report_store.insert_report(
                payload=payload,
                validation_ts=validation_ts,
                snapshot_ts=snapshot_ts,
                underlying=normalized_underlying,
                expiry=expiry,
                primary_provider=self.primary_provider_name,
                reference_provider=self.reference_provider_name,
                status=status,
                severity=severity,
                primary_quote_count=len(primary_quotes),
                reference_quote_count=len(reference_quotes),
            )
            result = _result_from_payload(stored, payload)
            self._log_result(result, normalized_underlying, expiry, snapshot_ts)
            return result
        except Exception as exc:
            logger.exception(
                "oi_chain_validation failed underlying=%s expiry=%s snapshot_ts=%s error=%s",
                normalized_underlying,
                expiry.isoformat(),
                snapshot_ts.isoformat(),
                exc,
            )
            result = self._record_error(
                underlying=normalized_underlying,
                expiry=expiry,
                snapshot_ts=snapshot_ts,
                validation_ts=validation_ts,
                error=str(exc),
                primary_quote_count=len(primary_quotes),
                source_metrics=self._record_source_observation(failed=True),
                error_classification=classify_reference_validation_error(exc),
            )
            if self.config.fail_on_error:
                raise
            return result

    def _record_error(
        self,
        *,
        underlying: str,
        expiry: date,
        snapshot_ts: datetime,
        validation_ts: datetime,
        error: str,
        primary_quote_count: int,
        source_metrics: Mapping[str, Any] | None = None,
        error_classification: str | None = None,
    ) -> RealtimeOptionChainValidationResult:
        metadata = {
            "auto_realtime_validation": True,
            "validation_only": True,
            "snapshot_ts": snapshot_ts.isoformat(),
            "validation_ts": validation_ts.isoformat(),
            "primary_provider": self.primary_provider_name,
            "reference_provider": self.reference_provider_name,
            "primary_quote_count": primary_quote_count,
            "reference_quote_count": 0,
            "error": error,
        }
        if error_classification:
            metadata["error_classification"] = error_classification
        metadata.update(dict(source_metrics or {}))
        payload = {
            "underlying": underlying,
            "expiry": expiry.isoformat(),
            "ok": False,
            "compared_contracts": 0,
            "angel_only_contracts": [],
            "nse_only_contracts": [],
            "mismatches": [],
            "missing_angel_iv": 0,
            "missing_nse_iv": 0,
            "metadata": metadata,
        }
        try:
            stored = self.report_store.insert_report(
                payload=payload,
                validation_ts=validation_ts,
                snapshot_ts=snapshot_ts,
                underlying=underlying,
                expiry=expiry,
                primary_provider=self.primary_provider_name,
                reference_provider=self.reference_provider_name,
                status="ERROR",
                severity="ERROR",
                primary_quote_count=primary_quote_count,
                reference_quote_count=0,
            )
            return RealtimeOptionChainValidationResult(
                report_id=stored.report_id,
                status="ERROR",
                severity="ERROR",
                compared_contracts=0,
                mismatch_count=0,
                primary_only_count=0,
                reference_only_count=0,
                missing_primary_iv=0,
                missing_reference_iv=0,
                error=error,
            )
        except Exception:
            logger.exception(
                "oi_chain_validation failed to persist validation error underlying=%s expiry=%s",
                underlying,
                expiry.isoformat(),
            )
            return RealtimeOptionChainValidationResult(
                report_id=None,
                status="ERROR",
                severity="ERROR",
                compared_contracts=0,
                mismatch_count=0,
                primary_only_count=0,
                reference_only_count=0,
                missing_primary_iv=0,
                missing_reference_iv=0,
                error=error,
            )

    def _log_result(
        self,
        result: RealtimeOptionChainValidationResult,
        underlying: str,
        expiry: date,
        snapshot_ts: datetime,
    ) -> None:
        log = logger.info if result.severity == "INFO" else logger.warning
        if result.severity == "INFO" and not self.config.log_all_observations:
            return
        log(
            "oi_chain_validation observation status=%s severity=%s report_id=%s "
            "underlying=%s expiry=%s snapshot_ts=%s compared=%d mismatches=%d "
            "primary_only=%d reference_only=%d missing_primary_iv=%d missing_reference_iv=%d",
            result.status,
            result.severity,
            result.report_id,
            underlying,
            expiry.isoformat(),
            snapshot_ts.isoformat(),
            result.compared_contracts,
            result.mismatch_count,
            result.primary_only_count,
            result.reference_only_count,
            result.missing_primary_iv,
            result.missing_reference_iv,
        )

    def _record_source_observation(self, *, failed: bool) -> dict[str, Any]:
        max_size = max(1, int(self.config.source_error_window_size))
        while len(self._source_error_window) >= max_size:
            self._source_error_window.popleft()
        self._source_error_window.append(bool(failed))
        count = len(self._source_error_window)
        failures = sum(1 for item in self._source_error_window if item)
        rate = failures / count if count else 0.0
        threshold = max(0.0, float(self.config.source_error_rate_warn_threshold))
        return {
            "reference_error_window_size": max_size,
            "reference_error_window_count": count,
            "reference_error_count": failures,
            "reference_error_rate": round(rate, 4),
            "reference_error_rate_warn_threshold": threshold,
            "reference_error_rate_state": "breach" if rate >= threshold else "ok",
        }


def _status_and_severity(payload: Mapping[str, Any]) -> tuple[str, str]:
    if payload.get("ok") is True and not _has_missing_reference_iv(payload):
        return "OK", "INFO"
    if payload.get("metadata", {}).get("error"):
        return "ERROR", "ERROR"
    return "MISMATCH", "WARN"


def classify_reference_validation_error(exc: Exception) -> str:
    if isinstance(exc, TimeoutError):
        return "provider_timeout"
    message = str(exc or "").lower()
    if "timeout" in message or "timed out" in message:
        return "provider_timeout"
    if "login" in message or "auth" in message:
        return "provider_login_failed"
    return type(exc).__name__


def _reference_sources(quotes: Sequence[OptionQuote]) -> list[str]:
    sources: set[str] = set()
    for quote in quotes:
        flags = dict(getattr(quote, "quality_flags", None) or {})
        source = str(flags.get("nse_source") or "").strip()
        if source:
            sources.add(source)
    return sorted(sources)


def _has_missing_reference_iv(payload: Mapping[str, Any]) -> bool:
    return bool(payload.get("missing_nse_iv"))


def _result_from_payload(
    stored: StoredOptionChainValidationReport,
    payload: Mapping[str, Any],
) -> RealtimeOptionChainValidationResult:
    return RealtimeOptionChainValidationResult(
        report_id=stored.report_id,
        status=stored.status,
        severity=stored.severity,
        compared_contracts=int(payload.get("compared_contracts") or 0),
        mismatch_count=stored.mismatch_count,
        primary_only_count=stored.primary_only_count,
        reference_only_count=stored.reference_only_count,
        missing_primary_iv=int(payload.get("missing_angel_iv") or 0),
        missing_reference_iv=int(payload.get("missing_nse_iv") or 0),
    )


__all__ = [
    "RealtimeOptionChainValidationConfig",
    "RealtimeOptionChainValidationResult",
    "RealtimeOptionChainValidator",
    "classify_reference_validation_error",
]
