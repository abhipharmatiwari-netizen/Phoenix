"""Health checks for durable internal position-record invariants."""

from __future__ import annotations

from typing import Optional

from app.config.settings import Settings
from app.data.postgres import connect_with_retry, get_control_plane_dsn


def count_terminal_nonzero_position_records(
    settings: Optional[Settings] = None,
) -> int:
    """Return terminal records whose durable net quantity is not flat."""

    dsn = get_control_plane_dsn(settings)
    sql = """
        SELECT COUNT(*)
        FROM public.internal_position_records
        WHERE position_state IN ('FLAT', 'NONE')
          AND ABS(COALESCE(net_qty, 0)) > 0.0001
    """
    with connect_with_retry(
        dsn,
        autocommit=True,
        max_attempts=1,
        base_backoff_seconds=0.0,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
    return int((row or [0])[0] or 0)


__all__ = ["count_terminal_nonzero_position_records"]
