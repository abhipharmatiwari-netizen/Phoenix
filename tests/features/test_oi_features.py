from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.data.option_chain_provider import OptionQuote
from app.features.oi_features import (
    build_oi_features,
    detect_oi_wall,
    max_pain_strike,
    oi_concentration_ratio,
    strike_pcr,
    total_pcr,
)


SNAPSHOT_TS = datetime(2026, 5, 19, 10, 0, tzinfo=timezone.utc)


def _quote(strike: int, option_type: str, oi: int, **overrides) -> OptionQuote:
    base = {
        "snapshot_ts": SNAPSHOT_TS,
        "underlying": "NIFTY",
        "expiry": date(2026, 5, 19),
        "strike": strike,
        "option_type": option_type,
        "trading_symbol": f"NIFTY19MAY26{strike}{option_type}",
        "exchange": "NFO",
        "symbol_token": f"{strike}{option_type}",
        "provider": "unit",
        "oi": oi,
        "volume": 100,
        "iv": "12.0",
        "bid": "40.0",
        "ask": "41.0",
        "ltp": "40.5",
        "underlying_ltp": "25090",
    }
    base.update(overrides)
    return OptionQuote(**base)


def test_max_pain_uses_minimum_total_option_payout():
    quotes = [
        _quote(25000, "CE", 100),
        _quote(25100, "CE", 500),
        _quote(25200, "CE", 100),
        _quote(25000, "PE", 100),
        _quote(25100, "PE", 500),
        _quote(25200, "PE", 100),
    ]

    assert max_pain_strike(quotes) == 25100


def test_pcr_returns_none_when_call_oi_denominator_is_zero():
    quotes = [_quote(25100, "PE", 500)]

    assert total_pcr(quotes) is None
    assert strike_pcr(quotes, 25100) is None


def test_detect_oi_wall_finds_call_wall_at_or_above_candidate():
    quotes = [
        _quote(25100, "CE", 100),
        _quote(25200, "CE", 1000),
        _quote(25300, "CE", 100),
    ]

    wall = detect_oi_wall(quotes, candidate_strike=25100, option_type="CE")

    assert wall.present is True
    assert wall.strike == 25200
    assert wall.multiple == 10.0


def test_oi_concentration_ratio_uses_top_n_side_oi():
    quotes = [
        _quote(25100, "CE", 100),
        _quote(25200, "CE", 300),
        _quote(25300, "CE", 600),
        _quote(25100, "PE", 900),
    ]

    assert oi_concentration_ratio(quotes, option_type="CE", top_n=2) == 0.9


def test_build_oi_features_combines_core_candidate_metrics():
    quotes = [
        _quote(25100, "CE", 100),
        _quote(25200, "CE", 1000),
        _quote(25300, "CE", 100),
        _quote(25100, "PE", 600),
        _quote(25200, "PE", 300),
    ]

    features = build_oi_features(
        quotes,
        candidate_strike=25200,
        option_type="CE",
        decision_ts=SNAPSHOT_TS,
    )

    assert features["candidate_oi"] == 1000
    assert features["candidate_oi_share"] == pytest.approx(1000 / 1200)
    assert features["pcr_total"] == pytest.approx(900 / 1200)
    assert features["pcr_strike"] == pytest.approx(300 / 1000)
    assert features["oi_wall_present"] is True
    assert features["oi_wall_strike"] == 25200
    assert features["candidate_distance_pct"] == pytest.approx((25200 - 25090) / 25090)
    assert features["decision_ts"] == SNAPSHOT_TS.isoformat()
    assert features["candidate_bid_ask_spread"] == pytest.approx(1.0)
    assert features["candidate_missing_fields_count"] == 2  # source_ts/vix absent in fixture
    assert features["vix_regime"] is None


def test_build_oi_features_rejects_future_snapshot_rows():
    future = _quote(
        25200,
        "CE",
        1000,
        snapshot_ts=datetime(2026, 5, 19, 10, 1, tzinfo=timezone.utc),
    )

    with pytest.raises(ValueError, match="future snapshots"):
        build_oi_features([future], candidate_strike=25200, decision_ts=SNAPSHOT_TS)


def test_build_oi_features_adds_velocity_persistence_and_beta_lineage():
    previous = _quote(
        25200,
        "CE",
        800,
        snapshot_ts=datetime(2026, 5, 19, 9, 59, tzinfo=timezone.utc),
        source_ts=datetime(2026, 5, 19, 9, 58, 50, tzinfo=timezone.utc),
        ingested_at=datetime(2026, 5, 19, 9, 59, 5, tzinfo=timezone.utc),
        underlying_ltp="25080",
        vix="15.0",
    )
    latest = _quote(
        25200,
        "CE",
        1000,
        source_ts=datetime(2026, 5, 19, 9, 59, 50, tzinfo=timezone.utc),
        ingested_at=datetime(2026, 5, 19, 10, 0, 5, tzinfo=timezone.utc),
        underlying_ltp="25090",
        vix="16.0",
    )
    features = build_oi_features(
        [
            previous,
            _quote(25100, "CE", 100, snapshot_ts=previous.snapshot_ts),
            _quote(25300, "CE", 100, snapshot_ts=previous.snapshot_ts),
            latest,
            _quote(25100, "CE", 100),
            _quote(25300, "CE", 100),
        ],
        candidate_strike=25200,
        decision_ts=SNAPSHOT_TS,
    )

    assert features["candidate_oi_velocity_per_minute"] == pytest.approx(200.0)
    assert features["oi_wall_persistence_snapshots"] == 2
    assert features["oi_vs_spot_beta"] is not None
    assert features["max_source_ts"] == "2026-05-19T09:59:50+00:00"
    assert features["max_ingested_at"] == "2026-05-19T10:00:05+00:00"
    assert features["vix_regime"] == "NORMAL"
