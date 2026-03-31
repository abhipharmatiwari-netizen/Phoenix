"""
Profit lock state manager for trailing daily profit protection.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional, Protocol, Tuple, runtime_checkable
from zoneinfo import ZoneInfo

from app.core.identifiers import BrokerAccountId, TenantId

logger = logging.getLogger(__name__)


# Track trailing profit lock state for an account.
@dataclass
class ProfitLockState:
    tenant_id: str
    broker_account_id: str
    last_reset_date: str
    target_reached: bool = False
    peak_total_pnl: float = 0.0
    last_update_ts: Optional[str] = None
    last_exit_attempt_ts: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        """Serialize state to a plain dict (suitable for JSON/JSONB storage)."""
        return {
            "tenant_id": self.tenant_id,
            "broker_account_id": self.broker_account_id,
            "last_reset_date": self.last_reset_date,
            "target_reached": self.target_reached,
            "peak_total_pnl": self.peak_total_pnl,
            "last_update_ts": self.last_update_ts,
            "last_exit_attempt_ts": self.last_exit_attempt_ts,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "ProfitLockState":
        """Deserialize state from a plain dict."""
        return cls(
            tenant_id=str(data.get("tenant_id", "")),
            broker_account_id=str(data.get("broker_account_id", "")),
            last_reset_date=str(data.get("last_reset_date", "")),
            target_reached=bool(data.get("target_reached", False)),
            peak_total_pnl=float(data.get("peak_total_pnl", 0.0)),  # type: ignore[arg-type]
            last_update_ts=data.get("last_update_ts") if data.get("last_update_ts") is not None else None,  # type: ignore[arg-type]
            last_exit_attempt_ts=data.get("last_exit_attempt_ts") if data.get("last_exit_attempt_ts") is not None else None,  # type: ignore[arg-type]
        )


# Decision result for profit lock evaluation.
@dataclass
class ProfitLockDecision:
    lock_active: bool
    lock_floor: Optional[float]
    peak_total_pnl: float
    exit_required: bool
    exit_reason: Optional[str]
    signal_valid: Optional[bool]
    target: Optional[float]


@runtime_checkable
class ProfitLockPersistenceBackend(Protocol):
    """Protocol for profit lock state persistence."""

    def load_state(
        self, tenant_id: str, broker_account_id: str
    ) -> Optional[ProfitLockState]: ...

    def save_state(self, state: ProfitLockState) -> None: ...


class PostgresProfitLockBackend:
    """Persist profit lock state to Postgres (for LIVE mode)."""

    _CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS profit_lock_state (
        tenant_id            TEXT NOT NULL,
        broker_account_id    TEXT NOT NULL,
        last_reset_date      TEXT NOT NULL,
        target_reached       BOOLEAN NOT NULL DEFAULT FALSE,
        peak_total_pnl       DOUBLE PRECISION NOT NULL DEFAULT 0.0,
        last_update_ts       TEXT,
        last_exit_attempt_ts TEXT,
        updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (tenant_id, broker_account_id)
    );
    """

    _UPSERT = """
    INSERT INTO profit_lock_state
        (tenant_id, broker_account_id, last_reset_date,
         target_reached, peak_total_pnl, last_update_ts,
         last_exit_attempt_ts, updated_at)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (tenant_id, broker_account_id) DO UPDATE SET
        last_reset_date      = EXCLUDED.last_reset_date,
        target_reached       = EXCLUDED.target_reached,
        peak_total_pnl       = EXCLUDED.peak_total_pnl,
        last_update_ts       = EXCLUDED.last_update_ts,
        last_exit_attempt_ts = EXCLUDED.last_exit_attempt_ts,
        updated_at           = EXCLUDED.updated_at;
    """

    _SELECT = """
    SELECT last_reset_date, target_reached, peak_total_pnl,
           last_update_ts, last_exit_attempt_ts
    FROM profit_lock_state
    WHERE tenant_id = %s AND broker_account_id = %s;
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
        self, tenant_id: str, broker_account_id: str
    ) -> Optional[ProfitLockState]:
        conn = self._connect()
        try:
            cur = conn.execute(self._SELECT, (tenant_id, broker_account_id))
            row = cur.fetchone()
            if row is None:
                return None
            return ProfitLockState(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                last_reset_date=row[0],
                target_reached=bool(row[1]),
                peak_total_pnl=float(row[2]),
                last_update_ts=row[3],
                last_exit_attempt_ts=row[4],
            )
        except Exception:
            logger.exception("profit_lock: failed to load state for %s/%s", tenant_id, broker_account_id)
            return None
        finally:
            conn.close()

    def save_state(self, state: ProfitLockState) -> None:
        conn = self._connect()
        try:
            conn.execute(
                self._UPSERT,
                (
                    state.tenant_id,
                    state.broker_account_id,
                    state.last_reset_date,
                    state.target_reached,
                    state.peak_total_pnl,
                    state.last_update_ts,
                    state.last_exit_attempt_ts,
                    datetime.now(timezone.utc),
                ),
            )
        except Exception:
            logger.exception("profit_lock: failed to save state for %s/%s", state.tenant_id, state.broker_account_id)
        finally:
            conn.close()


class _NoopProfitLockBackend:
    """No-op backend for non-LIVE modes (paper, backtest)."""

    def load_state(
        self, tenant_id: str, broker_account_id: str
    ) -> Optional[ProfitLockState]:
        return None

    def save_state(self, state: ProfitLockState) -> None:
        pass


class ProfitLockManager:
    """
    In-memory manager for profit lock state with daily reset.

    - Activates when total PnL >= target.
    - Trails peak PnL by a giveback percentage.
    - Can trigger exit if signal becomes invalid.
    """

    # Initialize state tracking and time zone.
    def __init__(
        self,
        default_time_zone: str = "Asia/Kolkata",
        backend: Optional[ProfitLockPersistenceBackend] = None,
    ) -> None:
        self._tz = ZoneInfo(default_time_zone)
        self._states: Dict[Tuple[str, str], ProfitLockState] = {}
        self._backend: ProfitLockPersistenceBackend = backend or _NoopProfitLockBackend()

    # Return today's date string in the configured time zone.
    def _today(self) -> str:
        now = datetime.now(self._tz)
        return now.strftime("%Y-%m-%d")

    # Persist state to the backend (best-effort).
    def _persist(self, state: ProfitLockState) -> None:
        self._backend.save_state(state)

    # Get or create the profit lock state for a tenant/account.
    def _get_state(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
    ) -> ProfitLockState:
        key = (str(tenant_id), str(broker_account_id))
        state = self._states.get(key)
        today = self._today()

        # Try loading from backend if not in memory.
        if state is None:
            state = self._backend.load_state(str(tenant_id), str(broker_account_id))
            if state is not None:
                self._states[key] = state

        if state is None or state.last_reset_date != today:
            state = ProfitLockState(
                tenant_id=str(tenant_id),
                broker_account_id=str(broker_account_id),
                last_reset_date=today,
            )
            self._states[key] = state
            self._persist(state)
        return state

    # Evaluate profit lock status and whether an exit is required.
    def evaluate(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        total_pnl: Optional[float],
        target: Optional[float],
        giveback_pct: float,
        signal_valid: Optional[bool],
        require_signal: bool,
        exit_cooldown_seconds: float,
    ) -> ProfitLockDecision:
        state = self._get_state(tenant_id, broker_account_id)
        now = datetime.now(self._tz)
        now_iso = now.isoformat()

        total_val = float(total_pnl or 0.0)
        if target is None or target <= 0:
            state.last_update_ts = now_iso
            self._persist(state)
            return ProfitLockDecision(
                lock_active=False,
                lock_floor=None,
                peak_total_pnl=state.peak_total_pnl,
                exit_required=False,
                exit_reason="target_not_configured",
                signal_valid=signal_valid,
                target=target,
            )

        if not state.target_reached and total_val >= float(target):
            state.target_reached = True
            state.peak_total_pnl = total_val

        lock_floor: Optional[float] = None
        exit_required = False
        exit_reason: Optional[str] = None

        if state.target_reached:
            if total_val > state.peak_total_pnl:
                state.peak_total_pnl = total_val

            lock_floor = max(float(target), state.peak_total_pnl * (1.0 - giveback_pct))

            if require_signal and signal_valid is False:
                exit_required = True
                exit_reason = "signal_invalid"
            elif total_val < lock_floor:
                exit_required = True
                exit_reason = "giveback_breach"

            if exit_required and exit_cooldown_seconds > 0:
                last_ts = state.last_exit_attempt_ts
                if last_ts:
                    try:
                        last_dt = datetime.fromisoformat(last_ts)
                        elapsed = (now - last_dt).total_seconds()
                        if elapsed < float(exit_cooldown_seconds):
                            exit_required = False
                            exit_reason = "exit_cooldown"
                    except Exception:
                        pass

            if exit_required:
                state.last_exit_attempt_ts = now_iso

        state.last_update_ts = now_iso
        self._persist(state)

        return ProfitLockDecision(
            lock_active=state.target_reached,
            lock_floor=lock_floor,
            peak_total_pnl=state.peak_total_pnl,
            exit_required=exit_required,
            exit_reason=exit_reason,
            signal_valid=signal_valid,
            target=target,
        )


__all__ = [
    "ProfitLockDecision",
    "ProfitLockManager",
    "ProfitLockPersistenceBackend",
    "ProfitLockState",
    "PostgresProfitLockBackend",
]
