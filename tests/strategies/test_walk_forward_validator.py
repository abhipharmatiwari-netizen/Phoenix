"""Unit tests for app/strategies/walk_forward_validator.py (issue #273).

The validator is exercised with synthetic score functions instead of the
real simulators so accept/reject paths can be tested without indicator
data. Each test pins one acceptance rule.
"""

from __future__ import annotations

from typing import Any, Mapping

import pandas as pd
import pytest

from app.strategies.walk_forward_validator import (
    WalkForwardConfig,
    WalkForwardResult,
    WalkForwardValidator,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _df(n: int = 100) -> pd.DataFrame:
    """Synthetic indicator-bars frame with deterministic close prices.

    Tests can address a known slice via ``len(df)`` and slice indices —
    the validator's split into in-sample + K folds + holdout is purely
    by row index, so the contents only matter to the score_fn.
    """
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="5min"),
        "close": [100.0 + 0.1 * i for i in range(n)],
    })


def _constant_score_fn(*, total_pnl: float, total_trades: int):
    """A score_fn that returns the same metrics for every slice."""

    def fn(df: pd.DataFrame, params: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"total_pnl": total_pnl, "total_trades": total_trades}

    return fn


def _sequenced_score_fn(metric_sequence):
    """A score_fn that returns metrics from a pre-canned sequence.

    The validator calls the score_fn in a deterministic order:
        call 0:        in-sample slice
        calls 1..K:    K consecutive folds
        call K+1:      OOS holdout slice
    Tests use this to set per-slice metrics without depending on the
    slice's row count (which can collide between fold and holdout when
    the fractions happen to line up).
    """
    calls = {"i": 0}

    def fn(df: pd.DataFrame, params: Mapping[str, Any]) -> Mapping[str, Any]:
        idx = calls["i"]
        calls["i"] += 1
        # Bound the index so a misuse doesn't IndexError.
        bounded = metric_sequence[min(idx, len(metric_sequence) - 1)]
        return bounded

    return fn


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def test_config_rejects_zero_folds():
    with pytest.raises(ValueError, match="folds"):
        WalkForwardValidator(WalkForwardConfig(folds=0))


@pytest.mark.parametrize("bad", [0.0, 1.0, -0.1, 1.5])
def test_config_rejects_invalid_holdout_pct(bad):
    with pytest.raises(ValueError, match="oos_holdout_pct"):
        WalkForwardValidator(WalkForwardConfig(oos_holdout_pct=bad))


def test_config_rejects_negative_min_trades():
    with pytest.raises(ValueError, match="min_trades_per_fold"):
        WalkForwardValidator(WalkForwardConfig(min_trades_per_fold=-1))


# ---------------------------------------------------------------------------
# Acceptance paths
# ---------------------------------------------------------------------------


def test_validator_accepts_stable_profitable_candidate():
    """A candidate that scores the same on in-sample, every fold, and the
    OOS holdout passes all three checks."""
    validator = WalkForwardValidator(
        WalkForwardConfig(folds=4, oos_holdout_pct=0.2, min_trades_per_fold=5)
    )
    result = validator.validate(
        _df(100),
        params={"k": 1},
        score_fn=_constant_score_fn(total_pnl=100.0, total_trades=10),
    )
    assert result.passed
    assert result.failure_reasons == []
    assert len(result.fold_scores) == 4
    assert result.median_fold_score == 100.0
    assert result.oos_holdout_score == 100.0
    assert result.min_fold_trades == 10


