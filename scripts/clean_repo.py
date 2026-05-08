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
    # Runtime-state targets (logs/, state/) are bind-mounted to host
    # directories on the OCI VM and hold operational data
    # (safety_alerts.log, risk_positions.json, executed_tokens_state.json,
    # etc.). Deleting them on the VM nukes production state. Gate behind
    # an explicit --include-runtime opt-in so a stray --yes on a misplaced
    # checkout cannot do irreversible damage.
    runtime: bool = False


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
    CleanupTarget(Path("logs"), "local runtime logs", runtime=True),
    CleanupTarget(Path("state"), "local runtime state", runtime=True),
    CleanupTarget(Path("replay_output"), "replay output"),
    CleanupTarget(Path("frontend/build"), "frontend production build"),
    CleanupTarget(Path("frontend/playwright-report"), "Playwright report"),
    CleanupTarget(Path("frontend/test-results"), "Playwright test results"),
    CleanupTarget(Path("frontend/node_modules"), "frontend dependency install", dependency=True),
)

RECURSIVE_DIR_NAMES = frozenset({"__pycache__"})
RECURSIVE_FILE_SUFFIXES = (".pyc", ".pyo")

# Virtualenv directories at the workspace root. Recursing into these would
# enumerate every __pycache__ inside installed packages -- noisy churn
# (Python regenerates them on import) and slow on large environments.
# Skip the same way we skip .git.
VENV_DIR_NAMES = frozenset({".venv", "venv", "env", ".backtest_venv"})


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _is_under_git(path: Path) -> bool:
    return ".git" in path.parts


def existing_targets(
    root: Path,
    include_deps: bool = False,
    include_runtime: bool = False,
) -> list[CleanupTarget]:
    """Return cleanup targets that exist under *root*.

    ``include_deps`` opts in to dependency-install paths (e.g.
    ``frontend/node_modules``). ``include_runtime`` opts in to runtime-state
    paths (``logs/``, ``state/``) -- never enable on the production VM
    where these are bind-mounted to host operational data.
    """
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
        if target.runtime and not include_runtime:
            continue
        path = root / target.path
        if path.exists() or path.is_symlink():
            add(
                CleanupTarget(
                    path,
                    target.reason,
                    dependency=target.dependency,
                    runtime=target.runtime,
                )
            )

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
            # Skip virtualenvs at any depth -- recursing inside them produces
            # thousands of __pycache__ "deletions" inside installed packages
            # that Python regenerates on import.
            if child.name in VENV_DIR_NAMES:
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
    # Emit POSIX-style relative paths so output is portable across Windows
    # and Linux (matches .gitignore conventions, plays well with grep).
    return [
        f"{target.path.relative_to(root).as_posix()}  # {target.reason}"
        for target in targets
    ]


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
    parser.add_argument(
        "--include-runtime",
        action="store_true",
        help=(
            "also remove runtime state directories (logs/, state/). "
            "DESTRUCTIVE on the production VM where these are bind-mounted "
            "to host operational data -- never enable on prod."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = repo_root()
    targets = existing_targets(
        root,
        include_deps=args.include_deps,
        include_runtime=args.include_runtime,
    )

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
