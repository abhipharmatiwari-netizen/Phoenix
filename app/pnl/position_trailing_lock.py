"""
Per-position trailing profit lock.

Each open position has its own peak unrealized PnL, tracked since entry.
When current unrealized PnL falls below peak * (1 - giveback_pct), and the
peak has crossed an arming floor (e.g. >= +Rs.500), the engine emits an
exit order for that single position via the existing OrderRouter exit path.

State is keyed by (tenant_id, broker_account_id, symbol) and persisted to
Postgres so peaks survive restarts. State is cleared when the position
closes (qty = 0).

This is parallel to ProfitLockManager, which operates on account-level
total PnL and only activates after a daily target is reached. The two are
independent and may both be active simultaneously.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Protocol, Tuple, runtime_checkable
from zoneinfo import ZoneInfo

from app.core.identifiers import BrokerAccountId, TenantId

logger = logging.getLogger(__name__)


@dataclass
class PositionTrailingLockState:
    tenant_id: str
    broker_account_id: str
    symbol: str
    peak_unrealized_pnl: float = 0.0
    armed: bool = False
    opened_at: Optional[str] = None
    last_update_ts: Optional[str] = None
    last_exit_attempt_ts: Optional[str] = None


@dataclass
class PositionTrailingLockDecision:
    symbol: str
    peak_unrealized_pnl: float
    current_unrealized_pnl: float
    armed: bool
    lock_floor: Optional[float]
    exit_required: bool
    exit_reason: Optional[str]


@runtime_checkable
class PositionTrailingLockBackend(Protocol):
    def load_state(
        self, tenant_id: str, broker_account_id: str, symbol: str
    ) -> Optional[PositionTrailingLockState]: ...

    def save_state(self, state: PositionTrailingLockState) -> None: ...

    def delete_state(self, tenant_id: str, broker_account_id: str, symbol: str) -> None: ...

    def list_states(
        self, tenant_id: str, broker_account_id: str
    ) -> Iterable[PositionTrailingLockState]: ...


class PostgresPositionTrailingLockBackend:
    _CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS position_trailing_lock_state (
        tenant_id            TEXT NOT NULL,
        broker_account_id    TEXT NOT NULL,
        symbol               TEXT NOT NULL,
        peak_unrealized_pnl  DOUBLE PRECISION NOT NULL DEFAULT 0.0,
        armed                BOOLEAN NOT NULL DEFAULT FALSE,
        opened_at            TEXT,
        last_update_ts       TEXT,
        last_exit_attempt_ts TEXT,
        updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (tenant_id, broker_account_id, symbol)
    );
    """

    _UPSERT = """
    INSERT INTO position_trailing_lock_state
        (tenant_id, broker_account_id, symbol, peak_unrealized_pnl, armed,
         opened_at, last_update_ts, last_exit_attempt_ts, updated_at)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (tenant_id, broker_account_id, symbol) DO UPDATE SET
        peak_unrealized_pnl  = EXCLUDED.peak_unrealized_pnl,
        armed                = EXCLUDED.armed,
        opened_at            = COALESCE(position_trailing_lock_state.opened_at, EXCLUDED.opened_at),
        last_update_ts       = EXCLUDED.last_update_ts,
        last_exit_attempt_ts = EXCLUDED.last_exit_attempt_ts,
        updated_at           = EXCLUDED.updated_at;
    """

    _SELECT = """
    SELECT peak_unrealized_pnl, armed, opened_at,
           last_update_ts, last_exit_attempt_ts
    FROM position_trailing_lock_state
    WHERE tenant_id = %s AND broker_account_id = %s AND symbol = %s;
    """

    _LIST = """
    SELECT symbol, peak_unrealized_pnl, armed, opened_at,
           last_update_ts, last_exit_attempt_ts
    FROM position_trailing_lock_state
    WHERE tenant_id = %s AND broker_account_id = %s;
    """

    _DELETE = """
    DELETE FROM position_trailing_lock_state
    WHERE tenant_id = %s AND broker_account_id = %s AND symbol = %s;
    """

    def __init__(self, dsn: str) -> None:
        from app.data.postgres import connect_with_retry

        self._dsn = dsn
        conn = connect_with_retry(dsn)
        try:
            conn.execute(self._CREATE_TABLE)
        finally:
            conn.close()

    def _connect(self):  # noqa: ANN202
        from app.data.postgres import connect_with_retry

        return connect_with_retry(self._dsn)

    def load_state(
        self, tenant_id: str, broker_account_id: str, symbol: str
    ) -> Optional[PositionTrailingLockState]:
        conn = self._connect()
        try:
            cur = conn.execute(self._SELECT, (tenant_id, broker_account_id, symbol))
            row = cur.fetchone()
            if row is None:
                return None
            return PositionTrailingLockState(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                symbol=symbol,
                peak_unrealized_pnl=float(row[0] or 0.0),
                armed=bool(row[1]),
                opened_at=row[2],
                last_update_ts=row[3],
                last_exit_attempt_ts=row[4],
            )
        except Exception:
            logger.exception(
                "position_trailing_lock: load_state failed %s/%s/%s",
                tenant_id,
                broker_account_id,
                symbol,
            )
            return None
        finally:
            conn.close()

    def save_state(self, state: PositionTrailingLockState) -> None:
        conn = self._connect()
        try:
            conn.execute(
                self._UPSERT,
                (
                    state.tenant_id,
                    state.broker_account_id,
                    state.symbol,
                    state.peak_unrealized_pnl,
                    state.armed,
                    state.opened_at,
                    state.last_update_ts,
                    state.last_exit_attempt_ts,
                    datetime.now(timezone.utc),
                ),
            )
        except Exception:
            logger.exception(
                "position_trailing_lock: save_state failed %s/%s/%s",
                state.tenant_id,
                state.broker_account_id,
                state.symbol,
            )
        finally:
            conn.close()

    def delete_state(self, tenant_id: str, broker_account_id: str, symbol: str) -> None:
        conn = self._connect()
        try:
            conn.execute(self._DELETE, (tenant_id, broker_account_id, symbol))
        except Exception:
            logger.exception(
                "position_trailing_lock: delete_state failed %s/%s/%s",
                tenant_id,
                broker_account_id,
                symbol,
            )
        finally:
            conn.close()

    def list_states(
        self, tenant_id: str, broker_account_id: str
    ) -> Iterable[PositionTrailingLockState]:
        conn = self._connect()
        try:
            cur = conn.execute(self._LIST, (tenant_id, broker_account_id))
            for row in cur.fetchall():
                yield PositionTrailingLockState(
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    symbol=row[0],
                    peak_unrealized_pnl=float(row[1] or 0.0),
                    armed=bool(row[2]),
                    opened_at=row[3],
                    last_update_ts=row[4],
                    last_exit_attempt_ts=row[5],
                )
        except Exception:
            logger.exception(
                "position_trailing_lock: list_states failed %s/%s",
                tenant_id,
                broker_account_id,
            )
            return
        finally:
            conn.close()


