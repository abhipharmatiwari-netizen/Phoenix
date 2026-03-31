#!/usr/bin/env python3
"""Build a clean Phoenix promotion artifact from git-tracked sources."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPO_ROOT / "release" / "phoenix-live-source.zip"

ALLOWED_DIRS = (
    "app",
    "docs",
    "frontend",
    "migrations",
    "nginx",
    "scripts",
)

ALLOWED_FILES = (
    ".docker-live.env",
    ".env.example",
    "ABOUTME.md",
    "ARCHITECTURE.md",
    "Dockerfile",
    "README.md",
    "cloudrun.env",
    "docker-compose.live.single.yml",
    "docker.env",
    "requirements.txt",
    "start-docker-secretstore.cmd",
    "start-docker-secretstore.ps1",
    "start-local.ps1",
)

EXCLUDED_PREFIXES = (
    ".claude/",
    ".github/",
    ".pytest_cache/",
    ".test_tmp/",
    ".venv/",
    "docs/obsolete/",
    "frontend/node_modules/",
    "frontend/playwright-report/",
    "frontend/test-results/",
    "logs/",
    "pytest-cache-files-",
    "release/",
    "tests/",
)

EXCLUDED_SEGMENTS = {
    "__pycache__",
    ".pytest_cache",
    ".test_tmp",
    ".venv",
}


def _run_git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=REPO_ROOT)


def _tracked_paths() -> list[str]:
    pathspecs = [*ALLOWED_DIRS, *ALLOWED_FILES]
    raw = _run_git("ls-files", "-z", "--", *pathspecs)
    return [entry for entry in raw.decode("utf-8").split("\x00") if entry]


def _should_include(rel_path: str) -> bool:
    norm = rel_path.replace("\\", "/")
    parts = tuple(part for part in Path(norm).parts if part not in (".", ""))
    if not parts:
        return False
    if any(segment in EXCLUDED_SEGMENTS for segment in parts):
        return False
    if any(norm.startswith(prefix) for prefix in EXCLUDED_PREFIXES):
        return False
    top = parts[0]
    if top in ALLOWED_DIRS:
        return True
    return norm in ALLOWED_FILES


def _git_head() -> str:
    return _run_git("rev-parse", "HEAD").decode("utf-8").strip()


def _git_dirty() -> bool:
    status = _run_git("status", "--short", "--untracked-files=no").decode("utf-8").strip()
    return bool(status)


def build_release_artifact(output_path: Path, prefix: str) -> Path:
    tracked = sorted(path for path in _tracked_paths() if _should_include(path))
    output_path.parent.mkdir(parents=True, exist_ok=True)

    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "git_head": _git_head(),
        "git_dirty": _git_dirty(),
        "included_file_count": len(tracked),
        "allowed_dirs": list(ALLOWED_DIRS),
        "allowed_files": list(ALLOWED_FILES),
        "excluded_prefixes": list(EXCLUDED_PREFIXES),
    }

    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for rel_path in tracked:
            source_path = REPO_ROOT / rel_path
            if not source_path.is_file():
                continue
            archive.write(source_path, arcname=f"{prefix}/{rel_path}")
        archive.writestr(
            f"{prefix}/release-manifest.json",
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        )

    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a clean Phoenix promotion artifact from git-tracked files."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Zip output path. Default: {DEFAULT_OUTPUT}",
    )
    parser.add_argument(
        "--prefix",
        default="phoenix-v9",
        help="Root directory name inside the zip archive.",
    )
    args = parser.parse_args()

    output_path = build_release_artifact(args.output.resolve(), args.prefix.strip("/\\") or "phoenix-v9")
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
