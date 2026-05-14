"""Static validation for migration 020_strategy_config_candidates.sql.

This file does not exercise a live Postgres — it asserts structural
properties of the migration SQL so accidental drift (missing FK, missing
CHECK, missing index, missing required column) trips CI before the
migration reaches a database. The matching `db-preflight` step inside
docker-compose.oci-live.yml / docker-compose.live.single.yml will catch
schema gaps at deploy time; this test catches gaps at PR time.

Issue: #271 (epic: #270).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "020_strategy_config_candidates.sql"
)


@pytest.fixture(scope="module")
def migration_sql() -> str:
    assert _MIGRATION_PATH.exists(), (
        f"Migration file not found: {_MIGRATION_PATH}. "
        "If it was renamed, update this test alongside the rename."
    )
    return _MIGRATION_PATH.read_text(encoding="utf-8")


def _normalize(sql: str) -> str:
    """Collapse whitespace for tolerant substring matching."""
    return re.sub(r"\s+", " ", sql)


def test_creates_strategy_config_candidates_table(migration_sql: str) -> None:
    norm = _normalize(migration_sql)
    assert "CREATE TABLE IF NOT EXISTS public.strategy_config_candidates" in norm, (
        "Migration must create public.strategy_config_candidates (idempotent)."
    )


@pytest.mark.parametrize(
    "column_def_fragment",
    [
        "candidate_id TEXT PRIMARY KEY",
        "strategy_config_id TEXT NOT NULL",
        "params JSONB NOT NULL",
        "metrics JSONB NOT NULL",
        "backtest_window DATERANGE NOT NULL",
        "optimizer_version TEXT NOT NULL",
        "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
        "reviewed_at TIMESTAMPTZ",
        "reviewed_by TEXT",
        "status TEXT NOT NULL DEFAULT 'pending'",
    ],
)
def test_required_columns_present(migration_sql: str, column_def_fragment: str) -> None:
    norm = _normalize(migration_sql)
    assert column_def_fragment in norm, (
        f"Required column definition missing or altered: {column_def_fragment!r}. "
        "If the schema is intentionally changing, update this test and the admin "
        "API (#275) writer in lockstep."
    )


def test_fk_to_strategy_configs_no_cascade(migration_sql: str) -> None:
    norm = _normalize(migration_sql)
    assert "REFERENCES public.strategy_configs(strategy_config_id)" in norm, (
        "strategy_config_id must FK to strategy_configs.strategy_config_id."
    )
    # FK must NOT cascade delete — audit trail of candidates must outlive
    # ad-hoc deletes of a strategy_config row. The optimizer pipeline is
    # for a real-money system; losing candidate history would erode
    # post-incident review.
    assert "ON DELETE CASCADE" not in norm, (
        "FK to strategy_configs must NOT use ON DELETE CASCADE — "
        "candidates are part of the production audit trail."
    )


def test_status_check_constraint_lists_all_lifecycle_states(migration_sql: str) -> None:
    norm = _normalize(migration_sql)
    for state in ("pending", "approved", "rejected", "promoted", "superseded"):
        assert f"'{state}'" in norm, (
            f"status CHECK constraint missing lifecycle state: {state!r}. "
            "The full lifecycle is documented in the migration header."
        )
    assert "CHECK (status IN (" in norm, (
        "status column must enforce a CHECK constraint, not just a default."
    )


def test_indexes_present(migration_sql: str) -> None:
    norm = _normalize(migration_sql)
    assert (
        "CREATE INDEX IF NOT EXISTS idx_strategy_config_candidates_cfg_status "
        "ON public.strategy_config_candidates (strategy_config_id, status)"
    ) in norm, (
        "Missing (strategy_config_id, status) index. Admin API #275 reads "
        "pending candidates per strategy on every dashboard refresh."
    )
    assert (
        "CREATE INDEX IF NOT EXISTS idx_strategy_config_candidates_created_at "
        "ON public.strategy_config_candidates (created_at DESC)"
    ) in norm, (
        "Missing created_at DESC index. Used by 'newest pending first' query."
    )


def test_table_and_critical_column_comments_present(migration_sql: str) -> None:
    norm = _normalize(migration_sql)
    assert "COMMENT ON TABLE public.strategy_config_candidates IS" in norm
    for column in ("params", "metrics", "backtest_window", "optimizer_version", "status"):
        assert (
            f"COMMENT ON COLUMN public.strategy_config_candidates.{column} IS"
            in norm
        ), f"Missing COMMENT ON COLUMN for {column!r}; required for self-documenting schema."


def test_migration_filename_matches_sequence() -> None:
    # Sanity: the alphabetical migration runner (scripts/run_migrations.sh)
    # applies files in lexicographic order. The new file must sort after
    # the prior head (019_position_trailing_lock_inflight.sql) for the
    # FK target (strategy_configs, created in 011) to already exist.
    migrations_dir = _MIGRATION_PATH.parent
    sql_files = sorted(p.name for p in migrations_dir.glob("*.sql"))
    assert _MIGRATION_PATH.name in sql_files
    idx = sql_files.index(_MIGRATION_PATH.name)
    assert idx > 0, "Migration 020 must not be the first file."
    prior = sql_files[idx - 1]
    assert prior < _MIGRATION_PATH.name, (
        f"Migration 020 must sort after {prior!r}; otherwise the FK target "
        "may not exist when 020 is applied on a fresh database."
    )
