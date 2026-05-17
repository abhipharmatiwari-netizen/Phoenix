"""Runtime scoring contracts for the OI/ML CE seller."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import json
import math
from typing import Any, Mapping, Protocol, Sequence

from app.strategies.oi_ml.training import LightGbmUnavailableError


@dataclass(frozen=True)
class OiMlScore:
    """Stage-1 probability plus Stage-2 premium MAE estimate."""

    probability: float
    predicted_mae_premium: float
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        probability = float(self.probability)
        mae = float(self.predicted_mae_premium)
        if not math.isfinite(probability) or probability < 0.0 or probability > 1.0:
            raise ValueError("OI/ML probability must be finite and within [0, 1]")
        if not math.isfinite(mae) or mae < 0.0:
            raise ValueError("OI/ML predicted MAE must be finite and non-negative")
        object.__setattr__(self, "probability", probability)
        object.__setattr__(self, "predicted_mae_premium", mae)


class OiMlScorer(Protocol):
    """Protocol implemented by runtime score providers."""

    def score(self, features: Mapping[str, Any]) -> OiMlScore:
        """Score one feature vector."""


@dataclass(frozen=True)
class ConstantOiMlScorer:
    """Deterministic scorer for tests, dry-runs, and fail-fast wiring checks."""

    probability: float = 0.0
    predicted_mae_premium: float = 0.0

    def score(self, features: Mapping[str, Any]) -> OiMlScore:
        return OiMlScore(
            probability=self.probability,
            predicted_mae_premium=self.predicted_mae_premium,
            metadata={"scorer": "constant", "feature_count": len(features)},
        )


class MissingOiMlScorer:
    """Fail-closed scorer used when no trained artifact is configured."""

    def score(self, features: Mapping[str, Any]) -> OiMlScore:
        raise RuntimeError("OI/ML scorer is not configured")


@dataclass(frozen=True)
class LightGbmOiMlScorer:
    """Runtime scorer backed by a binary model and optional MAE model."""

    classifier_model: Any
    feature_names: tuple[str, ...]
    mae_model: Any | None = None
    default_mae_premium: float = 0.0

    @classmethod
    def from_artifacts(
        cls,
        *,
        classifier_path: str | Path,
        feature_names_path: str | Path,
        mae_model_path: str | Path | None = None,
        default_mae_premium: float = 0.0,
    ) -> "LightGbmOiMlScorer":
        lgb = _import_lightgbm()
        with Path(feature_names_path).open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        feature_names = _feature_names_from_payload(payload)
        mae_model = (
            lgb.Booster(model_file=str(mae_model_path))
            if mae_model_path is not None
            else None
        )
        return cls(
            classifier_model=lgb.Booster(model_file=str(classifier_path)),
            feature_names=tuple(feature_names),
            mae_model=mae_model,
            default_mae_premium=float(default_mae_premium),
        )

    def score(self, features: Mapping[str, Any]) -> OiMlScore:
        matrix = [[_numeric(features.get(name)) for name in self.feature_names]]
        probability = _first_prediction(self.classifier_model.predict(matrix))
        if self.mae_model is not None:
            predicted_mae = _first_prediction(self.mae_model.predict(matrix))
        else:
            predicted_mae = float(self.default_mae_premium)
        return OiMlScore(
            probability=probability,
            predicted_mae_premium=predicted_mae,
            metadata={
                "scorer": "lightgbm",
                "feature_count": len(self.feature_names),
            },
        )


def _import_lightgbm() -> Any:
    try:
        import lightgbm as lgb  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on optional package
        raise LightGbmUnavailableError(
            "lightgbm is required for runtime OI/ML scoring. "
            "Install it in the strategy runtime before enabling model scoring."
        ) from exc
    return lgb


def _feature_names_from_payload(payload: Any) -> list[str]:
    if isinstance(payload, Mapping):
        value = payload.get("feature_names")
    else:
        value = payload
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("feature_names artifact must contain a list of feature names")
    names = [str(item) for item in value]
    if not names:
        raise ValueError("feature_names artifact is empty")
    return names


def _first_prediction(value: Any) -> float:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if not value:
            raise ValueError("model returned no predictions")
        first = value[0]
        if isinstance(first, Sequence) and not isinstance(first, (str, bytes)):
            if not first:
                raise ValueError("model returned empty nested prediction")
            first = first[0]
        return float(first)
    return float(value)


def _numeric(value: Any) -> float:
    if value is None or value == "":
        return math.nan
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return math.nan


__all__ = [
    "ConstantOiMlScorer",
    "LightGbmOiMlScorer",
    "MissingOiMlScorer",
    "OiMlScore",
    "OiMlScorer",
]
