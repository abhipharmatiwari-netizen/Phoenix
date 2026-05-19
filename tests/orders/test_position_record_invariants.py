from __future__ import annotations

from app.orders import position_record_invariants as invariants


class _Cursor:
    def __init__(self) -> None:
        self.sql = ""

    def execute(self, sql):
        self.sql = sql

    def fetchone(self):
        return (3,)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Conn:
    def __init__(self) -> None:
        self.cursor_obj = _Cursor()

    def cursor(self):
        return self.cursor_obj

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_count_terminal_nonzero_position_records_queries_terminal_states(monkeypatch):
    conn = _Conn()
    monkeypatch.setattr(invariants, "get_control_plane_dsn", lambda *_args: "dsn")
    monkeypatch.setattr(invariants, "connect_with_retry", lambda *_args, **_kw: conn)

    count = invariants.count_terminal_nonzero_position_records()

    assert count == 3
    assert "position_state IN ('FLAT', 'NONE')" in conn.cursor_obj.sql
    assert "ABS(COALESCE(net_qty, 0)) > 0.0001" in conn.cursor_obj.sql
