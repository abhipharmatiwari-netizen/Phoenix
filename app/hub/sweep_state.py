"""Persistent state management for dual profit sweep strategies.

This module provides distributed state tracking for profit sweep operations,
enabling multi-instance deployments with consistent sweep counts and cooldown
management across all running instances.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Optional, Dict, Tuple
from zoneinfo import ZoneInfo
import logging

try:  # pragma: no cover - optional in lightweight test environments
    from google.cloud import firestore  # type: ignore
except Exception:  # pragma: no cover - optional in lightweight test environments
    firestore = None

try:  # pragma: no cover - optional in lightweight test environments
    import psycopg  # type: ignore
    from psycopg.rows import dict_row  # type: ignore
except Exception:  # pragma: no cover - optional in lightweight test environments
    psycopg = None  # type: ignore[assignment]
    dict_row = None

from app.core.identifiers import BrokerAccountId, TenantId
from app.data.postgres import connect_with_retry

logger = logging.getLogger(__name__)


@dataclass
class SweepState:
    """Persistent state for profit sweep tracking."""
    
    tenant_id: str
    broker_account_id: str
    sweep_count_today: int = 0
    last_sweep_time: Optional[str] = None  # ISO 8601 string
    last_reset_date: Optional[str] = None  # YYYY-MM-DD
    
    # Build SweepState from a Firestore document dict.
    @classmethod
    def from_firestore(cls, doc_data: Dict) -> SweepState:
        """Create SweepState from Firestore document."""
        return cls(
            tenant_id=doc_data.get("tenant_id", ""),
            broker_account_id=doc_data.get("broker_account_id", ""),
            sweep_count_today=doc_data.get("sweep_count_today", 0),
            last_sweep_time=doc_data.get("last_sweep_time"),
            last_reset_date=doc_data.get("last_reset_date"),
        )
    
    # Convert SweepState into Firestore-ready fields.
    def to_firestore(self) -> Dict:
        """Convert to Firestore document format."""
        return {
            "tenant_id": self.tenant_id,
            "broker_account_id": self.broker_account_id,
            "sweep_count_today": self.sweep_count_today,
            "last_sweep_time": self.last_sweep_time,
            "last_reset_date": self.last_reset_date,
            "updated_at": datetime.now(ZoneInfo("UTC")).isoformat(),
        }


class SweepStateStore:
    """Manages persistent sweep state in Firestore.
    
    Handles:
    - Distributed sweep count tracking across multiple instances
    - Cooldown state management with timestamps
    - Daily reset logic with date-based keys
    - Thread-safe operations with optimistic locking
    """
    
    COLLECTION_NAME = "sweep_states"
    
    # Initialize Firestore client and time zone handling.
    def __init__(self, firestore_client, default_time_zone: str = "Asia/Kolkata"):
        """Initialize the sweep state store.
        
        Args:
            firestore_client: Firestore client for database operations
            default_time_zone: Time zone for daily reset logic
        """
        self.firestore_client = firestore_client
        self.default_time_zone = default_time_zone
        self._tz = ZoneInfo(default_time_zone)
    
    # Build the document id for a tenant/account pair.
    def _get_doc_id(self, tenant_id: TenantId, broker_account_id: BrokerAccountId) -> str:
        """Generate document ID for a sweep state entry."""
        return f"{tenant_id}_{broker_account_id}"
    
    # Return today's date string in the configured time zone.
    def _get_today(self) -> str:
        """Get today's date in YYYY-MM-DD format."""
        now = datetime.now(self._tz)
        return now.strftime("%Y-%m-%d")
    
    def get_sweep_state(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
    ) -> SweepState:
        # Fetch sweep state and apply daily reset if needed.
        """Get current sweep state for an account.
        
        Performs daily reset check if needed.
        """
        doc_id = self._get_doc_id(tenant_id, broker_account_id)
        
        try:
            doc = self.firestore_client.collection(self.COLLECTION_NAME).document(doc_id).get()
            
            if not doc.exists:
                # Create new state entry
                state = SweepState(
                    tenant_id=str(tenant_id),
                    broker_account_id=str(broker_account_id),
                )
                self._save_sweep_state(doc_id, state)
                return state
            
            state = SweepState.from_firestore(doc.to_dict())
            
            # Check if daily reset is needed
            today = self._get_today()
            if state.last_reset_date != today:
                # Reset counters for new day
                state.sweep_count_today = 0
                state.last_sweep_time = None
                state.last_reset_date = today
                self._save_sweep_state(doc_id, state)
            
            return state
        
        except Exception as exc:
            logger.error(
                f"Failed to get sweep state for {tenant_id}/{broker_account_id}: {exc}",
                exc_info=True,
            )
            # Return default state on error
            return SweepState(
                tenant_id=str(tenant_id),
                broker_account_id=str(broker_account_id),
            )
    
    # Record a sweep and update count/timestamps.
    def record_sweep(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
    ) -> bool:
        """Record a sweep execution and update state.
        
        Returns True if successful, False otherwise.
        """
        doc_id = self._get_doc_id(tenant_id, broker_account_id)
        
        try:
            # Get current state
            state = self.get_sweep_state(tenant_id, broker_account_id)
            
            # Increment counter and record time
            state.sweep_count_today += 1
            state.last_sweep_time = datetime.now(self._tz).isoformat()
            
            # Save updated state
            self._save_sweep_state(doc_id, state)
            
            logger.debug(
                f"Recorded sweep for {tenant_id}/{broker_account_id}: "
                f"count={state.sweep_count_today}",
            )
            return True
        
        except Exception as exc:
            logger.error(
                f"Failed to record sweep for {tenant_id}/{broker_account_id}: {exc}",
                exc_info=True,
            )
            return False
    
    # Return the timestamp of the last sweep as a datetime.
    def get_last_sweep_time(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
    ) -> Optional[datetime]:
        """Get the timestamp of the last sweep.
        
        Returns None if no sweep recorded yet.
        """
        state = self.get_sweep_state(tenant_id, broker_account_id)
        
        if state.last_sweep_time is None:
            return None
        
        try:
            return datetime.fromisoformat(state.last_sweep_time)
        except ValueError:
            logger.warning(
                f"Invalid last_sweep_time format for {tenant_id}/{broker_account_id}: "
                f"{state.last_sweep_time}",
            )
            return None
    
    # Return how many sweeps happened today.
    def get_sweep_count_today(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
    ) -> int:
        """Get the number of sweeps already performed today."""
        state = self.get_sweep_state(tenant_id, broker_account_id)
        return state.sweep_count_today
    
    # Return minutes since the last sweep, if any.
    def get_minutes_since_last_sweep(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
    ) -> Optional[float]:
        """Get minutes elapsed since the last sweep.
        
        Returns None if no sweep recorded yet.
        """
        last_sweep = self.get_last_sweep_time(tenant_id, broker_account_id)
        
        if last_sweep is None:
            return None
        
        now = datetime.now(self._tz)
        elapsed = (now - last_sweep).total_seconds() / 60
        return elapsed
    
    # Check daily limits and cooldown to determine if a sweep can run.
    def can_sweep_now(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        max_sweeps_per_day: int = 5,
        cooldown_minutes: float = 30,
    ) -> Tuple[bool, Optional[str]]:
        """Check if an account can perform a sweep now.
        
        Validates against:
        - Daily sweep limit
        - Cooldown period
        
        Returns:
            Tuple of (can_sweep, reason_if_blocked)
        """
        state = self.get_sweep_state(tenant_id, broker_account_id)
        
        # Check daily limit
        if state.sweep_count_today >= max_sweeps_per_day:
            return False, f"Daily limit reached ({max_sweeps_per_day} sweeps)"
        
        # Check cooldown
        if state.last_sweep_time is not None:
            minutes_since = self.get_minutes_since_last_sweep(tenant_id, broker_account_id)
            if minutes_since is not None and minutes_since < cooldown_minutes:
                remaining = cooldown_minutes - minutes_since
                return False, f"Cooldown active ({int(remaining)} min remaining)"
        
        return True, None
    
    # Reset sweep state manually for a tenant/account.
    def reset_sweep_state(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
    ) -> bool:
        """Manually reset sweep state (for testing or manual override).
        
        Returns True if successful.
        """
        doc_id = self._get_doc_id(tenant_id, broker_account_id)
        
        try:
            state = SweepState(
                tenant_id=str(tenant_id),
                broker_account_id=str(broker_account_id),
                last_reset_date=self._get_today(),
            )
            self._save_sweep_state(doc_id, state)
            return True
        
        except Exception as exc:
            logger.error(
                f"Failed to reset sweep state for {tenant_id}/{broker_account_id}: {exc}",
                exc_info=True,
            )
            return False
    
    # Persist sweep state to Firestore.
    def _save_sweep_state(self, doc_id: str, state: SweepState) -> None:
        """Save sweep state to Firestore."""
        self.firestore_client.collection(self.COLLECTION_NAME).document(doc_id).set(
            state.to_firestore()
        )


