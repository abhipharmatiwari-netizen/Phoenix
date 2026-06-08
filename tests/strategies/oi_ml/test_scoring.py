from __future__ import annotations

import pytest
import math

from app.strategies.oi_ml.scoring import (
    ConstantOiMlScorer,
    LightGbmOiMlScorer,
    MissingOiMlScorer,
    OiMlScore,
    OiMlScorerNotConfiguredError,
)


def test_constant_scorer_returns_valid_score_with_metadata():
    score = ConstantOiMlScorer(
        probability=0.63,
        predicted_mae_premium=42.0,
    ).score({"a": 1, "b": True})

    assert score.probability == 0.63
    assert score.predicted_mae_premium == 42.0
    assert score.metadata["scorer"] == "constant"
    assert score.metadata["feature_count"] == 2


@pytest.mark.parametrize(
    ("probability", "mae"),
    [
        (-0.01, 1.0),
        (1.01, 1.0),
        (0.50, -1.0),
    ],
)
def test_score_validates_probability_and_mae(probability, mae):
    with pytest.raises(ValueError):
        OiMlScore(probability=probability, predicted_mae_premium=mae)


def test_missing_scorer_fails_closed():
    with pytest.raises(OiMlScorerNotConfiguredError, match="not configured"):
        MissingOiMlScorer().score({"feature": 1.0})


class _FakeModel:
    def __init__(self, prediction):
        self.prediction = prediction
        self.last_matrix = None

    def predict(self, matrix):
        self.last_matrix = matrix
        return self.prediction


class _ArrayLike:
    def __init__(self, value):
        self.value = value

    def tolist(self):
        return self.value


def test_lightgbm_runtime_scorer_uses_feature_order_and_mae_model():
    classifier = _FakeModel([0.71])
    mae = _FakeModel([35.5])
    scorer = LightGbmOiMlScorer(
        classifier_model=classifier,
        mae_model=mae,
        feature_names=("feature_b", "feature_a", "missing_feature"),
    )

    score = scorer.score({"feature_a": True, "feature_b": "12.5"})

    assert score.probability == 0.71
    assert score.predicted_mae_premium == 35.5
    assert classifier.last_matrix[0][0:2] == [12.5, 1.0]
    assert math.isnan(classifier.last_matrix[0][2])
    assert score.metadata["scorer"] == "lightgbm"


def test_lightgbm_runtime_scorer_uses_default_mae_without_mae_model():
    scorer = LightGbmOiMlScorer(
        classifier_model=_FakeModel([[0.62]]),
        feature_names=("x",),
        default_mae_premium=25.0,
    )

    score = scorer.score({"x": 1})

    assert score.probability == 0.62
    assert score.predicted_mae_premium == 25.0


def test_lightgbm_runtime_scorer_accepts_array_like_predictions():
    scorer = LightGbmOiMlScorer(
        classifier_model=_FakeModel(_ArrayLike([0.67])),
        mae_model=_FakeModel(_ArrayLike([[18.0]])),
        feature_names=("x",),
    )

    score = scorer.score({"x": 1})

    assert score.probability == 0.67
    assert score.predicted_mae_premium == 18.0