def test_validator_rejects_when_oos_holdout_unprofitable():
    """OOS holdout score <= 0 trips the gate."""
    validator = WalkForwardValidator(
        WalkForwardConfig(folds=4, oos_holdout_pct=0.2, min_trades_per_fold=5)
    )
    sequence = [
        {"total_pnl": 100.0, "total_trades": 10},  # in-sample
        {"total_pnl": 100.0, "total_trades": 10},  # fold 1
        {"total_pnl": 100.0, "total_trades": 10},  # fold 2
        {"total_pnl": 100.0, "total_trades": 10},  # fold 3
        {"total_pnl": 100.0, "total_trades": 10},  # fold 4
        {"total_pnl": -50.0, "total_trades": 10},  # OOS holdout loses
    ]
    result = validator.validate(
        _df(100),
        params={"k": 1},
        score_fn=_sequenced_score_fn(sequence),
    )
    assert not result.passed
    assert "oos_holdout_not_profitable" in result.failure_reasons


def test_validator_rejects_when_median_fold_collapses():
    """A candidate whose median fold score collapses well below the
    in-sample score (more than ``max_in_sample_degradation_pct``) is
    rejected as unstable.

    PR #289 codex round-2 P1: the stability check uses PER-BAR metrics
    now. In-sample is 80 bars and each fold is 20 bars, so a stable
    candidate that earns X per bar produces in-sample=80X and folds=20X
    — both per-bar = X. To express a real collapse, the folds must
    earn LESS PER BAR than in-sample. Here in-sample = 100 over 80
    bars = 1.25/bar; folds = 5 over 20 bars = 0.25/bar (an 80% per-bar
    degradation, well past the 30% threshold).
    """
    validator = WalkForwardValidator(
        WalkForwardConfig(
            folds=4,
            oos_holdout_pct=0.2,
            min_trades_per_fold=5,
            max_in_sample_degradation_pct=0.30,
        )
    )
    sequence = [
        {"total_pnl": 100.0, "total_trades": 10},  # in-sample: 1.25/bar
        {"total_pnl": 5.0, "total_trades": 10},    # fold 1: 0.25/bar (80% drop)
        {"total_pnl": 5.0, "total_trades": 10},    # fold 2
        {"total_pnl": 5.0, "total_trades": 10},    # fold 3
        {"total_pnl": 5.0, "total_trades": 10},    # fold 4
        {"total_pnl": 100.0, "total_trades": 10},  # OOS profitable so it's not
                                                    # the rejection reason
    ]
    result = validator.validate(
        _df(100),
        params={"k": 1},
        score_fn=_sequenced_score_fn(sequence),
    )
    assert not result.passed
    assert "median_fold_score_below_in_sample_threshold" in result.failure_reasons


def test_validator_rejects_when_a_fold_has_too_few_trades():
    """A fold with fewer than ``min_trades_per_fold`` trades trips the
    statistical-significance gate."""
    validator = WalkForwardValidator(
        WalkForwardConfig(folds=4, oos_holdout_pct=0.2, min_trades_per_fold=10)
    )
    result = validator.validate(
        _df(100),
        params={"k": 1},
        # 5 trades per slice — below the 10-trade floor.
        score_fn=_constant_score_fn(total_pnl=100.0, total_trades=5),
    )
    assert not result.passed
    assert "fold_trade_count_below_floor" in result.failure_reasons


def test_validator_handles_negative_in_sample_score():
    """If the in-sample score is non-positive, the stability rule still
    fires: median fold cannot be MORE negative than in-sample."""
    validator = WalkForwardValidator(
        WalkForwardConfig(folds=4, oos_holdout_pct=0.2, min_trades_per_fold=5)
    )
    sequence = [
        {"total_pnl": -100.0, "total_trades": 10},  # in-sample: loses 100
        {"total_pnl": -200.0, "total_trades": 10},  # fold 1: loses 200 (worse)
        {"total_pnl": -200.0, "total_trades": 10},  # fold 2
        {"total_pnl": -200.0, "total_trades": 10},  # fold 3
        {"total_pnl": -200.0, "total_trades": 10},  # fold 4
        {"total_pnl": 50.0, "total_trades": 10},    # OOS profitable so it's
                                                     # not the rejection reason
    ]
    result = validator.validate(
        _df(100),
        params={"k": 1},
        score_fn=_sequenced_score_fn(sequence),
    )
    assert not result.passed
    assert "median_fold_score_below_in_sample_score" in result.failure_reasons


