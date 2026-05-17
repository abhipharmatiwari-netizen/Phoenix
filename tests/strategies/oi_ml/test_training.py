from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from app.strategies.oi_ml import training


def _record(**overrides):
    base = {
        "decision_ts": "2026-05-19T04:30:00+00:00",
        "snapshot_ts": "2026-05-19T04:30:00+00:00",
        "underlying": "NIFTY",
        "expiry": "2026-05-19",
        "strike": 25200,
        "option_type": "CE",
        "feature_candidate_oi": 1000,
        "feature_oi_wall_present": True,
        "feature_pcr_total": 0.65,
        "primary_label": 1,
        "mae_premium": 0.0,
    }
    base.update(overrides)
    return base


def test_jsonl_artifact_round_trips_stable_records(tmp_path):
    path = tmp_path / "dataset.jsonl"
    records = [_record(), _record(strike=25300, primary_label=0, mae_premium=80.0)]

    count = training.write_training_jsonl(records, path)
    loaded = training.load_training_jsonl(path)

    assert count == 2
    assert loaded == records
    assert path.read_text(encoding="utf-8").count("\n") == 2


def test_csv_artifact_writes_nested_values_as_json(tmp_path):
    path = tmp_path / "dataset.csv"

    count = training.write_training_csv([_record(label_quality_flags=["fees_not_applied"])], path)

    assert count == 1
    text = path.read_text(encoding="utf-8")
    assert "label_quality_flags" in text
    assert "fees_not_applied" in text


def test_validate_training_records_requires_rows_features_and_label():
    dataset = training.validate_training_records([_record(), _record(primary_label=0)])

    assert dataset.row_count == 2
    assert dataset.label_name == "primary_label"
    assert dataset.feature_names == [
        "feature_candidate_oi",
        "feature_oi_wall_present",
        "feature_pcr_total",
    ]

    with pytest.raises(ValueError, match="empty"):
        training.validate_training_records([])
    with pytest.raises(ValueError, match="no 'feature_'"):
        training.validate_training_records([{"primary_label": 1}])
    with pytest.raises(ValueError, match="missing label"):
        training.validate_training_records([_record()], label_name="missing")


def test_training_matrix_converts_bool_none_and_numbers():
    dataset = training.validate_training_records(
        [_record(feature_candidate_oi=None, feature_oi_wall_present=False)]
    )

    x, y = training.training_matrix(dataset)

    assert x[0][1] == 0.0  # feature_oi_wall_present
    assert x[0][2] == 0.65
    assert y == [1.0]
    assert x[0][0] != x[0][0]  # NaN for missing candidate OI


def test_train_lightgbm_fails_clearly_when_dependency_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "lightgbm", None)

    with pytest.raises(training.LightGbmUnavailableError, match="lightgbm is required"):
        training.train_lightgbm([_record()])


def test_train_lightgbm_uses_optional_dependency_when_available(monkeypatch):
    calls = {}

    class FakeDataset:
        def __init__(self, x, label, feature_name):
            calls["x"] = x
            calls["label"] = label
            calls["feature_name"] = feature_name

    class FakeModel:
        def save_model(self, path):
            calls["saved_path"] = path

    def fake_train(params, data, num_boost_round):
        calls["train"] = (params, data, num_boost_round)
        return FakeModel()

    fake_lgb = SimpleNamespace(
        Dataset=FakeDataset,
        train=fake_train,
    )
    monkeypatch.setitem(sys.modules, "lightgbm", fake_lgb)

    result = training.train_lightgbm([_record()], num_boost_round=7)
    training.save_lightgbm_model(result, "model.txt")

    assert result.row_count == 1
    assert result.objective == "binary"
    assert calls["feature_name"] == result.feature_names
    assert calls["train"][0]["objective"] == "binary"
    assert calls["train"][2] == 7
    assert calls["saved_path"] == "model.txt"
