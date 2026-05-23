from __future__ import annotations

from datetime import date, datetime, time, timedelta

from app.data.option_chain_provider import OptionQuote
from app.data.option_chain_quality_gate import (
    IST,
    OptionChainProviderDecision,
    build_option_chain_quality_report,
)


SESSION_DATE = date(2026, 5, 19)


def _quote(minute: int, strike: int, option_type: str = "CE", **overrides):
    start = datetime.combine(SESSION_DATE, time(9, 15), tzinfo=IST)
    ts = start + timedelta(minutes=minute)
    base = {
        "snapshot_ts": ts,
        "source_ts": ts - timedelta(seconds=5),
        "ingested_at": ts + timedelta(seconds=1),
        "underlying": "NIFTY",
        "expiry": date(2026, 5, 19),
        "strike": strike,
        "option_type": option_type,
        "trading_symbol": f"NIFTY19MAY26{strike}{option_type}",
        "exchange": "NFO",
        "symbol_token": f"{strike}{option_type}",
        "provider": "angel",
        "oi": 120000,
        "volume": 1500,
        "iv": "11.25",
        "bid": "42.5",
        "ask": "43.0",
        "ltp": "42.8",
        "underlying_ltp": "25140.5",
        "vix": "12.4",
    }
    base.update(overrides)
    return OptionQuote(**base)


def _decision(**overrides):
    base = {
        "provider": "angel",
        "live_source": "Angel One FULL quote",
        "historical_source": "approved vendor backfill",
        "retention_months": 18,
        "production_feed_allowed": True,
        "expired_weeklies_available": True,
    }
    base.update(overrides)
    return OptionChainProviderDecision(**base)


def test_quality_report_passes_documented_coverage_and_completeness_gate():
    quotes = []
    for minute in range(0, 376):
        quotes.append(_quote(minute, 25200, "CE"))
        quotes.append(_quote(minute, 25200, "PE"))
        quotes.append(_quote(minute, 25300, "CE"))

    report = build_option_chain_quality_report(
        quotes,
        provider_decision=_decision(),
        underlying="NIFTY",
        session_date=SESSION_DATE,
        candidate_strikes=[25200, 25300],
        reconciliation_plan=("random-day broker terminal comparison",),
        stress_backfill_decision="Extend for 2024-06-04 and March 2020.",
    )

    assert report.passed is True
    assert report.trading_minute_coverage == 1.0
    assert report.candidate_field_completeness == 1.0
    assert report.to_dict()["provider_decision"]["approved"] is True


def test_quality_report_fails_when_candidate_hard_fields_are_missing():
    quotes = [_quote(0, 25200, iv=None), _quote(1, 25200)]

    report = build_option_chain_quality_report(
        quotes,
        provider_decision=_decision(),
        underlying="NIFTY",
        session_date=SESSION_DATE,
        candidate_strikes=[25200],
        reconciliation_plan=("random-day broker terminal comparison",),
        stress_backfill_decision="Extend for stress windows.",
    )

    assert report.passed is False
    assert report.missing_by_field["iv"] == 1


def test_quality_report_fails_if_expired_weekly_retention_is_not_proven():
    quotes = [_quote(minute, 25200) for minute in range(0, 376)]

    report = build_option_chain_quality_report(
        quotes,
        provider_decision=_decision(retention_months=12),
        underlying="NIFTY",
        session_date=SESSION_DATE,
        candidate_strikes=[25200],
        reconciliation_plan=("random-day broker terminal comparison",),
        stress_backfill_decision="Extend for stress windows.",
    )

    assert report.provider_decision.approved is False
    assert report.passed is False