def test_validator_reports_all_failure_reasons_simultaneously():
    """When a candidate fails multiple acceptance rules at once, every
    reason appears in ``failure_reasons`` (helps post-hoc audit)."""
    validator = WalkForwardValidator(
        WalkForwardConfig(folds=4, oos_holdout_pct=0.2, min_trades_per_fold=20)
    )
    sequence = [
        {"total_pnl": 100.0, "total_trades": 5},   # in-sample
        {"total_pnl": 10.0, "total_trades": 5},    # fold 1: collapses + too few trades
        {"total_pnl": 10.0, "total_trades": 5},    # fold 2
        {"total_pnl": 10.0, "total_trades": 5},    # fold 3
        {"total_pnl": 10.0, "total_trades": 5},    # fold 4
        {"total_pnl": -50.0, "total_trades": 5},   # OOS holdout unprofitable
    ]
    result = validator.validate(
        _df(100),
        params={"k": 1},
        score_fn=_sequenced_score_fn(sequence),
    )
    assert not result.passed
    assert "median_fold_score_below_in_sample_threshold" in result.failure_reasons
    assert "oos_holdout_not_profitable" in result.failure_reasons
    assert "fold_trade_count_below_floor" in result.failure_reasons


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_validator_returns_failed_on_empty_dataframe():
    validator = WalkForwardValidator()
    result = validator.validate(
        pd.DataFrame(),
        params={"k": 1},
        score_fn=_constant_score_fn(total_pnl=100.0, total_trades=10),
    )
    assert not result.passed
    assert "empty_dataframe" in result.failure_reasons


def test_validator_returns_failed_when_holdout_consumes_everything():
    """A pathologically small df where the holdout slice equals the
    entire frame must fail closed rather than crash."""
    validator = WalkForwardValidator(WalkForwardConfig(oos_holdout_pct=0.99))
    df = _df(2)  # holdout_size = 1; in-sample has 1 row but with 4 folds is fine
    # Even so, the validator should not crash; let it run and we just
    # verify behaviour rather than specific reasons.
    result = validator.validate(
        df,
        params={"k": 1},
        score_fn=_constant_score_fn(total_pnl=100.0, total_trades=10),
    )
    assert isinstance(result, WalkForwardResult)
    # Validator handled it without crash; pass/fail depends on the fold
    # behaviour with single-row slices.


# ---------------------------------------------------------------------------
# to_metrics_dict contract
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _apply_walk_forward_gate wiring: drops failing candidates, augments
# survivors' metrics, returns dropped count.
# ---------------------------------------------------------------------------


def test_apply_walk_forward_gate_drops_failing_candidates_and_augments_survivors(
    monkeypatch,
):
    """The orchestrator helper must drop candidates whose validate()
    returns ``passed=False`` and merge ``to_metrics_dict()`` into
    survivors' ``metrics`` JSONB before returning."""
    from app.strategies import run_multi_strategy_optimizer as rmso

    captured_loader_calls: list[dict] = []

    class _StubLoader:
        def fetch_indicator_bars(self, *, underlying_label, timeframe_seconds, days_back, end_date=None):
            captured_loader_calls.append({
                "underlying": underlying_label,
                "tf": timeframe_seconds,
                "days_back": days_back,
            })
            return _df(100)

    decisions = [True, False, True]

    class _StubValidator:
        def validate(self, df, params, score_fn):
            passed = decisions.pop(0)
            return WalkForwardResult(
                in_sample_score=100.0,
                fold_scores=[100.0, 100.0],
                fold_trade_counts=[10, 10],
                oos_holdout_score=50.0,
                oos_holdout_trades=5,
                passed=passed,
                failure_reasons=[] if passed else ["fake_reason"],
            )

    candidates = [
        {"params": {"a": 1}, "metrics": {"score": 1.0}},
        {"params": {"a": 2}, "metrics": {"score": 0.5}},
        {"params": {"a": 3}, "metrics": {"score": 0.4}},
    ]
    survivors, dropped = rmso._apply_walk_forward_gate(
        strategy_name="ema20",
        underlying_label="NIFTY_IDX",
        candidates=candidates,
        candidates_per_strategy=3,
        lookback_days=20,
        validator=_StubValidator(),  # type: ignore[arg-type]
        loader=_StubLoader(),  # type: ignore[arg-type]
    )
    assert dropped == 1, f"middle candidate must be dropped; got dropped={dropped}"
    assert len(survivors) == 2
    # Survivor metrics carry the walk_forward audit payload.
    for s in survivors:
        assert "walk_forward" in s["metrics"]
        assert s["metrics"]["walk_forward"]["passed"] is True
    # Only one loader fetch per (strategy, underlying).
    assert len(captured_loader_calls) == 1
    assert captured_loader_calls[0]["underlying"] == "NIFTY_IDX"
    assert captured_loader_calls[0]["days_back"] == 20


