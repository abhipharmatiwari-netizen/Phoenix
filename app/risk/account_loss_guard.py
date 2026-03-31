from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import threading
from typing import Dict, Optional, Tuple

from app.core.identifiers import BrokerAccountId, TenantId


@dataclass(frozen=True)
class AccountLossSnapshot:
    tenant_id: str
    broker_account_id: str
    realized_pnl: float
    daily_realized_pnl: float
    unrealized_pnl: float
    daily_total_pnl: float
    max_daily_loss: float
    max_intraday_drawdown: float
    kill_switch_activated: bool
    source: str
    updated_at: datetime


class AccountLossGuard:
    def __init__(self) -> None:
        self._snapshots: Dict[Tuple[str, str], AccountLossSnapshot] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _key(
        tenant_id: Optional[TenantId],
        broker_account_id: Optional[BrokerAccountId],
    ) -> Optional[Tuple[str, str]]:
        tenant = str(tenant_id or "").strip()
        broker = str(broker_account_id or "").strip()
        if not tenant or not broker:
            return None
        return tenant, broker

    def update_snapshot(
        self,
        *,
        tenant_id: Optional[TenantId],
        broker_account_id: Optional[BrokerAccountId],
        realized_pnl: float,
        daily_realized_pnl: float,
        unrealized_pnl: float = 0.0,
        daily_total_pnl: float | None = None,
        max_daily_loss: float,
        max_intraday_drawdown: float = 0.0,
        kill_switch_activated: bool,
        source: str,
    ) -> Optional[AccountLossSnapshot]:
        key = self._key(tenant_id, broker_account_id)
        if key is None:
            return None
        total_pnl = (
            float(daily_total_pnl)
            if daily_total_pnl is not None
            else float(daily_realized_pnl) + float(unrealized_pnl)
        )
        snapshot = AccountLossSnapshot(
            tenant_id=key[0],
            broker_account_id=key[1],
            realized_pnl=float(realized_pnl),
            daily_realized_pnl=float(daily_realized_pnl),
            unrealized_pnl=float(unrealized_pnl),
            daily_total_pnl=total_pnl,
            max_daily_loss=float(max_daily_loss or 0.0),
            max_intraday_drawdown=float(max_intraday_drawdown or 0.0),
            kill_switch_activated=bool(kill_switch_activated),
            source=str(source or "").strip() or "unspecified",
            updated_at=datetime.now(timezone.utc),
        )
        with self._lock:
            self._snapshots[key] = snapshot
        return snapshot

    def get_snapshot(
        self,
        *,
        tenant_id: Optional[TenantId],
        broker_account_id: Optional[BrokerAccountId],
    ) -> Optional[AccountLossSnapshot]:
        key = self._key(tenant_id, broker_account_id)
        if key is None:
            return None
        with self._lock:
            return self._snapshots.get(key)

    def clear_snapshot(
        self,
        *,
        tenant_id: Optional[TenantId],
        broker_account_id: Optional[BrokerAccountId],
    ) -> None:
        key = self._key(tenant_id, broker_account_id)
        if key is None:
            return
        with self._lock:
            self._snapshots.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._snapshots.clear()


account_loss_guard = AccountLossGuard()


__all__ = [
    "AccountLossGuard",
    "AccountLossSnapshot",
    "account_loss_guard",
]
