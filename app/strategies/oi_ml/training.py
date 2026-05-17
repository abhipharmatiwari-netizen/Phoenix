"""Artifact and LightGBM training helpers for OI/ML datasets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import csv
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from app.strategies.oi_ml.dataset import OiMlTrainingRow


DEFAULT_FEATURE_PREFIX = "feature_"
DEFAULT_BINARY_LABEL = "primary_label"
DEFAULT_REGRESSION_LABEL = "mae_premium"


class LightGbmUnavailableError(RuntimeError):
    """Raised when training is requested without the optional dependency."""


@dataclass(frozen=True)
class TrainingDataset:
    records: list[dict[str, Any]]
    feature_names: list[str]
    label_name: str

    @property
    def row_count(self) -> int:
        return len(self.records)


@dataclass(frozen=True)
class LightGbmTrainingResult:
    model: Any
    feature_names: list[str]
    label_name: str
    row_count: int
    objective: str


def records_from_rows(rows: Iterable[OiMlTrainingRow | Mapping[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, OiMlTrainingRow):
            records.append(row.to_dict())
        elif hasattr(row, "to_dict") and callable(row.to_dict):
            records.append(dict(row.to_dict()))
        else:
            records.append(dict(row))
    return records


def write_training_jsonl(
    rows: Iterable[OiMlTrainingRow | Mapping[str, Any]],
    path: str | Path,
) -> int:
    records = records_from_rows(rows)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as fh:
        for record in records:
            fh.write(json.dumps(_json_safe(record), sort_keys=True, separators=(",", ":")))
            fh.write("\n")
    return len(records)


def load_training_jsonl(path: str | Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            records.append(json.loads(line))
    return records


def write_training_csv(
    rows: Iterable[OiMlTrainingRow | Mapping[str, Any]],
    path: str | Path,
) -> int:
    records = records_from_rows(rows)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({key for record in records for key in record})
    with target.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            writer.writerow({key: _csv_value(record.get(key)) for key in fieldnames})
    return len(records)


def validate_training_records(
    records: Sequence[Mapping[str, Any]],
    *,
    label_name: str = DEFAULT_BINARY_LABEL,
    feature_prefix: str = DEFAULT_FEATURE_PREFIX,
) -> TrainingDataset:
    if not records:
        raise ValueError("training dataset is empty")
    feature_names = sorted(
        {
            key
            for record in records
            for key in record.keys()
            if str(key).startswith(feature_prefix)
        }
    )
    if not feature_names:
        raise ValueError(f"training dataset has no {feature_prefix!r} feature columns")
    for idx, record in enumerate(records):
        if label_name not in record:
            raise ValueError(f"training row {idx} missing label {label_name!r}")
        missing_features = [name for name in feature_names if name not in record]
        if missing_features:
            raise ValueError(f"training row {idx} missing feature columns: {missing_features}")
    return TrainingDataset(
        records=[dict(record) for record in records],
        feature_names=feature_names,
        label_name=label_name,
    )


def training_matrix(
    dataset: TrainingDataset,
) -> tuple[list[list[float]], list[float]]:
    x = [
        [_numeric(record.get(feature_name)) for feature_name in dataset.feature_names]
        for record in dataset.records
    ]
    y = [_numeric(record.get(dataset.label_name)) for record in dataset.records]
    return x, y


def train_lightgbm(
    records: Sequence[Mapping[str, Any]],
    *,
    label_name: str = DEFAULT_BINARY_LABEL,
    objective: str = "binary",
    feature_prefix: str = DEFAULT_FEATURE_PREFIX,
    params: Mapping[str, Any] | None = None,
    num_boost_round: int = 100,
) -> LightGbmTrainingResult:
    lgb = _import_lightgbm()
    dataset = validate_training_records(
        records,
        label_name=label_name,
        feature_prefix=feature_prefix,
    )
    x, y = training_matrix(dataset)
    train_data = lgb.Dataset(x, label=y, feature_name=dataset.feature_names)
    train_params = {
        "objective": objective,
        "metric": "binary_logloss" if objective == "binary" else "l2",
        "verbosity": -1,
    }
    train_params.update(dict(params or {}))
    model = lgb.train(train_params, train_data, num_boost_round=int(num_boost_round))
    return LightGbmTrainingResult(
        model=model,
        feature_names=dataset.feature_names,
        label_name=label_name,
        row_count=dataset.row_count,
        objective=objective,
    )


def save_lightgbm_model(result: LightGbmTrainingResult, path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    result.model.save_model(str(target))


def _import_lightgbm() -> Any:
    try:
        import lightgbm as lgb  # type: ignore
    except Exception as exc:  # pragma: no cover - exercised by tests via monkeypatch
        raise LightGbmUnavailableError(
            "lightgbm is required for OI/ML model training. "
            "Install it in the training environment before running the trainer."
        ) from exc
    return lgb


def _numeric(value: Any) -> float:
    if value is None or value == "":
        return math.nan
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return math.nan


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _csv_value(value: Any) -> Any:
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(_json_safe(value), sort_keys=True)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


__all__ = [
    "DEFAULT_BINARY_LABEL",
    "DEFAULT_FEATURE_PREFIX",
    "DEFAULT_REGRESSION_LABEL",
    "LightGbmTrainingResult",
    "LightGbmUnavailableError",
    "TrainingDataset",
    "load_training_jsonl",
    "records_from_rows",
    "save_lightgbm_model",
    "train_lightgbm",
    "training_matrix",
    "validate_training_records",
    "write_training_csv",
    "write_training_jsonl",
]