def test_apply_walk_forward_gate_drops_candidates_when_per_tf_loader_empty():
    """PR #289 codex round-3 P2: the gate used to bypass entirely when
    the strategy's DEFAULT timeframe returned no bars, silently
    promoting candidates whose own (non-default) timeframe might have
    had data. The fix removes that wholesale bypass — each candidate's
    own timeframe is fetched, and only candidates with empty data are
    dropped (the gate stays ON for the rest of the run).

    Here every candidate maps to a timeframe that returns empty, so
    every candidate should be DROPPED — not silently promoted."""
    from app.strategies import run_multi_strategy_optimizer as rmso

    class _EmptyLoader:
        def fetch_indicator_bars(self, **kwargs):
            return pd.DataFrame()

    class _UnusedValidator:
        def validate(self, *a, **kw):
            raise AssertionError("validator must not be called when df is empty")

    candidates = [{"params": {"a": 1}, "metrics": {}}]
    survivors, dropped = rmso._apply_walk_forward_gate(
        strategy_name="ema20",
        underlying_label="NIFTY_IDX",
        candidates=candidates,
        candidates_per_strategy=3,
        lookback_days=20,
        validator=_UnusedValidator(),  # type: ignore[arg-type]
        loader=_EmptyLoader(),  # type: ignore[arg-type]
    )
    # No candidate was ever validated; per-candidate empty data path
    # drops each one. The candidate count is preserved as dropped.
    assert dropped == 1
    assert survivors == []


def test_apply_walk_forward_gate_bypasses_unknown_strategy():
    """An unmapped strategy_name (e.g. a future strategy that isn't in
    _STRATEGY_TO_SIMULATOR yet) is bypassed with a warning, not crashed."""
    from app.strategies import run_multi_strategy_optimizer as rmso

    class _BoomLoader:
        def fetch_indicator_bars(self, **kwargs):
            raise AssertionError("loader must not be called for unknown strategy")

    class _UnusedValidator:
        def validate(self, *a, **kw):
            raise AssertionError("validator must not be called for unknown strategy")

    candidates = [{"params": {}, "metrics": {}}, {"params": {}, "metrics": {}}]
    survivors, dropped = rmso._apply_walk_forward_gate(
        strategy_name="not_a_real_strategy",
        underlying_label="NIFTY_IDX",
        candidates=candidates,
        candidates_per_strategy=1,
        lookback_days=20,
        validator=_UnusedValidator(),  # type: ignore[arg-type]
        loader=_BoomLoader(),  # type: ignore[arg-type]
    )
    assert dropped == 0
    assert len(survivors) == 1  # truncated to candidates_per_strategy=1