class PostgresSweepStateStore:
    """Manages persistent sweep state in Postgres."""

    def __init__(self, dsn: str, default_time_zone: str = "Asia/Kolkata"):
        if dict_row is None or psycopg is None:
            raise RuntimeError("psycopg is required for Postgres sweep state store")
        self.dsn = dsn
        self.default_time_zone = default_time_zone
        self._tz = ZoneInfo(default_time_zone)

    def _conn(self):
        return connect_with_retry(self.dsn, autocommit=True)

    def _get_today(self) -> str:
        now = datetime.now(self._tz)
        return now.strftime("%Y-%m-%d")

    def _row_to_state(self, row: Dict[str, object]) -> SweepState:
        last_sweep_time = row.get("last_sweep_time")
        if isinstance(last_sweep_time, datetime):
            last_sweep_time = last_sweep_time.isoformat()
        last_reset_date = row.get("last_reset_date")
        if last_reset_date is not None:
            last_reset_date = str(last_reset_date)
        return SweepState(
            tenant_id=row.get("tenant_id", ""),
            broker_account_id=row.get("broker_account_id", ""),
            sweep_count_today=int(row.get("sweep_count_today", 0) or 0),
            last_sweep_time=last_sweep_time,
            last_reset_date=last_reset_date,
        )

    def _save_state(self, state: SweepState) -> None:
        sql = """
            INSERT INTO sweep_states (
                tenant_id, broker_account_id, sweep_count_today, last_sweep_time, last_reset_date, updated_at
            ) VALUES (
                %(tenant_id)s, %(broker_account_id)s, %(sweep_count_today)s, %(last_sweep_time)s, %(last_reset_date)s, %(updated_at)s
            )
            ON CONFLICT (tenant_id, broker_account_id) DO UPDATE SET
                sweep_count_today = EXCLUDED.sweep_count_today,
                last_sweep_time = EXCLUDED.last_sweep_time,
                last_reset_date = EXCLUDED.last_reset_date,
                updated_at = EXCLUDED.updated_at
        """
        payload = {
            "tenant_id": state.tenant_id,
            "broker_account_id": state.broker_account_id,
            "sweep_count_today": state.sweep_count_today,
            "last_sweep_time": state.last_sweep_time,
            "last_reset_date": state.last_reset_date,
            "updated_at": datetime.now(ZoneInfo("UTC")),
        }
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, payload)

    def get_sweep_state(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
    ) -> SweepState:
        sql = """
            SELECT tenant_id, broker_account_id, sweep_count_today, last_sweep_time, last_reset_date
            FROM sweep_states
            WHERE tenant_id = %(tenant_id)s AND broker_account_id = %(broker_account_id)s
        """
        with self._conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    sql,
                    {"tenant_id": str(tenant_id), "broker_account_id": str(broker_account_id)},
                )
                row = cur.fetchone()
        if not row:
            state = SweepState(tenant_id=str(tenant_id), broker_account_id=str(broker_account_id))
            self._save_state(state)
            return state

        state = self._row_to_state(row)
        today = self._get_today()
        if state.last_reset_date != today:
            state.sweep_count_today = 0
            state.last_sweep_time = None
            state.last_reset_date = today
            self._save_state(state)
        return state

    def record_sweep(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
    ) -> bool:
        try:
            state = self.get_sweep_state(tenant_id, broker_account_id)
            state.sweep_count_today += 1
            state.last_sweep_time = datetime.now(self._tz).isoformat()
            self._save_state(state)
            logger.debug(
                "Recorded sweep for %s/%s: count=%s",
                tenant_id,
                broker_account_id,
                state.sweep_count_today,
            )
            return True
        except Exception as exc:
            logger.error(
                "Failed to record sweep for %s/%s: %s",
                tenant_id,
                broker_account_id,
                exc,
                exc_info=True,
            )
            return False

    def get_last_sweep_time(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
    ) -> Optional[datetime]:
        state = self.get_sweep_state(tenant_id, broker_account_id)
        if state.last_sweep_time is None:
            return None
        try:
            return datetime.fromisoformat(state.last_sweep_time)
        except ValueError:
            logger.warning(
                "Invalid last_sweep_time format for %s/%s: %s",
                tenant_id,
                broker_account_id,
                state.last_sweep_time,
            )
            return None

    def get_sweep_count_today(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
    ) -> int:
        state = self.get_sweep_state(tenant_id, broker_account_id)
        return state.sweep_count_today

    def get_minutes_since_last_sweep(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
    ) -> Optional[float]:
        last_sweep = self.get_last_sweep_time(tenant_id, broker_account_id)
        if last_sweep is None:
            return None
        now = datetime.now(self._tz)
        return (now - last_sweep).total_seconds() / 60

    def can_sweep_now(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        max_sweeps_per_day: int = 5,
        cooldown_minutes: float = 30,
    ) -> Tuple[bool, Optional[str]]:
        state = self.get_sweep_state(tenant_id, broker_account_id)
        if state.sweep_count_today >= max_sweeps_per_day:
            return False, f"Daily limit reached ({max_sweeps_per_day} sweeps)"
        if state.last_sweep_time is not None:
            minutes_since = self.get_minutes_since_last_sweep(tenant_id, broker_account_id)
            if minutes_since is not None and minutes_since < cooldown_minutes:
                remaining = cooldown_minutes - minutes_since
                return False, f"Cooldown active ({int(remaining)} min remaining)"
        return True, None

    def reset_sweep_state(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
    ) -> bool:
        try:
            state = SweepState(
                tenant_id=str(tenant_id),
                broker_account_id=str(broker_account_id),
                last_reset_date=self._get_today(),
            )
            self._save_state(state)
            return True
        except Exception as exc:
            logger.error(
                "Failed to reset sweep state for %s/%s: %s",
                tenant_id,
                broker_account_id,
                exc,
                exc_info=True,
            )
            return False


