from __future__ import annotations

from datetime import date, datetime, timezone
import logging

from app.data.option_chain_provider import OptionQuote
from app.data.option_chain_realtime_validator import (
    RealtimeOptionChainValidationConfig,
    RealtimeOptionChainValidator,
)
from app.data.option_chain_validation_store import StoredOptionChainValidationReport


def _quote(**overrides):
    base = {
        "snapshot_ts": datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
        "source_ts": datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
        "underlying": "NIFTY",
        "expiry": date(2026, 5, 19),
        "strike": 23600,
        "option_type": "CE",
        "trading_symbol": "NIFTY19MAY2623600CE",
        "exchange": "NFO",
        "symbol_token": "23600CE",
        "provider": "angel",
        "oi": 1250,
        "volume": 3210,
        "iv": "11.75",
        "bid": "81.8",
        "ask": "82.7",
        "ltp": "82.4",
    }
    base.update(overrides)
    return OptionQuote(**base)


class FakeReferenceProvider:
    provider_name = "nse_web"

    def __init__(self, quotes=None, error=None):
        self.quotes = quotes or []
        self.error = error
        self.calls = []

    def fetch_chain(self, *, underlying, expiry, snapshot_ts):
        self.calls.append((underlying, expiry, snapshot_ts))
        if self.error:
            raise self.error
        return self.quotes


class FakeQuoteStore:
    def __init__(self):
        self.quotes = []

    def upsert_quotes(self, quotes):
        self.quotes.extend(quotes)
        return len(quotes)


class FakeReportStore:
    def __init__(self):
        self.calls = []

    def insert_report(self, **kwargs):
        self.calls.append(kwargs)
        payload = kwargs["payload"]
        return StoredOptionChainValidationReport(
            report_id=7,
            status=kwargs["status"],
            severity=kwargs["severity"],
            mismatch_count=len(payload.get("mismatches") or []),
            primary_only_count=len(payload.get("angel_only_contracts") or []),
            reference_only_count=len(payload.get("nse_only_contracts") or []),
        )


def test_realtime_validator_stores_nse_quotes_report_and_logs_observation(caplog):
    primary = [_quote()]
    reference = [_quote(provider="nse_web")]
    reference_store = FakeQuoteStore()
    report_store = FakeReportStore()
    validator = RealtimeOptionChainValidator(
        reference_provider=FakeReferenceProvider(reference),
        reference_quote_store=reference_store,
        report_store=report_store,
        config=RealtimeOptionChainValidationConfig(enabled=True),
    )

    with caplog.at_level(logging.INFO):
        result = validator.validate(
            primary_quotes=primary,
            underlying="nifty",
            expiry=date(2026, 5, 19),
            snapshot_ts=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
        )

    assert result is not None
    assert result.status == "OK"
    assert result.severity == "INFO"
    assert result.report_id == 7
    assert len(reference_store.quotes) == 1
    assert report_store.calls[0]["payload"]["metadata"]["auto_realtime_validation"] is True
    assert "oi_chain_validation observation status=OK" in caplog.text


def test_realtime_validator_warns_and_persists_mismatch(caplog):
    primary = [_quote()]
    reference = [_quote(provider="nse_web", oi=1400, ltp="90.0")]
    report_store = FakeReportStore()
    validator = RealtimeOptionChainValidator(
        reference_provider=FakeReferenceProvider(reference),
        report_store=report_store,
        config=RealtimeOptionChainValidationConfig(enabled=True),
    )

    with caplog.at_level(logging.WARNING):
        result = validator.validate(
            primary_quotes=primary,
            underlying="NIFTY",
            expiry=date(2026, 5, 19),
            snapshot_ts=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
        )

    assert result is not None
    assert result.status == "MISMATCH"
    assert result.severity == "WARN"
    assert result.mismatch_count == 1
    assert report_store.calls[0]["status"] == "MISMATCH"
    assert "mismatches=1" in caplog.text


def test_realtime_validator_skips_fallback_reference_fields_missing_by_design():
    primary = [_quote()]
    reference = [
        _quote(
            provider="nse_web",
            iv=None,
            bid=None,
            ask=None,
            quality_flags={
                "nse_source": "live_equity_derivatives",
                "missing_reference_fields_expected": ["ask", "bid", "iv"],
            },
        )
    ]
    report_store = FakeReportStore()
    validator = RealtimeOptionChainValidator(
        reference_provider=FakeReferenceProvider(reference),
        report_store=report_store,
        config=RealtimeOptionChainValidationConfig(enabled=True),
    )

    result = validator.validate(
        primary_quotes=primary,
        underlying="NIFTY",
        expiry=date(2026, 5, 19),
        snapshot_ts=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
    )

    assert result is not None
    assert result.status == "OK"
    assert result.severity == "INFO"
    metadata = report_store.calls[0]["payload"]["metadata"]
    assert metadata["reference_sources"] == ["live_equity_derivatives"]
    assert metadata["skipped_missing_reference_fields"] == ["ask", "bid", "iv"]


def test_realtime_validator_records_error_when_reference_feed_returns_no_rows():
    report_store = FakeReportStore()
    validator = RealtimeOptionChainValidator(
        reference_provider=FakeReferenceProvider([]),
        report_store=report_store,
        config=RealtimeOptionChainValidationConfig(enabled=True),
    )

    result = validator.validate(
        primary_quotes=[_quote()],
        underlying="NIFTY",
        expiry=date(2026, 5, 19),
        snapshot_ts=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
    )

    assert result is not None
    assert result.status == "ERROR"
    assert result.severity == "ERROR"
    assert result.error is not None
    assert "nse_reference_quotes_empty" in result.error
    assert report_store.calls[0]["reference_quote_count"] == 0
    assert report_store.calls[0]["payload"]["metadata"]["reference_quote_count"] == 0


def test_realtime_validator_records_error_without_raising_by_default():
    report_store = FakeReportStore()
    validator = RealtimeOptionChainValidator(
        reference_provider=FakeReferenceProvider(error=RuntimeError("nse timeout")),
        report_store=report_store,
        config=RealtimeOptionChainValidationConfig(enabled=True),
    )

    result = validator.validate(
        primary_quotes=[_quote()],
        underlying="NIFTY",
        expiry=date(2026, 5, 19),
        snapshot_ts=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
    )

    assert result is not None
    assert result.status == "ERROR"
    assert result.severity == "ERROR"
    assert result.error == "nse timeout"
    assert report_store.calls[0]["payload"]["metadata"]["error"] == "nse timeout"
