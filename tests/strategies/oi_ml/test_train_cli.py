from __future__ import annotations

import json

from app.strategies.oi_ml.training import write_training_jsonl
from scripts.ml import train_mae_filter, walk_forward_oi_ce
from scripts.ml.train_oi_ce_scorer import main, parse_args


def _record(day: int = 19, pnl: float = 300.0):
    return {
        "decision_ts": f"2026-05-{day:02d}T04:30:00+00:00",
        "snapshot_ts": f"2026-05-{day:02d}T04:30:00+00:00",
        "expiry": "2026-05-28",
        "feature_candidate_oi": 1000,
        "feature_pcr_total": 0.65,
        "primary_label": 1,
        "mae_premium": 12.0,
        "pnl_per_lot": pnl,
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


def test_train_mae_filter_cli_defaults_to_mae_label(tmp_path, capsys):
    dataset_path = tmp_path / "dataset.jsonl"
    write_training_jsonl([_record()], dataset_path)

    code = train_mae_filter.main(["--input", str(dataset_path), "--dry-run"])

    assert code == 0
    assert "label=mae_premium" in capsys.readouterr().out


def test_walk_forward_cli_writes_paper_only_promotion_report(tmp_path):
    dataset_path = tmp_path / "dataset.jsonl"
    report_path = tmp_path / "promotion.json"
    records = [_record((idx % 28) + 1, pnl=300.0) for idx in range(260)]
    write_training_jsonl(records, dataset_path)

    code = walk_forward_oi_ce.main(
        [
            "--input",
            str(dataset_path),
            "--output",
            str(report_path),
            "--folds",
            "3",
        ]
    )
    payload = json.loads(report_path.read_text(encoding="utf-8"))

    assert code == 0
    assert payload["promotion"]["passed"] is True
    assert payload["paper_trading_enabled"] is False
    assert payload["paper_review_required"] is True
    assert payload["model_artifacts"]["walk_forward_folds"] > 0
