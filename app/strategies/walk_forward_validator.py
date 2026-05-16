"""Walk-forward + out-of-sample gate for candidate parameter sets.

Issue #273 (epic #270). The optimizer's Bayesian/GA search ranks
candidates against in-sample data. Before any of those candidates reach
the human review queue, this gate replays each top-N candidate across:
  - K consecutive in-sample folds (stability check across sub-windows),
  - a held-out trailing OOS slice (extrapolation check).

A candidate must pass three acceptance criteria simultaneously to be
inserted into ``strategy_config_candidates`` by the writer (#272):

  1. ``median(fold_scores) >= (1 - max_in_sample_degradation_pct) *
     in_sample_score`` — the candidate's performance does not collapse
     when scored on sub-windows.
  2. ``oos_holdout_score > 0`` — the candidate is profitable on data the
     in-sample optimization did not see.
  3. Every fold has at least ``min_trades_per_fold`` trades — small-N
     fold scores aren't statistically meaningful.

Failing candidates are dropped with a structured log line — they are
NOT inserted as ``status='rejected'`` rows. The queue should stay clean
and focused on candidates worth a human's attention; the audit trail is
the optimizer's JSON output, which already records every evaluated
parameter set.

Honesty note on "walk-forward":
- The original optimizer tuned each candidate's params on the full
  lookback_days range, so the in-sample optimization already saw the
  data the OOS holdout is computed on. The OOS check here is therefore
  a *stability* signal, not a true forward-projection — but it still
  catches candidates whose performance hinges on a particular fold and
  collapses on the rest. A future enhancement could re-tune params on
  the in-sample slice only; out of scope for this PR.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)


_SCORE_KEY = "total_pnl"
"""Metric used to rank a fold / holdout score. Total PnL on the slice
matches the optimizer's headline metric and is monotone in expected
edge; ``BacktestMetrics.score`` would also work but introduces extra
indirection."""


@dataclass(frozen=True)
class WalkForwardConfig:
    """Tunable acceptance thresholds for the gate."""

    folds: int = 4
    """Number of consecutive in-sample folds. The in-sample slice (the
    portion of ``df`` BEFORE the OOS holdout) is split into ``folds``
    equal contiguous parts. K=4 over a 30-day lookback ⇒ four ~7.5-day
    folds, which is enough granularity to spot a candidate whose edge
    hinges on a single week."""

    oos_holdout_pct: float = 0.20
    """Fraction of the trailing data reserved as the OOS holdout. The
    remaining ``1 - oos_holdout_pct`` of the data is split into the
    folds. Default 20% over 30 days ⇒ ~6-day trailing holdout."""

    min_trades_per_fold: int = 5
    """A fold with fewer than this many trades is considered statistically
    too small to score; any such fold trips the gate."""

    max_in_sample_degradation_pct: float = 0.30
    """Allowed fractional drop from the in-sample score to the median
    fold score. 0.30 ⇒ fold scores down to 70% of in-sample are still
    acceptable. Tighter values reject more candidates."""


@dataclass
class WalkForwardResult:
    """Per-candidate outcome of the gate."""

    in_sample_score: float
    fold_scores: List[float] = field(default_factory=list)
    fold_trade_counts: List[int] = field(default_factory=list)
    oos_holdout_score: float = 0.0
    oos_holdout_trades: int = 0
    passed: bool = False
    failure_reasons: List[str] = field(default_factory=list)
    # PR #289 codex round-2 P1: bar counts let the stability check
    # compare scores on a per-bar basis. The full in-sample slice has
    # K× more bars than each fold; without normalization the median
    # fold's raw ``total_pnl`` always falls below the 70%-of-in-sample
    # threshold for uniformly stable candidates.
    in_sample_bars: int = 1
    fold_bar_counts: List[int] = field(default_factory=list)

    @property
    def median_fold_score(self) -> float:
        """Median raw fold score (per-slice ``total_pnl``).

        Retained for display / audit. The stability check uses
        ``median_fold_score_per_bar`` for the apples-to-apples
        comparison with ``in_sample_score_per_bar``.
        """
        if not self.fold_scores:
            return 0.0
        return statistics.median(self.fold_scores)

    @property
    def in_sample_score_per_bar(self) -> float:
        if not self.in_sample_bars:
            return 0.0
        return self.in_sample_score / max(1, self.in_sample_bars)

    @property
    def fold_scores_per_bar(self) -> List[float]:
        return [
            (score / max(1, bars))
            for score, bars in zip(self.fold_scores, self.fold_bar_counts)
        ]

    @property
    def median_fold_score_per_bar(self) -> float:
        per_bar = self.fold_scores_per_bar
        if not per_bar:
            return 0.0
        return statistics.median(per_bar)

    @property
    def min_fold_trades(self) -> int:
        return min(self.fold_trade_counts) if self.fold_trade_counts else 0

    def to_metrics_dict(self) -> Dict[str, Any]:
        """Augmentation for the candidate's ``metrics`` JSONB so the
        admin API and reviewers can see exactly why the gate accepted a
        candidate."""
        return {
            "walk_forward": {
                "in_sample_score": round(self.in_sample_score, 4),
                "in_sample_score_per_bar": round(self.in_sample_score_per_bar, 6),
                "in_sample_bars": int(self.in_sample_bars),
                "fold_scores": [round(s, 4) for s in self.fold_scores],
                "fold_scores_per_bar": [round(s, 6) for s in self.fold_scores_per_bar],
                "median_fold_score": round(self.median_fold_score, 4),
                "median_fold_score_per_bar": round(self.median_fold_score_per_bar, 6),
                "oos_holdout_score": round(self.oos_holdout_score, 4),
                "fold_trade_counts": list(self.fold_trade_counts),
                "fold_bar_counts": list(self.fold_bar_counts),
                "min_fold_trades": self.min_fold_trades,
                "oos_holdout_trades": self.oos_holdout_trades,
                "passed": self.passed,
                "failure_reasons": list(self.failure_reasons),
            }
        }


ScoreFn = Callable[[pd.DataFrame, Mapping[str, Any]], Mapping[str, Any]]
"""Score function contract: ``(df, params) -> simulator-result dict``.