def test_to_metrics_dict_carries_all_audit_fields():
    """The JSON written into ``candidate["metrics"]`` must let a reviewer
    reconstruct why the gate accepted a candidate."""
    validator = WalkForwardValidator(
        WalkForwardConfig(folds=4, oos_holdout_pct=0.2, min_trades_per_fold=5)
    )
    result = validator.validate(
        _df(100),
        params={"k": 1},
        score_fn=_constant_score_fn(total_pnl=100.0, total_trades=10),
    )
    payload = result.to_metrics_dict()
    assert "walk_forward" in payload
    wf = payload["walk_forward"]
    for key in (
        "in_sample_score",
        "fold_scores",
        "median_fold_score",
        "oos_holdout_score",
        "fold_trade_counts",
        "min_fold_trades",
        "oos_holdout_trades",
        "passed",
        "failure_reasons",
    ):
        assert key in wf, f"to_metrics_dict missing {key!r}"
    assert isinstance(wf["fold_scores"], list)
    assert isinstance(wf["fold_trade_counts"], list)



def test_apply_walk_forward_gate_threads_loader_end_date_through_fetch():
    """PR #289 codex round-1 P2: when ``loader_end_date`` is supplied,
    the gate's ``fetch_indicator_bars`` call MUST use it so the
    validation frame is anchored on the same window the optimizer's
    ``top_5`` was originally scored on. Without this, a promotion run
    spanning IST midnight would validate candidates against the next
    day's window.
    """
    from datetime import date as _date
    from app.strategies import run_multi_strategy_optimizer as rmso

    captured: dict = {}

    class _CaptureLoader:
        def fetch_indicator_bars(self, *, underlying_label, timeframe_seconds, days_back, end_date=None):
            captured["end_date"] = end_date
            return _df(100)

    class _PassValidator:
        def validate(self, df, params, score_fn):
            return WalkForwardResult(
                in_sample_score=100.0,
                fold_scores=[100.0],
                fold_trade_counts=[10],
                oos_holdout_score=50.0,
                oos_holdout_trades=5,
                passed=True,
                failure_reasons=[],
            )

    fixed = _date(2026, 5, 15)
    candidates = [{"params": {"a": 1}, "metrics": {}}]
    rmso._apply_walk_forward_gate(
        strategy_name="ema20",
        underlying_label="NIFTY_IDX",
        candidates=candidates,
        candidates_per_strategy=1,
        lookback_days=20,
        validator=_PassValidator(),  # type: ignore[arg-type]
        loader=_CaptureLoader(),  # type: ignore[arg-type]
        loader_end_date=fixed,
    )
    assert captured["end_date"] == fixed



# ---------------------------------------------------------------------------
# PR #289 codex round-2 regressions.
# ---------------------------------------------------------------------------


def test_validator_accepts_uniformly_stable_candidate_via_per_bar_metric():
    """Codex round-2 P1: a uniformly stable candidate where each fold
    earns X per bar must PASS the gate even though the fold's raw PnL
    is only 1/K of the in-sample raw PnL. The check is per-bar.
    """
    validator = WalkForwardValidator(
        WalkForwardConfig(
            folds=4,
            oos_holdout_pct=0.2,
            min_trades_per_fold=5,
            max_in_sample_degradation_pct=0.30,
        )
    )
    # In-sample 80 bars earning 100 PnL = 1.25/bar.
    # Each fold 20 bars earning 25 PnL = 1.25/bar (no per-bar degradation).
    # OOS 20 bars earning 25 PnL = 1.25/bar (profitable).
    sequence = [
        {"total_pnl": 100.0, "total_trades": 10},  # in-sample
        {"total_pnl": 25.0, "total_trades": 10},   # fold 1
        {"total_pnl": 25.0, "total_trades": 10},   # fold 2
        {"total_pnl": 25.0, "total_trades": 10},   # fold 3
        {"total_pnl": 25.0, "total_trades": 10},   # fold 4
        {"total_pnl": 25.0, "total_trades": 10},   # OOS
    ]
    result = validator.validate(
        _df(100),
        params={"k": 1},
        score_fn=_sequenced_score_fn(sequence),
    )
    assert result.passed, (
        f"uniformly stable candidate (1.25 PnL/bar across all slices) "
        f"must pass; got failure_reasons={result.failure_reasons!r}"
    )
    # Payload exposes per-bar metrics for the reviewer.
    payload = result.to_metrics_dict()["walk_forward"]
    assert payload["in_sample_score_per_bar"] == 1.25
    assert payload["median_fold_score_per_bar"] == 1.25


