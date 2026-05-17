from __future__ import annotations

from app.strategies.oi_ml.training import write_training_jsonl
from scripts.ml.train_oi_ce_scorer import main, parse_args


def _record():
    return {
        "decision_ts": "2026-05-19T04:30:00+00:00",
        "snapshot_ts": "2026-05-19T04:30:00+00:00",
        "feature_candidate_oi": 1000,
        "feature_pcr_total": 0.65,
        "primary_label": 1,
        "mae_premium": 0.0,
    }


def test_train_cli_parse_defaults():
    args = parse_args(["--input", "dataset.jsonl"])

    assert args.input == "dataset.jsonl"
    assert args.task == "binary"
    assert args.output is None
    assert args.dry_run is False


def test_train_cli_dry_run_validates_dataset_without_output(tmp_path, capsys):
    dataset_path = tmp_path / "dataset.jsonl"
    write_training_jsonl([_record()], dataset_path)

    code = main(["--input", str(dataset_path), "--dry-run"])

    assert code == 0
    out = capsys.readouterr().out
    assert "Validated 1 rows" in out
    assert "2 features" in out


def test_train_cli_requires_output_when_not_dry_run(tmp_path, capsys):
    dataset_path = tmp_path / "dataset.jsonl"
    write_training_jsonl([_record()], dataset_path)

    code = main(["--input", str(dataset_path)])

    assert code == 2
    assert "--output is required" in capsys.readouterr().err


def test_train_cli_reports_missing_lightgbm(tmp_path, capsys, monkeypatch):
    dataset_path = tmp_path / "dataset.jsonl"
    write_training_jsonl([_record()], dataset_path)
    monkeypatch.setattr(
        "scripts.ml.train_oi_ce_scorer.train_lightgbm",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            __import__(
                "app.strategies.oi_ml.training",
                fromlist=["LightGbmUnavailableError"],
            ).LightGbmUnavailableError("lightgbm is required for training")
        ),
    )

    code = main(["--input", str(dataset_path), "--output", str(tmp_path / "model.txt")])

    assert code == 3
    assert "lightgbm is required" in capsys.readouterr().err
