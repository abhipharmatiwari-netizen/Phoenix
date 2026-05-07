#!/usr/bin/env python3
"""Build a clean Phoenix promotion artifact from allowed source files only."""

from __future__ import annotations

import argparse
import json
import subprocess
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
    ".env.example",
    "ABOUTME.md",
    "ARCHITECTURE.md",
    "Dockerfile",
    "README.md",
    "cloudrun.env",
    "docker-compose.oci-live.yml",
    "docker-compose.live.single.yml",
    "docker.env",
    "phoenix-override.yml.example",
    "release-manifest.json",
    "requirements.txt",
    "start-docker-secretstore.cmd",
    "start-docker-secretstore.ps1",
    "start-local.ps1",
)

EXCLUDED_PREFIXES = (
    ".claude/",
    ".docker-tmp/",
    ".github/",
    ".pytest_cache/",
    ".test_tmp/",
    ".venv/",
    "docs/archive/",
    "docs/obsolete/",
    "frontend/node_modules/",
    "frontend/playwright-report/",
    "frontend/test-results/",
    "google/",
    "logs/",
    "psycopg/",
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

EXCLUDED_EXACT = {
    ".backend-live.env.runtime",
}

EXCLUDED_SUFFIXES = (
    ".env.runtime",
    ".runtime.env",
    ".pyc",
    ".pyo",
)


def _run_git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=REPO_ROOT, stderr=subprocess.DEVNULL)


def _git_available() -> bool:
    try:
        _run_git("rev-parse", "--show-toplevel")
        return True
    except Exception:
        return False


def _filesystem_paths() -> list[str]:
    candidates: set[str] = set()
    for allowed_dir in ALLOWED_DIRS:
        root = REPO_ROOT / allowed_dir
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file():
                candidates.add(path.relative_to(REPO_ROOT).as_posix())
    for allowed_file in ALLOWED_FILES:
        path = REPO_ROOT / allowed_file
        if path.is_file():
            candidates.add(path.relative_to(REPO_ROOT).as_posix())
    return sorted(candidates)


def _tracked_paths() -> tuple[list[str], str]:
    if _git_available():
        pathspecs = [*ALLOWED_DIRS, *ALLOWED_FILES]
        raw = _run_git("ls-files", "-z", "--", *pathspecs)
        paths = [entry for entry in raw.decode("utf-8").split("\x00") if entry]
        return paths, "git"
    return _filesystem_paths(), "filesystem"


def _should_include(rel_path: str) -> bool:
    norm = rel_path.replace("\\", "/")
    parts = tuple(part for part in Path(norm).parts if part not in (".", ""))
    if not parts:
        return False
    if norm in EXCLUDED_EXACT:
        return False
    if norm.endswith(EXCLUDED_SUFFIXES):
        return False
    if any(segment in EXCLUDED_SEGMENTS for segment in parts):
        return False
    if any(norm.startswith(prefix) for prefix in EXCLUDED_PREFIXES):
        return False
    top = parts[0]
    if top in ALLOWED_DIRS:
        return True
    return norm in ALLOWED_FILES


def _git_head() -> str | None:
    try:
        return _run_git("rev-parse", "HEAD").decode("utf-8").strip()
    except Exception:
        return None


def _git_dirty() -> bool | None:
    try:
        status = _run_git("status", "--short", "--untracked-files=no").decode("utf-8").strip()
        return bool(status)
    except Exception:
        return None


def build_release_artifact(output_path: Path, prefix: str) -> Path:
    discovered, discovery_method = _tracked_paths()
    included = sorted(path for path in discovered if _should_include(path))
    output_path.parent.mkdir(parents=True, exist_ok=True)

    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "inventory_method": discovery_method,
        "git_head": _git_head(),
        "git_dirty": _git_dirty(),
        "included_file_count": len(included),
        "allowed_dirs": list(ALLOWED_DIRS),
        "allowed_files": list(ALLOWED_FILES),
        "excluded_prefixes": list(EXCLUDED_PREFIXES),
        "excluded_exact": sorted(EXCLUDED_EXACT),
        "excluded_suffixes": list(EXCLUDED_SUFFIXES),
    }

    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for rel_path in included:
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
        description="Build a clean Phoenix promotion artifact from allowed source files."
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