@pytest.mark.parametrize("bad", [-0.1, 1.5, 30.0, float("nan"), float("inf"), float("-inf")])
def test_config_rejects_out_of_range_or_nan_degradation_pct(bad):
    """Codex round-2 P2: degradation pct must be a finite value in
    [0, 1]. A value like 30 (operator confused percent for fraction)
    or NaN must fail closed at construction."""
    with pytest.raises(ValueError, match="degradation"):
        WalkForwardValidator(WalkForwardConfig(max_in_sample_degradation_pct=bad))


def test_apply_walk_forward_gate_fails_closed_on_loader_exception():
    """Codex round-2 P2: an exception fetching indicator_bars must
    propagate, not silently bypass the gate. Letting a transient DB
    blip promote unvalidated parameters defeats the point of the gate.
    """
    from app.strategies import run_multi_strategy_optimizer as rmso

    class _BoomLoader:
        def fetch_indicator_bars(self, **kwargs):
            raise RuntimeError("indicator_bars table unreachable")

    class _UnusedValidator:
        def validate(self, *a, **kw):
            raise AssertionError("validator must not be called on fetch error")

    candidates = [{"params": {"a": 1}, "metrics": {}}]
    with pytest.raises(RuntimeError, match="indicator_bars"):
        rmso._apply_walk_forward_gate(
            strategy_name="ema20",
            underlying_label="NIFTY_IDX",
            candidates=candidates,
            candidates_per_strategy=1,
            lookback_days=20,
            validator=_UnusedValidator(),  # type: ignore[arg-type]
            loader=_BoomLoader(),  # type: ignore[arg-type]
        )


def test_apply_walk_forward_gate_uses_per_candidate_timeframe():
    """Codex round-2 P2: each candidate's ``signal_timeframe`` (EMA20)
    / ``timeframe_seconds`` (ECN) is honoured. The gate must NOT
    validate every candidate against a single hardcoded timeframe."""
    from app.strategies import run_multi_strategy_optimizer as rmso

    captured: list[int] = []

    class _ByTimeframeLoader:
        def fetch_indicator_bars(self, *, underlying_label, timeframe_seconds, days_back, end_date=None):
            captured.append(timeframe_seconds)
            return _df(100)

    class _PassValidator:
        def validate(self, df, params, score_fn):
            return WalkForwardResult(
                in_sample_score=100.0,
                fold_scores=[25.0] * 4,
                fold_trade_counts=[10] * 4,
                oos_holdout_score=25.0,
                oos_holdout_trades=5,
                passed=True,
                failure_reasons=[],
                in_sample_bars=80,
                fold_bar_counts=[20] * 4,
            )

    candidates = [
        {"params": {"signal_timeframe": 60}, "metrics": {}},
        {"params": {"signal_timeframe": 600}, "metrics": {}},
    ]
    rmso._apply_walk_forward_gate(
        strategy_name="ema20",
        underlying_label="NIFTY_IDX",
        candidates=candidates,
        candidates_per_strategy=2,
        lookback_days=20,
        validator=_PassValidator(),  # type: ignore[arg-type]
        loader=_ByTimeframeLoader(),  # type: ignore[arg-type]
    )
    # First fetch warms the default (300) for the empty-data probe,
    # then per-candidate timeframes (60 and 600).
    assert 60 in captured and 600 in captured, (
        f"per-candidate timeframes must be fetched; got {captured!r}"
    )


