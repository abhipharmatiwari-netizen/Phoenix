from __future__ import annotations

import json
from types import SimpleNamespace

from app.strategies.oi_ml import model


def _record(day: int, pnl: float = 250.0, probability: float = 0.7):
    return {
        "decision_ts": f"2026-05-{day:02d}T04:30:00+00:00",
        "expiry": "2026-05-26",
        "feature_candidate_oi": 1000 + day,
        "feature_pcr_total": 0.7,
        "primary_label": 1 if pnl > 0 else 0,
        "mae_premium": 20.0,
        "score_probability": probability,
        "pnl_per_lot": pnl,
    }


def test_grouped_walk_forward_splits_are_session_expiry_ordered_with_purge():
    records = [_record(day) for day in range(1, 7)]

    splits = model.grouped_walk_forward_splits(
        records,
        folds=3,
        purge_groups=1,
        embargo_groups=1,
    )

    assert splits
    for split in splits:
        assert max(split.train_groups) < min(split.test_groups)
        assert set(split.train_indices).isdisjoint(split.test_indices)


def test_promotion_report_enforces_oos_gates_and_writes_rejection(tmp_path):
    failing = [_record(day, pnl=-100.0) for day in range(1, 5)]

    report = model.evaluate_promotion_gates(
        failing,
        fold_expectancies=[-100.0],
        lookahead_violations=1,
        eod_violations=1,
    )
    target = tmp_path / "report.json"
    model.write_rejection_or_promotion_report(report, target)
    payload = json.loads(target.read_text(encoding="utf-8"))

    assert report.passed is False
    assert "accepted_trades_below_min" in report.reasons
    assert "lookahead_audit_failed" in report.reasons
    assert payload["paper_trading_enabled"] is False


def test_promotion_report_passes_strict_thresholds_for_clean_sample():
    records = [_record(day, pnl=300.0) for day in range(1, 261)]

    report = model.evaluate_promotion_gates(
        records,
        fold_expectancies=[250.0, 260.0, 270.0],
    )

    assert report.passed is True
    assert report.expectancy_rupees == 300.0
    assert report.paper_only_review is True


def test_calibration_and_feature_importance_reports_are_offline_explainability():
    records = [_record(day, pnl=250.0, probability=0.65) for day in range(1, 5)]

    calibration = model.calibration_report(records, bins=5)
    importance = model.feature_importance_report(records)

    assert len(calibration) == 5
    assert sum(bin.count for bin in calibration) == 4
    assert list(importance) == ["feature_candidate_oi", "feature_pcr_total"]


def test_train_with_fallback_uses_sklearn_when_lightgbm_is_missing(monkeypatch):
    class _FakeClassifier:
        def fit(self, x, y):
            self.x = x
            self.y = y
            return self

    fake_ensemble = SimpleNamespace(
        HistGradientBoostingClassifier=_FakeClassifier,
        HistGradientBoostingRegressor=_FakeClassifier,
    )

    def _raise(*_args, **_kwargs):
        from app.strategies.oi_ml.training import LightGbmUnavailableError

        raise LightGbmUnavailableError("missing")

    monkeypatch.setattr(model, "train_lightgbm", _raise)
    sys_modules = __import__("sys").modules
    monkeypatch.setitem(sys_modules, "sklearn", SimpleNamespace(ensemble=fake_ensemble))
    monkeypatch.setitem(sys_modules, "sklearn.ensemble", fake_ensemble)

    result = model.train_with_fallback([_record(1), _record(2)])

    assert result.backend == "sklearn_hist_gradient_boosting"
    assert result.row_count == 2
