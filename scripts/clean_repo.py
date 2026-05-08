#!/usr/bin/env python3
"""Remove local generated artifacts so the checkout stays small and reproducible.

The script is intentionally conservative: it only deletes known build/cache/runtime
outputs and never traverses into `.git` or follows symlinks.
"""

from __future__ import annotations

import argparse
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class CleanupTarget:
    path: Path
    reason: str
    dependency: bool = False


ROOT_RELATIVE_TARGETS: tuple[CleanupTarget, ...] = (
    CleanupTarget(Path(".pytest_cache"), "pytest cache"),
    CleanupTarget(Path(".mypy_cache"), "mypy cache"),
    CleanupTarget(Path(".ruff_cache"), "ruff cache"),
    CleanupTarget(Path(".tox"), "tox virtualenv/cache"),
    CleanupTarget(Path(".nox"), "nox virtualenv/cache"),
    CleanupTarget(Path(".coverage"), "coverage data"),
    CleanupTarget(Path("coverage.xml"), "coverage report"),
    CleanupTarget(Path("htmlcov"), "HTML coverage report"),
    CleanupTarget(Path(".test_tmp"), "test scratch directory"),
    CleanupTarget(Path(".docker-tmp"), "Docker build scratch directory"),
    CleanupTarget(Path("logs"), "local runtime logs"),
    CleanupTarget(Path("state"), "local runtime state"),
    CleanupTarget(Path("replay_output"), "replay output"),
    CleanupTarget(Path("frontend/build"), "frontend production build"),
    CleanupTarget(Path("frontend/playwright-report"), "Playwright report"),
    CleanupTarget(Path("frontend/test-results"), "Playwright test results"),
    CleanupTarget(Path("frontend/node_modules"), "frontend dependency install", dependency=True),
)

RECURSIVE_DIR_NAMES = frozenset({"__pycache__"})
RECURSIVE_FILE_SUFFIXES = (".pyc", ".pyo")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _is_under_git(path: Path) -> bool:
    return ".git" in path.parts


def existing_targets(root: Path, include_deps: bool = False) -> list[CleanupTarget]:
    """Return cleanup targets that exist under *root*."""
    targets: list[CleanupTarget] = []
    seen: set[Path] = set()

    def add(target: CleanupTarget) -> None:
        resolved = target.path.resolve()
        relative_path = target.path.relative_to(root)
        if resolved in seen or _is_under_git(relative_path):
            return
        seen.add(resolved)
        targets.append(target)

    for target in ROOT_RELATIVE_TARGETS:
        if target.dependency and not include_deps:
            continue
        path = root / target.path
        if path.exists() or path.is_symlink():
            add(CleanupTarget(path, target.reason, target.dependency))

    release_dir = root / "release"
    if release_dir.exists():
        for archive in release_dir.glob("*.zip"):
            if archive.is_file() or archive.is_symlink():
                add(CleanupTarget(archive, "release archive"))

    pruned_dirs = {
        (root / target.path).resolve()
        for target in ROOT_RELATIVE_TARGETS
        if target.path != Path(".") and (root / target.path).is_dir()
    }

    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        if _is_under_git(current.relative_to(root)):
            dirnames[:] = []
            continue

        kept_dirnames: list[str] = []
        for dirname in dirnames:
            child = current / dirname
            if child.name == ".git":
                continue
            if child.resolve() in pruned_dirs:
                continue
            if not child.is_symlink() and child.name in RECURSIVE_DIR_NAMES:
                add(CleanupTarget(child, "Python bytecode cache"))
                continue
            kept_dirnames.append(dirname)
        dirnames[:] = kept_dirnames

        for filename in filenames:
            child = current / filename
            if child.suffix in RECURSIVE_FILE_SUFFIXES:
                add(CleanupTarget(child, "Python bytecode file"))

    sorted_targets = sorted(targets, key=lambda target: str(target.path.relative_to(root)))
    pruned: list[CleanupTarget] = []
    for target in sorted_targets:
        if any(parent.path in target.path.parents for parent in pruned):
            continue
        pruned.append(target)
    return pruned


def remove_target(target: CleanupTarget) -> None:
    path = target.path
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def format_targets(root: Path, targets: Iterable[CleanupTarget]) -> list[str]:
    return [f"{target.path.relative_to(root)}  # {target.reason}" for target in targets]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--yes",
        action="store_true",
        help="delete files instead of only printing what would be removed",
    )
    parser.add_argument(
        "--include-deps",
        action="store_true",
        help="also remove dependency installs such as frontend/node_modules",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = repo_root()
    targets = existing_targets(root, include_deps=args.include_deps)

    if not targets:
        print("Repo is already clean; no generated artifacts found.")
        return 0

    action = "Removing" if args.yes else "Would remove"
    print(f"{action} {len(targets)} generated artifact(s):")
    for line in format_targets(root, targets):
        print(f"  {line}")

    if args.yes:
        for target in targets:
            remove_target(target)
    else:
        print("Dry run only. Re-run with --yes to delete these paths.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
