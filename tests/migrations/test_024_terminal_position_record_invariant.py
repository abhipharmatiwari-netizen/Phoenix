from __future__ import annotations

from pathlib import Path


def _migration_sql() -> str:
    return (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "024_terminal_position_record_invariant.sql"
    ).read_text(encoding="utf-8")


def test_terminal_position_record_migration_zeroes_stale_terminal_net_qty() -> None:
    sql = " ".join(_migration_sql().split())

    assert "UPDATE public.internal_position_records" in sql
    assert "SET net_qty = 0" in sql
    assert "unrealized_pnl = 0" in sql
    assert "position_state IN ('FLAT', 'NONE')" in sql
    assert "ABS(COALESCE(net_qty, 0)) > 0.0001" in sql


def test_terminal_position_record_migration_adds_check_constraint() -> None:
    sql = " ".join(_migration_sql().split())

    assert "chk_internal_position_records_terminal_net_qty_zero" in sql
    assert "ADD CONSTRAINT chk_internal_position_records_terminal_net_qty_zero" in sql
    assert "position_state NOT IN ('FLAT', 'NONE')" in sql
    assert "ABS(COALESCE(net_qty, 0)) <= 0.0001" in sql


def test_terminal_position_record_migration_audits_before_state() -> None:
    sql = _migration_sql()

    assert "INSERT INTO public.audit_events" in sql
    assert "terminal_position_record_net_qty_zeroed" in sql
    assert "'net_qty', net_qty" in sql
    assert "'net_qty', 0" in sql