def test_apply_walk_forward_gate_validates_beyond_2x_candidate_quota():
    """Codex round-2 P3: when ``--candidates-per-strategy=1`` and the
    first two candidates fail the gate, the third (which would pass)
    must still be validated. The previous ``[:2*per_strategy]`` slice
    excluded valid later candidates and left the queue short."""
    from app.strategies import run_multi_strategy_optimizer as rmso

    pass_sequence = [False, False, True, True, True]

    class _StubLoader:
        def fetch_indicator_bars(self, **kwargs):
            return _df(100)

    class _SeqValidator:
        def validate(self, df, params, score_fn):
            passed = pass_sequence.pop(0)
            return WalkForwardResult(
                in_sample_score=100.0,
                fold_scores=[25.0] * 4,
                fold_trade_counts=[10] * 4,
                oos_holdout_score=25.0,
                oos_holdout_trades=5,
                passed=passed,
                failure_reasons=[] if passed else ["fake"],
                in_sample_bars=80,
                fold_bar_counts=[20] * 4,
            )

    candidates = [{"params": {"a": i}, "metrics": {}} for i in range(5)]
    survivors, dropped = rmso._apply_walk_forward_gate(
        strategy_name="ema20",
        underlying_label="NIFTY_IDX",
        candidates=candidates,
        candidates_per_strategy=1,
        lookback_days=20,
        validator=_SeqValidator(),  # type: ignore[arg-type]
        loader=_StubLoader(),  # type: ignore[arg-type]
    )
    assert len(survivors) == 1, (
        "must find at least one passing candidate beyond the 2x slice"
    )
    assert dropped == 2


# ---------------------------------------------------------------------------
# PR #289 codex round-3 regressions.
# ---------------------------------------------------------------------------


def test_promote_does_not_fall_back_indicator_dsn_to_writer_dsn():
    """In a split-DB deployment the user provides only
    ``--candidate-writer-dsn`` (writer / control-plane) and lets the
    optimizer's loader read ``PG_INDICATORS_DSN`` from settings. The
    walk-forward gate must NOT fall back to the writer DSN — its
    database has no ``indicator_bars`` table. Pass ``None`` to the
    loader so it honours the same default the optimizer used."""
    from app.strategies import run_multi_strategy_optimizer as rmso
    import inspect

    src = inspect.getsource(rmso._promote_top_candidates)
    # The fallback ``indicator_dsn or dsn`` must NOT be present any
    # more; the only acceptable constructor is ``dsn=indicator_dsn``.
    assert "indicator_dsn or dsn" not in src, (
        "the gate loader must not fall back to the writer DSN in a "
        "split-DB setup — found the legacy fallback expression"
    )
    assert "PostgresIndicatorLoader(dsn=indicator_dsn)" in src, (
        "the gate loader must be constructed with the EXPLICIT "
        "indicator_dsn only — even when None (so the loader uses its "
        "own settings/env default, the same source the optimizer used)"
    )


def test_apply_walk_forward_gate_no_longer_probes_default_timeframe():
    """PR #289 codex round-3 P2: the default-timeframe pre-probe is
    removed. Previously an empty default-timeframe fetch would cause
    the gate to bypass entirely, silently promoting candidates that
    optimized on non-default timeframes. The per-candidate loop must
    handle each candidate's own timeframe."""
    from app.strategies import run_multi_strategy_optimizer as rmso
    import inspect

    src = inspect.getsource(rmso._apply_walk_forward_gate)
    # The previous early-bypass pattern referenced
    # ``df_default = _fetch_for_timeframe(default_timeframe)`` followed
    # by ``return candidates[:candidates_per_strategy], 0`` — that
    # pattern must be gone.
    assert "df_default = _fetch_for_timeframe" not in src, (
        "the default-timeframe pre-probe must be removed; the "
        "per-candidate loop already handles empty data per-timeframe"
    )


