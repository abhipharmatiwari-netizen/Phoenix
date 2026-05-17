from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.data.option_chain_provider import OptionQuote
from app.strategies.oi_ml.dataset import (
    OiMlDatasetBuilder,
    OiMlDatasetConfig,
    select_candidate_quotes,
)
from app.strategies.oi_ml.labels import IntradayLabelConfig


DECISION_TS = datetime(2026, 5, 19, 4, 30, tzinfo=timezone.utc)  # 10:00 IST
EXPIRY = date(2026, 5, 19)


def _quote(
    strike: int,
    option_type: str,
    *,
    minute_offset: int = 0,
    ltp: float = 100.0,
    oi: int = 1000,
    spot: float = 25140.0,
    token: str | None = None,
) -> OptionQuote:
    ts = DECISION_TS + timedelta(minutes=minute_offset)
    return OptionQuote(
        snapshot_ts=ts,
        underlying="NIFTY",
        expiry=EXPIRY,
        strike=strike,
        option_type=option_type,
        trading_symbol=f"NIFTY19MAY26{strike}{option_type}",
        exchange="NFO",
        symbol_token=token or f"{strike}{option_type}",
        provider="angel",
        oi=oi,
        volume=1000,
        iv="12.0",
        bid=max(0.05, ltp - 0.5),
        ask=ltp + 0.5,
        ltp=ltp,
        underlying_ltp=spot,
    )


def test_select_candidate_quotes_filters_otm_side_premium_and_oi():
    snapshot = [
        _quote(25100, "CE", ltp=100.0, oi=1000),  # ITM for CE
        _quote(25200, "CE", ltp=80.0, oi=1000),
        _quote(25300, "CE", ltp=0.5, oi=1000),  # premium too low
        _quote(25400, "CE", ltp=60.0, oi=0),  # OI too low
        _quote(25100, "PE", ltp=75.0, oi=1000),
    ]

    candidates = select_candidate_quotes(
        snapshot,
        decision_ts=DECISION_TS,
        config=OiMlDatasetConfig(
            option_type="CE",
            min_premium=1.0,
            min_oi=1,
            min_otm_points=0.0,
        ),
    )

    assert [quote.strike for quote in candidates] == [25200]


def test_select_candidate_quotes_caps_by_nearest_otm_distance():
    snapshot = [
        _quote(25200, "CE", ltp=80.0, oi=1000),
        _quote(25300, "CE", ltp=70.0, oi=1000),
        _quote(25400, "CE", ltp=60.0, oi=1000),
    ]

    candidates = select_candidate_quotes(
        snapshot,
        decision_ts=DECISION_TS,
        config=OiMlDatasetConfig(
            option_type="CE",
            max_candidates_per_decision=2,
        ),
    )

    assert [quote.strike for quote in candidates] == [25200, 25300]


def test_select_candidate_quotes_rejects_future_feature_snapshot():
    with pytest.raises(ValueError, match="future quotes"):
        select_candidate_quotes(
            [_quote(25200, "CE", minute_offset=1)],
            decision_ts=DECISION_TS,
        )


class FakeRepository:
    def __init__(self, snapshot, windows):
        self.snapshot = snapshot
        self.windows = windows
        self.snapshot_calls = []
        self.window_calls = []

    def fetch_latest_snapshot(self, **kwargs):
        self.snapshot_calls.append(kwargs)
        return self.snapshot

    def fetch_candidate_window(self, **kwargs):
        self.window_calls.append(kwargs)
        return self.windows[(kwargs["strike"], kwargs["option_type"])]


def test_dataset_builder_joins_decision_features_with_intraday_labels():
    snapshot = [
        _quote(25100, "CE", ltp=100.0, oi=500),
        _quote(25200, "CE", ltp=100.0, oi=1000),
        _quote(25300, "CE", ltp=80.0, oi=400),
        _quote(25200, "PE", ltp=70.0, oi=600),
    ]
    windows = {
        (25200, "CE"): [
            _quote(25200, "CE", ltp=100.0, oi=1000),
            _quote(25200, "CE", minute_offset=6, ltp=49.0, oi=1000),
        ],
        (25300, "CE"): [
            _quote(25300, "CE", ltp=80.0, oi=400),
            _quote(25300, "CE", minute_offset=6, ltp=39.0, oi=400),
        ],
    }
    repo = FakeRepository(snapshot, windows)
    builder = OiMlDatasetBuilder(
        repo,
        config=OiMlDatasetConfig(
            option_type="CE",
            max_candidates_per_decision=1,
            label_config=IntradayLabelConfig(lot_size=65, fees_per_lot=0.0),
        ),
    )

    rows = builder.build_rows_for_decision(
        underlying="nifty",
        expiry=EXPIRY,
        decision_ts=DECISION_TS,
        provider="angel",
    )

    assert len(rows) == 1
    row = rows[0]
    as_dict = row.to_dict()
    assert row.strike == 25200
    assert as_dict["primary_label"] == 1
    assert as_dict["feature_candidate_oi"] == 1000
    assert as_dict["feature_pcr_total"] == pytest.approx(600 / 1900)
    assert as_dict["feature_oi_wall_present"] is True
    assert repo.snapshot_calls[0]["underlying"] == "nifty"
    assert repo.snapshot_calls[0]["decision_ts"] == DECISION_TS
    assert repo.snapshot_calls[0]["min_snapshot_ts"] == DECISION_TS - timedelta(seconds=120)
    assert repo.window_calls[0]["strike"] == 25200
    assert repo.window_calls[0]["provider"] == "angel"


def test_dataset_builder_skips_unlabelable_candidates_by_default():
    snapshot = [_quote(25200, "CE", ltp=100.0, oi=1000)]
    repo = FakeRepository(snapshot, {(25200, "CE"): []})

    rows = OiMlDatasetBuilder(repo).build_rows_for_decision(
        underlying="NIFTY",
        expiry=EXPIRY,
        decision_ts=DECISION_TS,
    )

    assert rows == []


def test_dataset_builder_can_raise_unlabelable_candidate_errors():
    snapshot = [_quote(25200, "CE", ltp=100.0, oi=1000)]
    repo = FakeRepository(snapshot, {(25200, "CE"): []})
    builder = OiMlDatasetBuilder(
        repo,
        config=OiMlDatasetConfig(skip_unlabelable=False),
    )

    with pytest.raises(ValueError, match="empty option-chain window"):
        builder.build_rows_for_decision(
            underlying="NIFTY",
            expiry=EXPIRY,
            decision_ts=DECISION_TS,
        )


def test_dataset_builder_handles_multiple_decision_times():
    snapshot = [_quote(25200, "CE", ltp=100.0, oi=1000)]
    windows = {
        (25200, "CE"): [
            _quote(25200, "CE", ltp=100.0, oi=1000),
            _quote(25200, "CE", minute_offset=6, ltp=49.0, oi=1000),
        ],
    }
    repo = FakeRepository(snapshot, windows)

    rows = OiMlDatasetBuilder(repo).build_rows_for_decisions(
        underlying="NIFTY",
        expiry=EXPIRY,
        decision_times=[DECISION_TS, DECISION_TS + timedelta(minutes=1)],
    )

    assert len(rows) == 2
    assert len(repo.snapshot_calls) == 2
