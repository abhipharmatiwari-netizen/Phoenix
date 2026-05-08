from pathlib import Path

from scripts.clean_repo import existing_targets, format_targets, remove_target


def test_existing_targets_are_conservative_by_default(tmp_path: Path) -> None:
    (tmp_path / ".pytest_cache").mkdir()
    (tmp_path / "frontend" / "node_modules").mkdir(parents=True)
    (tmp_path / "app" / "__pycache__").mkdir(parents=True)
    (tmp_path / "app" / "__pycache__" / "module.pyc").write_bytes(b"cache")

    targets = existing_targets(tmp_path)
    rendered = format_targets(tmp_path, targets)

    assert ".pytest_cache  # pytest cache" in rendered
    assert "app/__pycache__  # Python bytecode cache" in rendered
    assert all("node_modules" not in line for line in rendered)


def test_include_deps_allows_node_modules_cleanup(tmp_path: Path) -> None:
    (tmp_path / "frontend" / "node_modules").mkdir(parents=True)

    targets = existing_targets(tmp_path, include_deps=True)

    assert format_targets(tmp_path, targets) == [
        "frontend/node_modules  # frontend dependency install"
    ]


def test_remove_target_deletes_files_and_directories(tmp_path: Path) -> None:
    directory = tmp_path / "htmlcov"
    directory.mkdir()
    (directory / "index.html").write_text("coverage", encoding="utf-8")
    file_target = tmp_path / ".coverage"
    file_target.write_text("data", encoding="utf-8")

    for target in existing_targets(tmp_path):
        remove_target(target)

    assert not directory.exists()
    assert not file_target.exists()


# --- Safety-guard regression tests (PR #209 follow-up) -------------------

def test_runtime_dirs_excluded_by_default(tmp_path: Path) -> None:
    """logs/ and state/ are bind-mounted to host operational data on the
    production VM. The default cleanup MUST NOT touch them; an explicit
    --include-runtime flag is required."""
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "safety_alerts.log").write_text("would-be-prod-data")
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "risk_positions.json").write_text("{}")

    targets = existing_targets(tmp_path)
    rendered = format_targets(tmp_path, targets)

    assert all("logs" not in line for line in rendered), rendered
    assert all("state" not in line for line in rendered), rendered


def test_include_runtime_opts_in_to_logs_and_state(tmp_path: Path) -> None:
    """--include-runtime explicitly opts in to logs/ + state/ deletion."""
    (tmp_path / "logs").mkdir()
    (tmp_path / "state").mkdir()

    targets = existing_targets(tmp_path, include_runtime=True)
    rendered = format_targets(tmp_path, targets)

    assert "logs  # local runtime logs" in rendered
    assert "state  # local runtime state" in rendered


def test_venv_directories_are_skipped_during_walk(tmp_path: Path) -> None:
    """Walking into virtualenvs at workspace root produces thousands of
    __pycache__ entries inside installed packages. The script must skip
    .venv, venv, env, and .backtest_venv at any depth."""
    # Simulate a virtualenv with installed-package bytecode caches.
    for venv_name in (".venv", "venv", "env", ".backtest_venv"):
        venv_root = tmp_path / venv_name / "Lib" / "site-packages" / "somepkg"
        venv_root.mkdir(parents=True)
        (venv_root / "__pycache__").mkdir()
        (venv_root / "__pycache__" / "module.cpython-312.pyc").write_bytes(b"x")

    # Real source tree should still be cleaned.
    (tmp_path / "app" / "__pycache__").mkdir(parents=True)

    targets = existing_targets(tmp_path)
    rendered = format_targets(tmp_path, targets)

    # The app's __pycache__ is in scope.
    assert any("app/__pycache__" in line for line in rendered), rendered
    # None of the venv-internal __pycache__ dirs are in scope.
    for venv_name in (".venv", "venv", "env", ".backtest_venv"):
        assert all(venv_name not in line for line in rendered), (
            f"venv {venv_name!r} leaked into targets: {rendered!r}"
        )