class SweepStateManager:
    """High-level manager for sweep state operations.
    
    Provides convenient methods for common sweep state queries and updates,
    abstracting away Firestore details from the exit engines.
    """
    
    # Initialize with a SweepStateStore instance.
    def __init__(self, state_store: SweepStateStore):
        """Initialize the state manager.
        
        Args:
            state_store: The underlying SweepStateStore instance
        """
        self.state_store = state_store
    
    # Check whether a sweep can execute right now.
    def try_execute_sweep(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        max_sweeps_per_day: int = 5,
        cooldown_minutes: float = 30,
    ) -> Tuple[bool, Optional[str]]:
        """Check if a sweep can execute and return the result.
        
        This is the primary interface for sweep engines to check permissions.
        
        Returns:
            Tuple of (can_sweep, reason_if_blocked)
        """
        return self.state_store.can_sweep_now(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            max_sweeps_per_day=max_sweeps_per_day,
            cooldown_minutes=cooldown_minutes,
        )
    
    # Record that a sweep completed successfully.
    def record_sweep_execution(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
    ) -> bool:
        """Record that a sweep was executed.
        
        Should be called after all positions have been successfully exited.
        
        Returns True if recorded successfully.
        """
        return self.state_store.record_sweep(tenant_id, broker_account_id)
    
    # Return a user-friendly sweep status dict.
    def get_sweep_status(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
    ) -> Dict:
        """Get current sweep status for display/logging.
        
        Returns a dictionary with sweep statistics.
        """
        minutes_since = self.state_store.get_minutes_since_last_sweep(
            tenant_id, broker_account_id
        )
        state = self.state_store.get_sweep_state(tenant_id, broker_account_id)
        
        return {
            "sweep_count_today": state.sweep_count_today,
            "last_sweep_time": state.last_sweep_time,
            "minutes_since_last_sweep": round(minutes_since, 1) if minutes_since else None,
            "last_reset_date": state.last_reset_date,
        }
