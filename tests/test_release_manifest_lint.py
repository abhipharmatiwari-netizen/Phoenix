"""Packaging lint: assert the release artifact never includes dangerous files."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "release-manifest.json"
BUILD_SCRIPT_PATH = REPO_ROOT / "scripts" / "build_release_artifact.py"

# Files that must NEVER appear in a release artifact.
_FORBIDDEN_RELEASE_FILES = {
    ".docker-live.env",  # SHADOW-mode legacy reference env file
}


def _manifest_allowed_files() -> set[str]:
    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return set(data.get("allowed_files", []))


def _script_allowed_files() -> set[str]:
    """Parse ALLOWED_FILES tuple from build_release_artifact.py without importing it."""
    src = BUILD_SCRIPT_PATH.read_text(encoding="utf-8")
    # Locate the tuple and collect quoted string entries.
    in_block = False
    files: set[str] = set()
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("ALLOWED_FILES"):
            in_block = True
        if in_block:
            for tok in stripped.split('"'):
                candidate = tok.strip().strip("'")
                if candidate and not candidate.startswith(("#", "(", ")", ",")):
                    files.add(candidate)
            if stripped.endswith(")"):
                break
    return files


@pytest.mark.parametrize("forbidden", sorted(_FORBIDDEN_RELEASE_FILES))
def test_release_manifest_excludes_forbidden_file(forbidden: str):
    allowed = _manifest_allowed_files()
    assert forbidden not in allowed, (
        f"{forbidden!r} must not appear in release-manifest.json allowed_files — "
        "it contains SHADOW-mode settings and template placeholder secrets"
    )


@pytest.mark.parametrize("forbidden", sorted(_FORBIDDEN_RELEASE_FILES))
def test_build_script_excludes_forbidden_file(forbidden: str):
    allowed = _script_allowed_files()
    assert forbidden not in allowed, (
        f"{forbidden!r} must not appear in scripts/build_release_artifact.py ALLOWED_FILES — "
        "it contains SHADOW-mode settings and template placeholder secrets"
    )


def test_release_manifest_has_evidence_requirements():
    """Regression #70: release-manifest.json must define promotion evidence requirements."""
    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert "evidence_requirements" in data, (
        "release-manifest.json must contain an 'evidence_requirements' section documenting "
        "the minimum evidence required for LIVE promotion approval."
    )
    reqs = data["evidence_requirements"]
    assert isinstance(reqs.get("required"), list) and len(reqs["required"]) >= 1, (
        "evidence_requirements.required must be a non-empty list of evidence items."
    )
