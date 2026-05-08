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
