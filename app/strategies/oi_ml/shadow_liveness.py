"""Docker liveness probe for the OI/ML shadow sidecar.

Readiness and data-quality evidence live in ``shadow_health``. This probe is
intentionally narrower so transient validation-source outages do not make
Docker report the sidecar process as unhealthy.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


RUNNER_MARKER = "app.strategies.oi_ml.shadow_runner"


def runner_process_running(
    *,
    proc_root: Path = Path("/proc"),
    marker: str = RUNNER_MARKER,
) -> tuple[bool, str | None]:
    """Return whether the shadow runner process is visible under procfs."""
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return False, None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        cmdline_path = entry / "cmdline"
        try:
            raw = cmdline_path.read_bytes()
        except OSError:
            continue
        command = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
        if marker in command:
            return True, command
    return False, None


def liveness_payload(*, proc_root: Path = Path("/proc")) -> dict[str, Any]:
    running, command = runner_process_running(proc_root=proc_root)
    payload: dict[str, Any] = {
        "status": "ok" if running else "unhealthy",
        "process": "oi_ml_shadow_runner",
        "runner_process_seen": running,
        "readiness_probe": "python -m app.strategies.oi_ml.shadow_health",
    }
    if command:
        payload["runner_command"] = command
    return payload


def main() -> int:
    payload = liveness_payload()
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload["runner_process_seen"] else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["RUNNER_MARKER", "liveness_payload", "main", "runner_process_running"]
