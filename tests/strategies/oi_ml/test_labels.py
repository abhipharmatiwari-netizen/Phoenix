from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.data.option_chain_provider import OptionQuote
from app.strategies.oi_ml.labels import (
    IntradayExitReason,
    IntradayLabelConfig,
    label_candidate_from_repository,
    label_short_option_intraday,
)


DECISION_TS = datetime(2026, 5, 19, 4, 30, tzinfo=timezone.utc)  # 10:00 IST
EXPIRY = date(2026, 5, 19)


def _quote(
    minute_offset: int,
    *,
    ltp: float,
    bid: float | None = None,
    ask: float | None = None,
    spot: float = 25100.0,
) -> OptionQuote:
    ts = DECISION_TS + timedelta(minutes=minute_offset)
    return OptionQuote(
        snapshot_ts=ts,
        underlying="NIFTY",
        expiry=EXPIRY,
        strike=25200,
        option_type="CE",
        trading_symbol="NIFTY19MAY2625200CE",
        exchange="NFO",
        symbol_token="111",
        provider="unit",
        oi=120000,
        volume=1500,
        iv="11.25",
        bid=bid if bid is not None else max(0.05, ltp - 0.5),
        ask=ask if ask is not None else ltp + 0.5,
        ltp=ltp,
        underlying_ltp=spot,
    )


def _config(**overrides) -> IntradayLabelConfig:
    values = {
        "lot_size": 65,
        "fees_per_lot": 10.0,
    }
    values.update(overrides)
    return IntradayLabelConfig(**values)


def test_short_option_label_marks_take_profit_before_stop():
    label = label_short_option_intraday(
        [
            _quote(0, ltp=100.0, bid=99.0, ask=101.0),
            _quote(5, ltp=52.0, bid=51.5, ask=52.5),
            _quote(6, ltp=49.0, bid=48.5, ask=49.5),
        ],
        decision_ts=DECISION_TS,
        config=_config(),
    )

    assert label.exit_reason == IntradayExitReason.TAKE_PROFIT
    assert label.primary_label == 1
    assert label.profitable_label == 1
    assert label.pnl_per_lot == pytest.approx(((99.0 - 49.5) * 65) - 10.0)
    assert label.mae_premium == 0.0


def test_short_option_label_marks_premium_stop_first():
    label = label_short_option_intraday(
        [
            _quote(0, ltp=100.0, bid=99.0, ask=101.0),
            _quote(3, ltp=181.0, bid=180.0, ask=182.0),
            _quote(6, ltp=49.0, bid=48.5, ask=49.5),
        ],
        decision_ts=DECISION_TS,
        config=_config(),
    )

    assert label.exit_reason == IntradayExitReason.PREMIUM_STOP
    assert label.primary_label == 0
    assert label.profitable_label == 0
    assert label.pnl_per_lot == pytest.approx(((99.0 - 182.0) * 65) - 10.0)
    assert label.mae_premium == pytest.approx(81.0)
    assert label.mae_multiple == pytest.approx(1.81)


def test_short_option_label_exits_at_eod_when_no_threshold_hits():
    label = label_short_option_intraday(
        [
            _quote(0, ltp=100.0, bid=99.0, ask=101.0),
            _quote(30, ltp=91.0, bid=90.5, ask=91.5),
            _quote(320, ltp=88.0, bid=87.5, ask=88.5),
        ],
        decision_ts=DECISION_TS,
        config=_config(),
    )

    assert label.exit_reason == IntradayExitReason.EOD
    assert label.exit_ts == DECISION_TS + timedelta(minutes=320)
    assert label.pnl_per_lot == pytest.approx(((99.0 - 88.5) * 65) - 10.0)


def test_short_option_label_marks_no_followup_with_quality_flag():
    label = label_short_option_intraday(
        [_quote(0, ltp=100.0, bid=99.0, ask=101.0)],
        decision_ts=DECISION_TS,
        config=_config(),
    )

    assert label.exit_reason == IntradayExitReason.NO_FOLLOWUP
    assert "missing_followup_quotes" in label.quality_flags
    assert label.pnl_per_lot == pytest.approx(((99.0 - 101.0) * 65) - 10.0)


def test_short_option_label_rejects_missing_entry_quote_at_or_before_decision():
    with pytest.raises(ValueError, match="missing entry quote"):
        label_short_option_intraday(
            [_quote(1, ltp=100.0)],
            decision_ts=DECISION_TS,
            config=_config(),
        )


def test_short_option_label_rejects_stale_entry_quote():
    with pytest.raises(ValueError, match="stale"):
        label_short_option_intraday(
            [_quote(-5, ltp=100.0), _quote(1, ltp=99.0)],
            decision_ts=DECISION_TS,
            config=_config(max_entry_quote_age_seconds=120),
        )


def test_short_option_label_rejects_rows_past_intraday_deadline():
    with pytest.raises(ValueError, match="intraday deadline"):
        label_short_option_intraday(
            [
                _quote(0, ltp=100.0),
                _quote(351, ltp=99.0),  # 15:51 IST, beyond 15:20 deadline
            ],
            decision_ts=DECISION_TS,
            config=_config(),
        )


def test_short_option_label_can_use_spot_stop_when_configured():
    label = label_short_option_intraday(
        [
            _quote(0, ltp=100.0, bid=99.0, ask=101.0, spot=25100.0),
            _quote(5, ltp=110.0, bid=109.0, ask=111.0, spot=25195.0),
        ],
        decision_ts=DECISION_TS,
        config=_config(spot_stop_buffer_points=10.0),
    )

    assert label.exit_reason == IntradayExitReason.SPOT_STOP
    assert label.primary_label == 0


class FakeRepository:
    def __init__(self, quotes):
        self.quotes = quotes
        self.calls = []

    def fetch_candidate_window(self, **kwargs):
        self.calls.append(kwargs)
        return self.quotes


def test_label_candidate_from_repository_fetches_entry_lookback_to_eod_window():
    repo = FakeRepository(
        [
            _quote(0, ltp=100.0, bid=99.0, ask=101.0),
            _quote(6, ltp=49.0, bid=48.5, ask=49.5),
        ]
    )

    label = label_candidate_from_repository(
        repo,
        underlying="nifty",
        expiry=EXPIRY,
        strike=25200,
        option_type="ce",
        decision_ts=DECISION_TS,
        provider="angel",
        config=_config(max_entry_quote_age_seconds=180),
    )

    assert label.exit_reason == IntradayExitReason.TAKE_PROFIT
    call = repo.calls[0]
    assert call["underlying"] == "nifty"
    assert call["option_type"] == "CE"
    assert call["start_ts"] == DECISION_TS - timedelta(seconds=180)
    assert call["end_ts"] == datetime(2026, 5, 19, 9, 50, tzinfo=timezone.utc)
    assert call["provider"] == "angel"


def test_training_row_contains_primary_regression_and_tail_labels():
    label = label_short_option_intraday(
        [
            _quote(0, ltp=100.0, bid=99.0, ask=101.0),
            _quote(3, ltp=181.0, bid=180.0, ask=182.0),
        ],
        decision_ts=DECISION_TS,
        config=_config(),
    )

    row = label.to_training_row()

    assert row["primary_label"] == 0
    assert row["pnl_per_lot"] == label.pnl_per_lot
    assert row["mae_premium"] == label.tail_label