class _NoopPositionTrailingLockBackend:
    def load_state(
        self, tenant_id: str, broker_account_id: str, symbol: str
    ) -> Optional[PositionTrailingLockState]:
        return None

    def save_state(self, state: PositionTrailingLockState) -> None:
        return None

    def delete_state(self, tenant_id: str, broker_account_id: str, symbol: str) -> None:
        return None

    def list_states(
        self, tenant_id: str, broker_account_id: str
    ) -> Iterable[PositionTrailingLockState]:
        return iter(())


class PositionTrailingLockManager:
    """In-memory + persistent manager for per-position trailing profit locks."""

    def __init__(
        self,
        default_time_zone: str = "Asia/Kolkata",
        backend: Optional[PositionTrailingLockBackend] = None,
    ) -> None:
        self._tz = ZoneInfo(default_time_zone)
        self._states: Dict[Tuple[str, str, str], PositionTrailingLockState] = {}
        self._backend: PositionTrailingLockBackend = backend or _NoopPositionTrailingLockBackend()

    def _now_iso(self) -> str:
        return datetime.now(self._tz).isoformat()

    def _persist(self, state: PositionTrailingLockState) -> None:
        self._backend.save_state(state)

    def _key(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        symbol: str,
    ) -> Tuple[str, str, str]:
        return (str(tenant_id), str(broker_account_id), str(symbol))

    def _get_state(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        symbol: str,
    ) -> PositionTrailingLockState:
        key = self._key(tenant_id, broker_account_id, symbol)
        state = self._states.get(key)
        if state is None:
            loaded = self._backend.load_state(*key)
            if loaded is not None:
                self._states[key] = loaded
                state = loaded
        if state is None:
            state = PositionTrailingLockState(
                tenant_id=key[0],
                broker_account_id=key[1],
                symbol=key[2],
                opened_at=self._now_iso(),
            )
            self._states[key] = state
            self._persist(state)
        return state

    def reset_position(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        symbol: str,
    ) -> None:
        """Clear in-memory + persisted state for a closed position."""
        key = self._key(tenant_id, broker_account_id, symbol)
        self._states.pop(key, None)
        self._backend.delete_state(*key)

    def evaluate(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        symbol: str,
        current_unrealized_pnl: float,
        floor_inr: float,
        giveback_pct: float,
        exit_cooldown_seconds: float,
    ) -> PositionTrailingLockDecision:
        """Update peak + decide whether this position should be exited.

        Args:
            current_unrealized_pnl: live unrealized P&L for this single position.
            floor_inr: minimum peak (in INR) before trailing arms. The lock will
                NOT fire on a peak below this value, preventing churn on tiny
                wiggles. Must be > 0.
            giveback_pct: fraction of peak P&L that triggers exit when current
                falls below it. e.g. 0.10 => exit when current < peak * 0.90.
            exit_cooldown_seconds: minimum seconds between consecutive exit
                attempts for the same (account, symbol) — prevents repeat exits
                while a previous order is in flight.

        Returns:
            PositionTrailingLockDecision describing the new peak, the lock
            floor (if armed), and whether an exit is required NOW.
        """
        state = self._get_state(tenant_id, broker_account_id, symbol)
        now_iso = self._now_iso()
        cur = float(current_unrealized_pnl or 0.0)

        if cur > state.peak_unrealized_pnl:
            state.peak_unrealized_pnl = cur

        if not state.armed and state.peak_unrealized_pnl >= float(floor_inr):
            state.armed = True
            logger.info(
                "position_trailing_lock.armed tenant=%s account=%s symbol=%s peak=%.2f floor=%.2f",
                state.tenant_id,
                state.broker_account_id,
                state.symbol,
                state.peak_unrealized_pnl,
                float(floor_inr),
            )

        lock_floor: Optional[float] = None
        exit_required = False
        exit_reason: Optional[str] = None

        if state.armed:
            lock_floor = state.peak_unrealized_pnl * (1.0 - float(giveback_pct))
            if cur < lock_floor:
                if exit_cooldown_seconds > 0 and state.last_exit_attempt_ts:
                    try:
                        last_dt = datetime.fromisoformat(state.last_exit_attempt_ts)
                        now_dt = datetime.fromisoformat(now_iso)
                        elapsed = (now_dt - last_dt).total_seconds()
                        if elapsed < float(exit_cooldown_seconds):
                            exit_required = False
                            exit_reason = "exit_cooldown"
                        else:
                            exit_required = True
                            exit_reason = "position_giveback_breach"
                    except Exception:
                        exit_required = True
                        exit_reason = "position_giveback_breach"
                else:
                    exit_required = True
                    exit_reason = "position_giveback_breach"

            if exit_required:
                state.last_exit_attempt_ts = now_iso

        state.last_update_ts = now_iso
        self._persist(state)

        return PositionTrailingLockDecision(
            symbol=state.symbol,
            peak_unrealized_pnl=state.peak_unrealized_pnl,
            current_unrealized_pnl=cur,
            armed=state.armed,
            lock_floor=lock_floor,
            exit_required=exit_required,
            exit_reason=exit_reason,
        )


