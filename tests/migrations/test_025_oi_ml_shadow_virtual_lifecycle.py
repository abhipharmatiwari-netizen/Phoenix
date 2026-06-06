from __future__ import annotations

from pathlib import Path


def _migration_sql() -> str:
    return (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "025_oi_ml_shadow_virtual_lifecycle.sql"
    ).read_text(encoding="utf-8")


def test_oi_ml_shadow_virtual_lifecycle_columns_are_additive() -> None:
    sql = " ".join(_migration_sql().split())

    assert "ALTER TABLE public.oi_ml_shadow_order_intents" in sql
    assert "ADD COLUMN IF NOT EXISTS virtual_entry_at" in sql
    assert "ADD COLUMN IF NOT EXISTS virtual_exit_at" in sql
    assert "ADD COLUMN IF NOT EXISTS virtual_flat_at" in sql
    assert "ADD COLUMN IF NOT EXISTS realized_pnl_rupees" in sql
    assert "ADD COLUMN IF NOT EXISTS lifecycle_events" in sql


def test_oi_ml_shadow_virtual_lifecycle_status_constraint_is_dry_run_only() -> None:
    sql = " ".join(_migration_sql().split())

    assert "oi_ml_shadow_order_intents_status_check" in sql
    assert "'VIRTUAL_FILLED'" in sql
    assert "'FLAT'" in sql
    assert "live order queue" in _migration_sql()