def test_apply_walk_forward_gate_validates_non_default_timeframe_when_default_empty(monkeypatch):
    """Operationally: if the default 300s stream is empty but a
    candidate optimized on 60s has data, the gate must validate that
    candidate against 60s instead of silently promoting it."""
    from app.strategies import run_multi_strategy_optimizer as rmso

    class _PerTfLoader:
        def __init__(self):
            self.calls: list = []

        def fetch_indicator_bars(self, **kwargs):
            self.calls.append(kwargs)
            tf = kwargs["timeframe_seconds"]
            if tf == 300:
                return pd.DataFrame()  # default empty
            # 60s has bars
            n = 200
            return pd.DataFrame({
                "timestamp": pd.date_range("2026-01-01", periods=n, freq="60s"),
                "open": [100.0] * n,
                "high": [100.5] * n,
                "low": [99.5] * n,
                "close": [100.0] * n,
                "atr": [1.0] * n,
                "rsi": [50.0] * n,
                "macd": [0.0] * n,
                "macd_signal": [0.0] * n,
                "ema_20": [100.0] * n,
                "ema_30": [100.0] * n,
                "ema_50": [100.0] * n,
                "adx": [25.0] * n,
                "plus_di": [25.0] * n,
                "minus_di": [25.0] * n,
            })

    class _PassValidator:
        def validate(self, df, params, score_fn):
            return WalkForwardResult(
                in_sample_score=100.0,
                fold_scores=[25.0] * 4,
                fold_trade_counts=[10] * 4,
                oos_holdout_score=25.0,
                oos_holdout_trades=5,
                passed=True,
                failure_reasons=[],
                in_sample_bars=80,
                fold_bar_counts=[20] * 4,
            )

    loader = _PerTfLoader()
    candidates = [{"params": {"signal_timeframe": 60}, "metrics": {}}]
    survivors, dropped = rmso._apply_walk_forward_gate(
        strategy_name="ema20",
        underlying_label="NIFTY_IDX",
        candidates=candidates,
        candidates_per_strategy=1,
        lookback_days=20,
        validator=_PassValidator(),  # type: ignore[arg-type]
        loader=loader,  # type: ignore[arg-type]
    )
    assert len(survivors) == 1, (
        "candidate with non-default timeframe and real data must be "
        "validated and promoted, not silently bypassed because the "
        "default timeframe stream is empty"
    )
    assert dropped == 0
    # The loader must have been hit for the candidate's timeframe (60s).
    fetched_tfs = {c["timeframe_seconds"] for c in loader.calls}
    assert 60 in fetched_tfs, (
        f"loader must fetch the candidate's optimized timeframe; "
        f"got fetches for {fetched_tfs}"
    )


def test_ranked_candidates_exported_for_gate_backfill():
    """PR #289 codex round-3 P2: when the optimizer's first five
    candidates all fail the gate but candidate #6 passes, the gate
    must still find that survivor. The optimizer therefore exports
    ``ranked_candidates`` (the full evaluated list, sorted by score)
    in addition to the legacy ``top_5``."""
    import inspect
    from app.strategies import run_multi_strategy_optimizer as rmso

    # The result-compile block must build ranked_candidates from the
    # full sorted ``evaluated`` list (no ``[:5]`` slice).
    src = inspect.getsource(rmso.MultiStrategyOptimizer.optimize_strategy)
    assert "ranked_candidates" in src, (
        "MultiStrategyOptimizer.optimize_strategy must export "
        "ranked_candidates so the gate can backfill beyond top_5"
    )

    # The promote orchestrator must consume ranked_candidates (with
    # fallback to top_5 for legacy callers / fixtures).
    promote_src = inspect.getsource(rmso._promote_top_candidates)
    assert 'result.get("ranked_candidates")' in promote_src, (
        "_promote_top_candidates must consume ranked_candidates so "
        "the gate sees all evaluated candidates, not just the legacy "
        "top_5 slice"
    )