# ---------------------------------------------------------------------------
# Issue #251: durable inflight markers for the trailing-lock duplicate-fill
# guard. The in-memory dict on ``PositionTrailingLockEngine`` does not survive
# restarts; a process restart between submit_order and the broker's terminal
# confirmation drops the ONLY guard against re-submitting the same exit on
# the next watchdog cycle. We persist the markers to a tiny Postgres table
# (migration 019) so restart no longer disables the guard.
# ---------------------------------------------------------------------------


@dataclass
class PositionTrailingLockInflightMarker:
    """One persisted inflight marker.

    Stored per (tenant, account, symbol) while a trailing-lock exit is awaiting
    broker terminal confirmation. ``submitted_at`` is the UTC wall-clock instant
    the marker was armed; the engine compares the age against the configured
    inflight max age to decide when to auto-clear after a hard timeout.

    PR #261 round-3 review P2 (exit_engines.py:2509): ``exit_side`` and
    ``exit_broker_units`` are persisted alongside the marker so that the
    unknown-broker-order-id terminal-evidence fallback can match the right
    ledger row even after a process restart. Without these, hydration
    restores the marker but the in-memory ``_inflight_exit_specs`` dict
    is empty, so the post-restart fallback can never recognise a matching
    FILLED exit row and the marker is forced to wait for the inflight
    timeout. Both fields are Optional because legacy persisted rows
    written before this round-3 change have NULL in the new columns;
    the engine then falls back to the broker_order_id strict-match
    path and the spec capture in ``_emit_exit`` repopulates the in-
    memory dict on the next arm.
    """

    tenant_id: str
    broker_account_id: str
    symbol: str
    broker_order_id: Optional[str]
    submitted_at: datetime
    exit_side: Optional[str] = None
    exit_broker_units: Optional[int] = None


