from __future__ import annotations

import json

from app.strategies.oi_ml import shadow_liveness


def test_runner_process_running_detects_shadow_runner(tmp_path):
    proc_dir = tmp_path / "123"
    proc_dir.mkdir()
    (proc_dir / "cmdline").write_bytes(
        b"python\x00-m\x00app.strategies.oi_ml.shadow_runner\x00"
    )
    (tmp_path / "self").mkdir()

    running, command = shadow_liveness.runner_process_running(proc_root=tmp_path)

    assert running is True
    assert command == "python -m app.strategies.oi_ml.shadow_runner"


def test_runner_process_running_ignores_missing_runner(tmp_path):
    proc_dir = tmp_path / "456"
    proc_dir.mkdir()
    (proc_dir / "cmdline").write_bytes(b"python\x00-m\x00other.module\x00")

    assert shadow_liveness.runner_process_running(proc_root=tmp_path) == (False, None)


def test_liveness_payload_is_sanitized_and_references_readiness_probe(tmp_path):
    proc_dir = tmp_path / "789"
    proc_dir.mkdir()
    (proc_dir / "cmdline").write_bytes(
        b"python\x00-m\x00app.strategies.oi_ml.shadow_runner\x00"
    )

    payload = shadow_liveness.liveness_payload(proc_root=tmp_path)

    assert payload["status"] == "ok"
    assert payload["runner_process_seen"] is True
    assert payload["process"] == "oi_ml_shadow_runner"
    assert payload["readiness_probe"] == "python -m app.strategies.oi_ml.shadow_health"
    json.dumps(payload)
