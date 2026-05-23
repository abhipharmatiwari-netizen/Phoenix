"""Model training, walk-forward, and promotion gates for OI/ML CE seller."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.strategies.oi_ml.training import (
    DEFAULT_BINARY_LABEL,
    DEFAULT_FEATURE_PREFIX,
    LightGbmTrainingResult,
    LightGbmUnavailableError,
    train_lightgbm,
    validate_training_records,
)


@dataclass(frozen=True)
class GroupedWalkForwardSplit:
    train_groups: tuple[str, ...]
    test_groups: tuple[str, ...]
    train_indices: tuple[int, ...]
    test_indices: tuple[int, ...]


@dataclass(frozen=True)
class PromotionGateConfig:
    min_expectancy_rupees: float = 150.0
    min_profit_factor: float = 1.25
    max_drawdown_rupees: float = 40000.0
    min_accepted_trades: int = 250
    min_stable_fold_expectancy: float = 0.0
    require_clean_lookahead_audit: bool = True
    require_zero_eod_violations: bool = True


@dataclass(frozen=True)
class PromotionReport:
    passed: bool
    reasons: tuple[str, ...]
    accepted_trades: int
    expectancy_rupees: float
    profit_factor: float
    max_drawdown_rupees: float
    stable_folds: bool
    lookahead_violations: int
    eod_violations: int
    paper_only_review: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "reasons": list(self.reasons),
            "accepted_trades": self.accepted_trades,
            "expectancy_rupees": self.expectancy_rupees,
            "profit_factor": self.profit_factor,
            "max_drawdown_rupees": self.max_drawdown_rupees,
            "stable_folds": self.stable_folds,
            "lookahead_violations": self.lookahead_violations,
            "eod_violations": self.eod_violations,
            "paper_only_review": self.paper_only_review,
        }


@dataclass(frozen=True)
class CalibrationBin:
    lower: float
    upper: float
    count: int
    mean_probability: float
    observed_win_rate: float


@dataclass(frozen=True)
class FallbackTrainingResult:
    backend: str
    model: Any
    feature_names: tuple[str, ...]
    label_name: str
    objective: str
    row_count: int


def grouped_walk_forward_splits(
    records: Sequence[Mapping[str, Any]],
    *,
    folds: int = 3,
    purge_groups: int = 0,
    embargo_groups: int = 0,
) -> list[GroupedWalkForwardSplit]:
    groups = sorted({_group_key(record) for record in records})
    if len(groups) < 2:
        return []
    fold_count = max(1, min(int(folds), len(groups) - 1))
    test_chunks = _chunks(groups[1:], fold_count)
    splits: list[GroupedWalkForwardSplit] = []
    for test_groups in test_chunks:
        if not test_groups:
            continue
        test_start = groups.index(test_groups[0])
        train_end = max(0, test_start - int(purge_groups))
        embargo_end = min(len(groups), groups.index(test_groups[-1]) + 1 + int(embargo_groups))
        train_groups = tuple(groups[:train_end])
        blocked = set(test_groups) | set(groups[test_start:embargo_end])
        train_groups = tuple(group for group in train_groups if group not in blocked)
        if not train_groups:
            continue
        train_indices = tuple(
            idx for idx, record in enumerate(records) if _group_key(record) in train_groups
        )
        test_indices = tuple(
            idx for idx, record in enumerate(records) if _group_key(record) in set(test_groups)
        )
        splits.append(
            GroupedWalkForwardSplit(
                train_groups=train_groups,
                test_groups=tuple(test_groups),
                train_indices=train_indices,
                test_indices=test_indices,
            )
        )
    return splits


def train_with_fallback(
    records: Sequence[Mapping[str, Any]],
    *,
    label_name: str = DEFAULT_BINARY_LABEL,
    objective: str = "binary",
    feature_prefix: str = DEFAULT_FEATURE_PREFIX,
    num_boost_round: int = 100,
) -> LightGbmTrainingResult | FallbackTrainingResult:
    try:
        return train_lightgbm(
            records,
            label_name=label_name,
            objective=objective,
            feature_prefix=feature_prefix,
            num_boost_round=num_boost_round,
        )
    except LightGbmUnavailableError:
        return _train_hist_gradient_boosting(
            records,
            label_name=label_name,
            objective=objective,
            feature_prefix=feature_prefix,
        )


def calibration_report(
    records: Sequence[Mapping[str, Any]],
    *,
    probability_key: str = "score_probability",
    label_key: str = DEFAULT_BINARY_LABEL,
    bins: int = 10,
) -> list[CalibrationBin]:
    bucket_count = max(1, int(bins))
    buckets: list[list[tuple[float, float]]] = [[] for _ in range(bucket_count)]
    for record in records:
        probability = _float(record.get(probability_key))
        label = _float(record.get(label_key))
        if probability is None or label is None:
            continue
        index = min(bucket_count - 1, max(0, int(probability * bucket_count)))
        buckets[index].append((probability, label))
    out: list[CalibrationBin] = []
    for idx, bucket in enumerate(buckets):
        lower = idx / bucket_count
        upper = (idx + 1) / bucket_count
        if not bucket:
            out.append(CalibrationBin(lower, upper, 0, 0.0, 0.0))
            continue
        out.append(
            CalibrationBin(
                lower=lower,
                upper=upper,
                count=len(bucket),
                mean_probability=sum(item[0] for item in bucket) / len(bucket),
                observed_win_rate=sum(item[1] for item in bucket) / len(bucket),
            )
        )
    return out


def evaluate_promotion_gates(
    records: Sequence[Mapping[str, Any]],
    *,
    fold_expectancies: Sequence[float] = (),
    lookahead_violations: int = 0,
    eod_violations: int = 0,
    config: PromotionGateConfig | None = None,
) -> PromotionReport:
    cfg = config or PromotionGateConfig()
    pnl_values = [_float(record.get("pnl_per_lot")) for record in records]
    pnl = [value for value in pnl_values if value is not None and math.isfinite(value)]
    accepted = len(pnl)
    expectancy = sum(pnl) / accepted if accepted else 0.0
    gross_profit = sum(value for value in pnl if value > 0)
    gross_loss = abs(sum(value for value in pnl if value < 0))
    profit_factor = float("inf") if gross_loss == 0 and gross_profit > 0 else _safe_ratio(gross_profit, gross_loss)
    max_dd = _max_drawdown(pnl)
    stable_folds = bool(fold_expectancies) and all(
        float(value) >= float(cfg.min_stable_fold_expectancy)
        for value in fold_expectancies
    )
    reasons: list[str] = []
    if accepted < int(cfg.min_accepted_trades):
        reasons.append("accepted_trades_below_min")
    if expectancy <= float(cfg.min_expectancy_rupees):
        reasons.append("expectancy_below_min")
    if profit_factor < float(cfg.min_profit_factor):
        reasons.append("profit_factor_below_min")
    if max_dd > float(cfg.max_drawdown_rupees):
        reasons.append("max_drawdown_above_limit")
    if not stable_folds:
        reasons.append("unstable_folds")
    if cfg.require_clean_lookahead_audit and int(lookahead_violations) != 0:
        reasons.append("lookahead_audit_failed")
    if cfg.require_zero_eod_violations and int(eod_violations) != 0:
        reasons.append("eod_flatten_violations")
    return PromotionReport(
        passed=not reasons,
        reasons=tuple(reasons),
        accepted_trades=accepted,
        expectancy_rupees=expectancy,
        profit_factor=profit_factor,
        max_drawdown_rupees=max_dd,
        stable_folds=stable_folds,
        lookahead_violations=int(lookahead_violations),
        eod_violations=int(eod_violations),
    )


def write_rejection_or_promotion_report(
    report: PromotionReport,
    path: str | Path,
    *,
    model_artifacts: Mapping[str, Any] | None = None,
) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "promotion": report.to_dict(),
        "paper_trading_enabled": False,
        "model_artifacts": dict(model_artifacts or {}),
    }
    if report.passed:
        payload["paper_trading_enabled"] = False
        payload["paper_review_required"] = True
    target.write_text(
        json.dumps(payload, sort_keys=True, indent=2),
        encoding="utf-8",
    )


def feature_importance_report(
    records: Sequence[Mapping[str, Any]],
    *,
    label_name: str = DEFAULT_BINARY_LABEL,
    feature_prefix: str = DEFAULT_FEATURE_PREFIX,
) -> dict[str, float]:
    dataset = validate_training_records(
        records,
        label_name=label_name,
        feature_prefix=feature_prefix,
    )
    labels = [_float(record.get(label_name)) or 0.0 for record in dataset.records]
    out: dict[str, float] = {}
    for feature_name in dataset.feature_names:
        values = [_float(record.get(feature_name)) or 0.0 for record in dataset.records]
        out[feature_name] = abs(_correlation(values, labels))
    return dict(sorted(out.items(), key=lambda item: item[1], reverse=True))


def _train_hist_gradient_boosting(
    records: Sequence[Mapping[str, Any]],
    *,
    label_name: str,
    objective: str,
    feature_prefix: str,
) -> FallbackTrainingResult:
    dataset = validate_training_records(
        records,
        label_name=label_name,
        feature_prefix=feature_prefix,
    )
    x = [
        [_float(record.get(feature_name)) or 0.0 for feature_name in dataset.feature_names]
        for record in dataset.records
    ]
    y = [_float(record.get(label_name)) or 0.0 for record in dataset.records]
    if objective == "binary":
        from sklearn.ensemble import HistGradientBoostingClassifier  # type: ignore

        model = HistGradientBoostingClassifier().fit(x, y)
    else:
        from sklearn.ensemble import HistGradientBoostingRegressor  # type: ignore

        model = HistGradientBoostingRegressor(loss="quantile", quantile=0.9).fit(x, y)
    return FallbackTrainingResult(
        backend="sklearn_hist_gradient_boosting",
        model=model,
        feature_names=tuple(dataset.feature_names),
        label_name=dataset.label_name,
        objective=objective,
        row_count=dataset.row_count,
    )


def _group_key(record: Mapping[str, Any]) -> str:
    decision_raw = record.get("decision_ts") or record.get("entry_ts") or ""
    expiry_raw = record.get("expiry") or ""
    session = _session_date(decision_raw)
    expiry = _session_date(expiry_raw)
    return f"{session.isoformat()}|{expiry.isoformat()}"


def _session_date(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value or "").strip()
    if not text:
        return date.min
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return date.fromisoformat(text[:10])


def _chunks(values: Sequence[str], count: int) -> list[list[str]]:
    if not values:
        return []
    chunk_size = max(1, math.ceil(len(values) / max(1, int(count))))
    return [list(values[idx : idx + chunk_size]) for idx in range(0, len(values), chunk_size)]


def _float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_ratio(numerator: float, denominator: float) -> float:
    if denominator == 0:
        return 0.0
    return float(numerator) / float(denominator)


def _max_drawdown(pnl: Sequence[float]) -> float:
    peak = 0.0
    equity = 0.0
    max_dd = 0.0
    for value in pnl:
        equity += float(value)
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return max_dd


def _correlation(xs: Sequence[float], ys: Sequence[float]) -> float:
    if len(xs) != len(ys) or len(xs) < 2:
        return 0.0
    x_mean = sum(xs) / len(xs)
    y_mean = sum(ys) / len(ys)
    x_var = sum((x - x_mean) ** 2 for x in xs)
    y_var = sum((y - y_mean) ** 2 for y in ys)
    if x_var == 0 or y_var == 0:
        return 0.0
    cov = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    return cov / math.sqrt(x_var * y_var)


__all__ = [
    "CalibrationBin",
    "FallbackTrainingResult",
    "GroupedWalkForwardSplit",
    "PromotionGateConfig",
    "PromotionReport",
    "calibration_report",
    "evaluate_promotion_gates",
    "feature_importance_report",
    "grouped_walk_forward_splits",
    "train_with_fallback",
    "write_rejection_or_promotion_report",
]