The simulator-result dict matches what ``RealDataBacktester._simulate_*``
returns: at minimum a ``total_pnl`` and ``total_trades`` key. The
validator never calls a class method — the orchestrator passes one of
the static ``_simulate_ema20`` / ``_simulate_exclusive_nifty_ce`` /
``_simulate_put_momentum`` methods directly."""


class WalkForwardValidator:
    """Stage-5 gate that filters candidates before they are written to
    the review queue.

    Typical use in the multi-strategy orchestrator:

        validator = WalkForwardValidator(WalkForwardConfig(...))
        for candidate in top_n:
            result = validator.validate(df, candidate["params"], score_fn)
            if not result.passed:
                logger.info("walk_forward dropped candidate: %s", result.failure_reasons)
                continue
            # Augment metrics with the WF result before handing to writer:
            candidate["metrics"].update(result.to_metrics_dict())
            ...
    """

    def __init__(self, config: Optional[WalkForwardConfig] = None) -> None:
        import math

        self.config = config or WalkForwardConfig()
        if self.config.folds < 1:
            raise ValueError("WalkForwardConfig.folds must be >= 1")
        if not (0.0 < self.config.oos_holdout_pct < 1.0):
            raise ValueError(
                "WalkForwardConfig.oos_holdout_pct must be in (0, 1); "
                f"got {self.config.oos_holdout_pct}"
            )
        if self.config.min_trades_per_fold < 0:
            raise ValueError(
                "WalkForwardConfig.min_trades_per_fold must be >= 0"
            )
        # PR #289 codex round-2 P2: reject non-finite values and
        # values outside the intended fractional range. An operator
        # passing ``30`` instead of ``0.30`` (or ``NaN``) would have
        # turned the stability threshold negative / undefined and let
        # collapsed candidates through.
        deg = self.config.max_in_sample_degradation_pct
        if not math.isfinite(deg):
            raise ValueError(
                f"WalkForwardConfig.max_in_sample_degradation_pct must be "
                f"finite; got {deg!r}"
            )
        if not (0.0 <= deg <= 1.0):
            raise ValueError(
                "WalkForwardConfig.max_in_sample_degradation_pct must be in "
                f"[0, 1] (e.g. 0.30 for 30%); got {deg}. Did you pass a "
                "percent value like 30 by mistake?"
            )

    def validate(
        self,
        df: pd.DataFrame,
        params: Mapping[str, Any],
        score_fn: ScoreFn,
    ) -> WalkForwardResult:
        """Score the candidate on K folds + a held-out tail and decide
        accept / reject.

        ``df`` is the FULL indicator-bars frame the optimizer scored on.
        The validator slices it; the caller does not pre-split.
        """
        if df is None or df.empty:
            return WalkForwardResult(
                in_sample_score=0.0,
                passed=False,
                failure_reasons=["empty_dataframe"],
            )

        in_sample_df, holdout_df = self._split_in_sample_holdout(df)

        if in_sample_df.empty or holdout_df.empty:
            return WalkForwardResult(
                in_sample_score=0.0,
                passed=False,
                failure_reasons=["insufficient_data_for_split"],
            )

        in_sample_metrics = score_fn(in_sample_df, params)
        in_sample_score = float(in_sample_metrics.get(_SCORE_KEY, 0.0))
        in_sample_bars = max(1, len(in_sample_df))

        fold_scores: List[float] = []
        fold_trades: List[int] = []
        fold_bar_counts: List[int] = []
        for fold_df in self._iter_folds(in_sample_df):
            fold_metrics = score_fn(fold_df, params)
            fold_scores.append(float(fold_metrics.get(_SCORE_KEY, 0.0)))
            fold_trades.append(int(fold_metrics.get("total_trades", 0)))
            fold_bar_counts.append(max(1, len(fold_df)))

        holdout_metrics = score_fn(holdout_df, params)
        holdout_score = float(holdout_metrics.get(_SCORE_KEY, 0.0))
        holdout_trades = int(holdout_metrics.get("total_trades", 0))

        result = WalkForwardResult(
            in_sample_score=in_sample_score,
            fold_scores=fold_scores,
            fold_trade_counts=fold_trades,
            oos_holdout_score=holdout_score,
            oos_holdout_trades=holdout_trades,
            # PR #289 codex round-2 P1: pass per-bar slice sizes so the
            # stability check can compare apples-to-apples. The full
            # in-sample slice is K× larger than each fold; without
            # normalization the median fold's raw ``total_pnl`` is
            # roughly 1/K of the in-sample total and always falls below
            # the 70%-of-in-sample threshold even for uniformly stable
            # candidates.
            in_sample_bars=in_sample_bars,
            fold_bar_counts=fold_bar_counts,
        )
        result.failure_reasons = self._reasons_to_reject(result)
        result.passed = not result.failure_reasons
        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _split_in_sample_holdout(
        self, df: pd.DataFrame
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """Trailing-tail split. The OOS holdout is the most-recent slice
        so the gate is testing "would the in-sample-best params have
        held up on the most-recent data we have"."""
        n = len(df)
        holdout_size = max(1, int(n * self.config.oos_holdout_pct))
        if holdout_size >= n:
            return df.iloc[0:0], df.iloc[0:0]
        return df.iloc[:-holdout_size], df.iloc[-holdout_size:]

    def _iter_folds(self, in_sample_df: pd.DataFrame):
        """Split the in-sample frame into ``folds`` consecutive equal
        contiguous slices. Last fold absorbs the remainder if ``len(df)``
        is not divisible by ``folds``."""
        n = len(in_sample_df)
        folds = self.config.folds
        fold_size = max(1, n // folds)
        for k in range(folds):
            start = k * fold_size
            end = (k + 1) * fold_size if k < folds - 1 else n
            if start >= n:
                # The fold count is larger than the data can support;
                # yield an empty slice and let the min_trades gate
                # reject it downstream.
                yield in_sample_df.iloc[0:0]
                continue
            yield in_sample_df.iloc[start:end]

    def _reasons_to_reject(self, result: WalkForwardResult) -> List[str]:
        reasons: List[str] = []

        # 1. Stability — median fold score must not collapse vs in-sample.
        #
        # PR #289 codex round-2 P1: compare PER-BAR metrics so the
        # comparison is apples-to-apples. The full in-sample slice has
        # K× more bars than each fold; a uniformly stable candidate
        # earning ``X`` PnL per bar produces in-sample = X·N and median
        # fold = X·N/K. The raw-PnL comparison would reject every
        # stable candidate because X·N/K < 0.7·X·N. Per-bar
        # normalization removes the slice-size scaling so the threshold
        # tests actual stability.
        is_per_bar = result.in_sample_score_per_bar
        median_per_bar = result.median_fold_score_per_bar
        min_acceptable_median_per_bar = (
            (1.0 - self.config.max_in_sample_degradation_pct) * is_per_bar
        )
        if is_per_bar > 0:
            if median_per_bar < min_acceptable_median_per_bar:
                reasons.append("median_fold_score_below_in_sample_threshold")
        else:
            # Non-positive in-sample (per-bar): median must at least be
            # no worse than in-sample. A candidate that lost on
            # in-sample and lost MORE on the folds is unstable.
            if median_per_bar < is_per_bar:
                reasons.append("median_fold_score_below_in_sample_score")

        # 2. OOS holdout must be strictly profitable.
        if result.oos_holdout_score <= 0:
            reasons.append("oos_holdout_not_profitable")

        # 3. Every fold must have at least min_trades_per_fold trades.
        if result.min_fold_trades < self.config.min_trades_per_fold:
            reasons.append("fold_trade_count_below_floor")

        return reasons