@runtime_checkable
class PositionTrailingLockInflightBackend(Protocol):
    # PR #261 round-2 review (root cause): every persistence method accepts
    # ``raise_on_failure`` so LIVE callers can opt into the fail-closed
    # contract that the engine layer's ``fail_closed=True`` gates were
    # designed to enforce. The historical default (``False``) preserves the
    # PAPER/SHADOW best-effort log-and-continue semantics that several test
    # fixtures and the legacy non-LIVE call sites rely on.
    def save_marker(
        self,
        marker: PositionTrailingLockInflightMarker,
        *,
        raise_on_failure: bool = False,
    ) -> None: ...

    def delete_marker(
        self,
        tenant_id: str,
        broker_account_id: str,
        symbol: str,
        *,
        raise_on_failure: bool = False,
    ) -> None: ...

    def load_all(
        self, *, raise_on_failure: bool = False,
    ) -> Iterable[PositionTrailingLockInflightMarker]: ...


class _NoopPositionTrailingLockInflightBackend:
    """No-op backend used by tests and non-LIVE runs.

    Persists nothing; the in-memory dict on the engine remains authoritative.
    Safe to use in PAPER/SHADOW because no real broker order is at stake.
    """

    def save_marker(
        self,
        marker: PositionTrailingLockInflightMarker,
        *,
        raise_on_failure: bool = False,
    ) -> None:
        return None

    def delete_marker(
        self,
        tenant_id: str,
        broker_account_id: str,
        symbol: str,
        *,
        raise_on_failure: bool = False,
    ) -> None:
        return None

    def load_all(
        self, *, raise_on_failure: bool = False,
    ) -> Iterable[PositionTrailingLockInflightMarker]:
        return iter(())


class PostgresPositionTrailingLockInflightBackend:
    """Postgres backend for inflight trailing-lock markers (issue #251).

    Idempotent ``CREATE TABLE IF NOT EXISTS`` mirrors migration 019. Each
    call opens a fresh connection (matching the existing
    PostgresPositionTrailingLockBackend pattern) so the backend can be safely
    shared across the engine's async lifetime.

    Failure mode is selectable per-call via ``raise_on_failure``:

    * ``raise_on_failure=False`` (default) — log + return / yield empty.
      This preserves the original best-effort semantics used by PAPER/
      SHADOW tests and any caller that explicitly wants to degrade to
      in-memory-only on a transient Postgres outage.
    * ``raise_on_failure=True`` — propagate the underlying exception so
      the caller can enforce a fail-closed contract. LIVE call sites in
      ``PositionTrailingLockEngine`` opt into this so the engine's
      ``fail_closed=True`` gates (PR #261 round-1) actually see the
      backend failure rather than a silent empty/None.

    PR #261 round-2 review (root cause): the round-1 fix added the
    ``fail_closed`` parameter on the engine, but the backend continued
    to swallow Postgres failures internally, so the engine-layer gate
    never fired. The ``raise_on_failure`` flag is the round-2 fix —
    every LIVE call site MUST opt in.
    """

    _CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS position_trailing_lock_inflight (
        tenant_id           TEXT        NOT NULL,
        broker_account_id   TEXT        NOT NULL,
        symbol              TEXT        NOT NULL,
        broker_order_id     TEXT,
        submitted_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
        exit_side           TEXT,
        exit_broker_units   INTEGER,
        updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (tenant_id, broker_account_id, symbol)
    );
    """

    # PR #261 round-3 review P2 (exit_engines.py:2509): idempotent
    # ALTER TABLE adds the new columns on existing deployments where
    # the table was created by an older migration (or an older
    # ``CREATE TABLE IF NOT EXISTS`` revision of this module).
    # ``ADD COLUMN IF NOT EXISTS`` is supported by Postgres 9.6+ and
    # is safe to run on every backend init.
    _ALTER_ADD_COLUMNS = (
        (
            "ALTER TABLE position_trailing_lock_inflight "
            "ADD COLUMN IF NOT EXISTS exit_side TEXT;"
        ),
        (
            "ALTER TABLE position_trailing_lock_inflight "
            "ADD COLUMN IF NOT EXISTS exit_broker_units INTEGER;"
        ),
    )

    _UPSERT = """
    INSERT INTO position_trailing_lock_inflight
        (tenant_id, broker_account_id, symbol, broker_order_id,
         submitted_at, exit_side, exit_broker_units, updated_at)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (tenant_id, broker_account_id, symbol) DO UPDATE SET
        broker_order_id   = EXCLUDED.broker_order_id,
        submitted_at      = EXCLUDED.submitted_at,
        exit_side         = EXCLUDED.exit_side,
        exit_broker_units = EXCLUDED.exit_broker_units,
        updated_at        = EXCLUDED.updated_at;
    """

    _DELETE = """
    DELETE FROM position_trailing_lock_inflight
    WHERE tenant_id = %s AND broker_account_id = %s AND symbol = %s;
    """

    _LIST_ALL = """
    SELECT tenant_id, broker_account_id, symbol, broker_order_id,
           submitted_at, exit_side, exit_broker_units
    FROM position_trailing_lock_inflight;
    """

    def __init__(self, dsn: str) -> None:
        from app.data.postgres import connect_with_retry

        self._dsn = dsn
        conn = connect_with_retry(dsn)
        try:
            conn.execute(self._CREATE_TABLE)
            # Idempotent ALTER for existing deployments (round-3 P2).
            for ddl in self._ALTER_ADD_COLUMNS:
                try:
                    conn.execute(ddl)
                except Exception:
                    logger.exception(
                        "position_trailing_lock_inflight: idempotent ALTER "
                        "TABLE failed for `%s` — backend will still operate "
                        "but the round-3 spec round-trip may be degraded.",
                        ddl,
                    )
        finally:
            conn.close()

    def _connect(self):  # noqa: ANN202
        from app.data.postgres import connect_with_retry

        return connect_with_retry(self._dsn)

    def save_marker(
        self,
        marker: PositionTrailingLockInflightMarker,
        *,
        raise_on_failure: bool = False,
    ) -> None:
        try:
            conn = self._connect()
        except Exception:
            logger.exception(
                "position_trailing_lock_inflight: connect failed for save %s/%s/%s",
                marker.tenant_id,
                marker.broker_account_id,
                marker.symbol,
            )
            if raise_on_failure:
                raise
            return
        try:
            now_utc = datetime.now(timezone.utc)
            # PR #261 round-3 review P2 (exit_engines.py:2509): round-trip
            # the exit spec (side + broker units) alongside the marker so
            # the unknown-broker-order-id terminal-evidence fallback can
            # match the right ledger row after a restart. Both columns
            # are NULL-tolerant; legacy markers written before this
            # change persist with NULL and the engine falls back to the
            # broker_order_id strict path until the spec is repopulated.
            exit_side = (
                str(marker.exit_side).strip().upper()
                if marker.exit_side
                else None
            )
            exit_broker_units: Optional[int]
            try:
                exit_broker_units = (
                    int(marker.exit_broker_units)
                    if marker.exit_broker_units is not None
                    else None
                )
            except (TypeError, ValueError):
                exit_broker_units = None
            conn.execute(
                self._UPSERT,
                (
                    marker.tenant_id,
                    marker.broker_account_id,
                    marker.symbol,
                    marker.broker_order_id,
                    marker.submitted_at,
                    exit_side,
                    exit_broker_units,
                    now_utc,
                ),
            )
        except Exception:
            logger.exception(
                "position_trailing_lock_inflight: save_marker failed %s/%s/%s",
                marker.tenant_id,
                marker.broker_account_id,
                marker.symbol,
            )
            if raise_on_failure:
                raise
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def delete_marker(
        self,
        tenant_id: str,
        broker_account_id: str,
        symbol: str,
        *,
        raise_on_failure: bool = False,
    ) -> None:
        try:
            conn = self._connect()
        except Exception:
            logger.exception(
                "position_trailing_lock_inflight: connect failed for delete %s/%s/%s",
                tenant_id,
                broker_account_id,
                symbol,
            )
            if raise_on_failure:
                raise
            return
        try:
            conn.execute(self._DELETE, (tenant_id, broker_account_id, symbol))
        except Exception:
            logger.exception(
                "position_trailing_lock_inflight: delete_marker failed %s/%s/%s",
                tenant_id,
                broker_account_id,
                symbol,
            )
            if raise_on_failure:
                raise
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def load_all(
        self, *, raise_on_failure: bool = False,
    ) -> Iterable[PositionTrailingLockInflightMarker]:
        try:
            conn = self._connect()
        except Exception:
            logger.exception(
                "position_trailing_lock_inflight: connect failed for load_all"
            )
            if raise_on_failure:
                raise
            return []
        out: List[PositionTrailingLockInflightMarker] = []
        try:
            cur = conn.execute(self._LIST_ALL)
            for row in cur.fetchall():
                submitted_at_raw = row[4]
                if isinstance(submitted_at_raw, datetime):
                    submitted_at = submitted_at_raw
                else:
                    try:
                        submitted_at = datetime.fromisoformat(
                            str(submitted_at_raw).replace("Z", "+00:00")
                        )
                    except Exception:
                        # Treat unparseable timestamp as "freshly armed now"
                        # to fail closed — better to hold the marker briefly
                        # than to drop the guard.
                        submitted_at = datetime.now(timezone.utc)
                if submitted_at.tzinfo is None:
                    submitted_at = submitted_at.replace(tzinfo=timezone.utc)
                # PR #261 round-3 review P2 (exit_engines.py:2509):
                # round-trip the persisted exit spec so the engine can
                # repopulate ``_inflight_exit_specs`` on hydration. Rows
                # written before the round-3 change have NULL in these
                # columns; treat as ``None`` and let the engine fall
                # back to the broker_order_id strict-match path.
                # Defensive index access: tolerate older backends that
                # might not surface the new columns yet (e.g. mid-roll
                # deployments where the ALTER TABLE has not yet run).
                try:
                    exit_side_raw = row[5]
                except IndexError:
                    exit_side_raw = None
                try:
                    exit_units_raw = row[6]
                except IndexError:
                    exit_units_raw = None
                exit_side_val: Optional[str] = (
                    str(exit_side_raw).strip().upper()
                    if exit_side_raw not in (None, "")
                    else None
                )
                exit_units_val: Optional[int]
                try:
                    exit_units_val = (
                        int(exit_units_raw)
                        if exit_units_raw is not None
                        else None
                    )
                except (TypeError, ValueError):
                    exit_units_val = None
                if exit_units_val is not None and exit_units_val <= 0:
                    exit_units_val = None
                out.append(
                    PositionTrailingLockInflightMarker(
                        tenant_id=str(row[0] or ""),
                        broker_account_id=str(row[1] or ""),
                        symbol=str(row[2] or ""),
                        broker_order_id=(str(row[3]) if row[3] is not None else None),
                        submitted_at=submitted_at,
                        exit_side=exit_side_val,
                        exit_broker_units=exit_units_val,
                    )
                )
        except Exception:
            logger.exception(
                "position_trailing_lock_inflight: load_all failed"
            )
            if raise_on_failure:
                raise
            return []
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return out


__all__ = [
    "PositionTrailingLockBackend",
    "PositionTrailingLockDecision",
    "PositionTrailingLockInflightBackend",
    "PositionTrailingLockInflightMarker",
    "PositionTrailingLockManager",
    "PositionTrailingLockState",
    "PostgresPositionTrailingLockBackend",
    "PostgresPositionTrailingLockInflightBackend",
    "_NoopPositionTrailingLockInflightBackend",
]
