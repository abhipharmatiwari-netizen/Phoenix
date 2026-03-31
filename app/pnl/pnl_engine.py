"""
PnLEngine: in-memory PnL tracking for the multi-tenant hub.

Consumes TradeEvent objects and maintains per-(tenant, account, strategy)
PnLSnapshot objects.

Mark-to-Market Source (Architecture S12):
    Unrealized PnL uses the **Last Traded Price (LTP)** as the mark-to-market
    source.  When LTP is unavailable or stale, the system falls back to the
    last valid mark and logs a staleness warning.  Bid/ask midpoint is NOT
    used because Indian exchange data quality for bid/ask on illiquid options
    is unreliable.

Stale Quote Thresholds (Architecture S12):
    Quote freshness is configurable per instrument class via environment
    variables.  When a quote exceeds its class-specific threshold, PnL reads
    emit warnings and risk checks may block new entries.

    Environment variables:
        QUOTE_FRESHNESS_THRESHOLD_OPTIONS_SECONDS  (default: 120)
        QUOTE_FRESHNESS_THRESHOLD_FUTURES_SECONDS   (default: 60)
        QUOTE_FRESHNESS_THRESHOLD_EQUITY_SECONDS    (default: 300)
        PNL_FRESHNESS_THRESHOLD_SECONDS             (default: 300, global fallback)

Phase 1:
- Only realized PnL is updated from trades.
- Unrealized PnL and exposure can be updated via explicit calls if needed.
"""

from __future__ import annotations

from datetime import datetime, date
from typing import Optional, Any
import logging
import os
from zoneinfo import ZoneInfo

from app.core.identifiers import BrokerAccountId, StrategyId, TenantId
from app.core.clock import IClock, SystemClock
from app.config.settings import get_settings
from app.pnl.types import PnLSnapshot, PnLSnapshotKey, TradeEvent
from app.data.bq_persister import insert_daily_pnl_snapshot
from app.pnl.state_store import (
    InMemoryPnLStateStore,
    PnLStateStore,
)

logger = logging.getLogger(__name__)

ACCOUNT_BOOTSTRAP_STRATEGY_ID = StrategyId("__account_seed__")


class PnLEngine:
    """In-memory PnL tracker using LTP mark-to-market.

    Mark-to-Market Source:
        All unrealized PnL calculations use Last Traded Price (LTP) as the
        mark source.  The ``update_unrealized_and_exposure`` method accepts
        externally computed unrealized PnL which MUST be derived from LTP.

    Stale Mark Handling:
        When a snapshot's ``freshness_updated_at`` exceeds the configured
        threshold, mark-dependent reads such as ``get_current_total_pnl()`` and
        freshness-checked accessors emit structured warnings. Trade-derived
        realized PnL reads remain available without mark-freshness warnings.
        RiskEngine consumers should treat stale marks as fail-closed (block
        new entries).

    Per-Instrument-Class Thresholds:
        Use ``get_freshness_threshold_for_class()`` to obtain the correct
        threshold for options, futures, or equity instruments.
    """

    # Initialize in-memory snapshot store.
    def __init__(
        self,
        state_store: Optional[PnLStateStore] = None,
        clock: Optional[IClock] = None,
    ) -> None:
        self._state_store: PnLStateStore = state_store or InMemoryPnLStateStore()
        self._clock: IClock = clock or SystemClock()
        tz_name = "UTC"
        try:
            tz_name = str(get_settings().default_time_zone or "UTC")
        except Exception:
            tz_name = "UTC"
        try:
            self._tz = ZoneInfo(tz_name)
        except Exception:
            self._tz = ZoneInfo("UTC")

    def _today(self, now: Optional[datetime] = None) -> date:
        now_dt = now or self._clock.now_utc()
        try:
            return now_dt.astimezone(self._tz).date()
        except Exception:
            return now_dt.date()

    def _reset_if_new_day(self, snap: PnLSnapshot, now: datetime) -> None:
        today = self._today(now)
        if snap.session_date is None:
            snap.session_date = today
            snap.freshness_updated_at = now
            snap.freshness_source = "day_init"
            return
        if snap.session_date != today:
            snap.realized_pnl = 0.0
            snap.unrealized_pnl = 0.0
            snap.gross_exposure = 0.0
            snap.session_date = today
            snap.freshness_updated_at = now
            snap.freshness_source = "day_reset"

    # Get or create a snapshot for a tenant/account/strategy.
    def _get_or_create_snapshot(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
    ) -> PnLSnapshot:
        snap = self._state_store.get_snapshot(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
        )
        if snap is None:
            snap_key = PnLSnapshotKey(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                strategy_id=strategy_id,
            )
            snap = PnLSnapshot(key=snap_key)
        return snap

    # Ensure an account has at least one PnL snapshot row.
    def ensure_account_snapshot(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: Optional[StrategyId] = None,
    ) -> bool:
        seed_strategy_id = strategy_id or ACCOUNT_BOOTSTRAP_STRATEGY_ID
        now = self._clock.now_utc()
        existing = self._state_store.get_snapshot(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=seed_strategy_id,
        )
        if existing is not None:
            self._reset_if_new_day(existing, now)
            # Backfill freshness for snapshots created before freshness was tracked
            if existing.freshness_updated_at is None:
                existing.freshness_updated_at = now
                existing.freshness_source = "bootstrap_backfill"
            self._state_store.upsert_snapshot(existing)
            return False

        snapshot = PnLSnapshot(
            key=PnLSnapshotKey(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                strategy_id=seed_strategy_id,
            ),
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            gross_exposure=0.0,
            as_of=now,
            session_date=self._today(now),
            freshness_updated_at=now,
            freshness_source="bootstrap",
        )
        self._state_store.upsert_snapshot(snapshot)
        return True

    # Persist a snapshot to BigQuery for reporting.
    def _persist_snapshot(self, snap: PnLSnapshot) -> None:
        """
        Persist a PnL snapshot to BigQuery.
        This is called whenever a snapshot is modified to ensure real-time data.
        """
        try:
            now = self._clock.now_utc()
            record: dict[str, Any] = {
                "date": (snap.as_of or now).date(),
                "timestamp": snap.as_of or now,
                "tenant_id": str(snap.key.tenant_id),
                "broker_account_id": str(snap.key.broker_account_id),
                "strategy_id": str(snap.key.strategy_id),
                "realized_pnl": snap.realized_pnl,
                "unrealized_pnl": snap.unrealized_pnl,
                "total_pnl": snap.realized_pnl + snap.unrealized_pnl,
                "gross_exposure": snap.gross_exposure,
            }
            insert_daily_pnl_snapshot(record)
        except Exception as exc:
            logger.error(
                "Failed to persist PnL snapshot for %s/%s/%s: %s",
                snap.key.tenant_id,
                snap.key.broker_account_id,
                snap.key.strategy_id,
                exc,
            )

    # Consume a trade event and update realized PnL.
    def on_trade(self, event: TradeEvent) -> None:
        """
        Consume a trade event and update realized PnL.

        Phase 1:
        - Assumes each trade's PnL effect is encoded in event.qty * event.price (gross),
          minus fees. Buys have positive qty and reduce PnL (cash outflow), sells
          have negative qty and increase PnL (cash inflow). In practice, integrating
          with real position accounting will refine this.
        """
        snap = self._get_or_create_snapshot(
            event.tenant_id,
            event.broker_account_id,
            event.strategy_id,
        )
        self._reset_if_new_day(snap, event.trade_time)
        snap.realized_pnl -= event.qty * event.price
        snap.realized_pnl -= event.fees
        snap.as_of = event.trade_time
        snap.freshness_updated_at = event.trade_time
        snap.freshness_source = "trade"
        self._state_store.upsert_snapshot(snap)
        
        # Persist to BigQuery in real-time
        self._persist_snapshot(snap)

    # Update unrealized PnL and exposure for a snapshot.
    def update_unrealized_and_exposure(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        unrealized_pnl: float,
        gross_exposure: float,
        as_of: Optional[datetime] = None,
    ) -> None:
        snap = self._get_or_create_snapshot(
            tenant_id,
            broker_account_id,
            strategy_id,
        )
        self._reset_if_new_day(snap, as_of or self._clock.now_utc())
        now = as_of or self._clock.now_utc()
        snap.unrealized_pnl = unrealized_pnl
        snap.gross_exposure = gross_exposure
        snap.as_of = now
        snap.freshness_updated_at = now
        snap.freshness_source = "mark_update"
        self._state_store.upsert_snapshot(snap)
        
        # Persist to BigQuery in real-time
        self._persist_snapshot(snap)

    def sync_account_mark_to_market(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        account_unrealized_pnl: float,
        account_gross_exposure: float,
        per_strategy_marks: Optional[dict[StrategyId, tuple[float, float]]] = None,
        as_of: Optional[datetime] = None,
        source: str = "broker_sync",
    ) -> None:
        """
        Refresh mark-to-market snapshots for an account from a broker positions sync.

        The account bootstrap snapshot always carries the aggregate broker view.
        Any existing strategy snapshots for the account are refreshed as well;
        strategies without current owned positions are zeroed so total-PnL reads
        do not keep warning off stale day-reset timestamps.
        """
        now = as_of or self._clock.now_utc()
        strategy_marks = {
            StrategyId(str(strategy_id)): (
                float(values[0] or 0.0),
                float(values[1] or 0.0),
            )
            for strategy_id, values in (per_strategy_marks or {}).items()
            if str(strategy_id or "").strip()
        }

        existing = {
            snap.key.strategy_id: snap
            for snap in self._state_store.list_account_snapshots(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
            )
        }

        target_strategy_ids: set[StrategyId] = set(existing.keys())
        target_strategy_ids.update(strategy_marks.keys())
        target_strategy_ids.add(ACCOUNT_BOOTSTRAP_STRATEGY_ID)

        for strategy_id in target_strategy_ids:
            snap = existing.get(strategy_id)
            if snap is None:
                snap = self._get_or_create_snapshot(
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    strategy_id=strategy_id,
                )
            self._reset_if_new_day(snap, now)

            if strategy_id == ACCOUNT_BOOTSTRAP_STRATEGY_ID:
                unrealized_pnl = float(account_unrealized_pnl or 0.0)
                gross_exposure = float(account_gross_exposure or 0.0)
            else:
                unrealized_pnl, gross_exposure = strategy_marks.get(
                    strategy_id,
                    (0.0, 0.0),
                )

            snap.unrealized_pnl = unrealized_pnl
            snap.gross_exposure = gross_exposure
            snap.as_of = now
            snap.freshness_updated_at = now
            snap.freshness_source = str(source or "broker_sync")
            self._state_store.upsert_snapshot(snap)

            # Persist synced mark snapshots using the same contract as other PnL mutations.
            self._persist_snapshot(snap)

    # Return a snapshot for a specific tenant/account/strategy.
    def get_snapshot(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
    ) -> Optional[PnLSnapshot]:
        snap = self._state_store.get_snapshot(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
        )
        if snap is not None:
            self._reset_if_new_day(snap, self._clock.now_utc())
            self._state_store.upsert_snapshot(snap)
        return snap

    def _list_scoped_snapshots(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: Optional[StrategyId] = None,
    ) -> list[PnLSnapshot]:
        if strategy_id is not None:
            snap = self.get_snapshot(tenant_id, broker_account_id, strategy_id)
            return [snap] if snap is not None else []
        snaps = self._state_store.list_account_snapshots(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
        )
        now = self._clock.now_utc()
        for snap in snaps:
            self._reset_if_new_day(snap, now)
            self._state_store.upsert_snapshot(snap)
        return snaps

    def _warn_if_mark_snapshot_stale(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        snap: PnLSnapshot,
        now: datetime,
        read_kind: str,
    ) -> None:
        threshold = self._default_freshness_threshold_seconds()
        if snap.freshness_updated_at is not None:
            age = (now - snap.freshness_updated_at).total_seconds()
            if age > threshold:
                logger.warning(
                    "PnL snapshot stale during %s read (%.1fs > %.1fs) "
                    "for %s/%s/%s source=%s",
                    read_kind,
                    age,
                    threshold,
                    tenant_id,
                    broker_account_id,
                    snap.key.strategy_id,
                    snap.freshness_source or "unknown",
                )
            return
        if snap.as_of is not None:
            logger.warning(
                "PnL snapshot has no freshness timestamp during %s read "
                "for %s/%s/%s",
                read_kind,
                tenant_id,
                broker_account_id,
                snap.key.strategy_id,
            )

    # Return current realized PnL for a strategy or account.
    def get_current_realized_pnl(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: Optional[StrategyId] = None,
    ) -> Optional[float]:
        snaps = self._list_scoped_snapshots(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
        )
        if not snaps:
            return None
        return float(sum(s.realized_pnl or 0.0 for s in snaps))

    # Return current total PnL (realized + unrealized).
    def get_current_total_pnl(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: Optional[StrategyId] = None,
    ) -> Optional[float]:
        now = self._clock.now_utc()
        snaps = self._list_scoped_snapshots(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
        )
        if not snaps:
            return None
        if strategy_id is None:
            account_seed = next(
                (
                    snap
                    for snap in snaps
                    if snap.key.strategy_id == ACCOUNT_BOOTSTRAP_STRATEGY_ID
                ),
                None,
            )
            if account_seed is not None:
                self._warn_if_mark_snapshot_stale(
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    snap=account_seed,
                    now=now,
                    read_kind="total PnL",
                )
                realized_total = float(
                    sum(
                        s.realized_pnl or 0.0
                        for s in snaps
                        if s.key.strategy_id != ACCOUNT_BOOTSTRAP_STRATEGY_ID
                    )
                )
                if len(snaps) == 1:
                    realized_total += float(account_seed.realized_pnl or 0.0)
                return float(realized_total + float(account_seed.unrealized_pnl or 0.0))
        for snap in snaps:
            self._warn_if_mark_snapshot_stale(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
                snap=snap,
                now=now,
                read_kind="total PnL",
            )

        return float(
            sum((s.realized_pnl or 0.0) + (s.unrealized_pnl or 0.0) for s in snaps)
        )

    def _default_freshness_threshold_seconds(self) -> float:
        """Return freshness threshold from env or default 300s (5 min)."""
        return float(os.environ.get("PNL_FRESHNESS_THRESHOLD_SECONDS", "300"))

    def get_snapshot_age_seconds(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
    ) -> Optional[float]:
        """
        Return the age in seconds of the latest snapshot's freshness timestamp.
        Returns None if the snapshot does not exist or has no freshness timestamp.
        """
        snap = self.get_snapshot(tenant_id, broker_account_id, strategy_id)
        if snap is None or snap.freshness_updated_at is None:
            return None
        return (self._clock.now_utc() - snap.freshness_updated_at).total_seconds()

    def is_snapshot_fresh(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        *,
        threshold_seconds: Optional[float] = None,
    ) -> bool:
        """
        Return True if the snapshot exists and its freshness_updated_at is
        within *threshold_seconds* of now.  Returns False when the snapshot is
        missing or has no freshness timestamp (fail-closed).
        """
        snap = self.get_snapshot(tenant_id, broker_account_id, strategy_id)
        if snap is None or snap.freshness_updated_at is None:
            return False
        threshold = threshold_seconds if threshold_seconds is not None else self._default_freshness_threshold_seconds()
        age = (self._clock.now_utc() - snap.freshness_updated_at).total_seconds()
        return age <= threshold

    def get_snapshot_with_freshness_check(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        *,
        threshold_seconds: Optional[float] = None,
        fail_closed: bool = True,
    ) -> Optional[PnLSnapshot]:
        """
        Return the snapshot only if it is fresh.

        * If the snapshot is stale (or missing) and *fail_closed* is True,
          return None so callers block new entries.
        * If *fail_closed* is False, return the stale snapshot but log a
          warning.
        """
        snap = self.get_snapshot(tenant_id, broker_account_id, strategy_id)
        if snap is None:
            return None

        threshold = threshold_seconds if threshold_seconds is not None else self._default_freshness_threshold_seconds()

        if snap.freshness_updated_at is None:
            if fail_closed:
                logger.warning(
                    "PnL snapshot has no freshness timestamp – fail-closed for %s/%s/%s",
                    tenant_id, broker_account_id, strategy_id,
                )
                return None
            logger.warning(
                "PnL snapshot has no freshness timestamp – returning stale for %s/%s/%s",
                tenant_id, broker_account_id, strategy_id,
            )
            return snap

        age = (self._clock.now_utc() - snap.freshness_updated_at).total_seconds()
        if age > threshold:
            if fail_closed:
                logger.warning(
                    "PnL snapshot stale (%.1fs > %.1fs) – fail-closed for %s/%s/%s",
                    age, threshold, tenant_id, broker_account_id, strategy_id,
                )
                return None
            logger.warning(
                "PnL snapshot stale (%.1fs > %.1fs) – returning anyway for %s/%s/%s",
                age, threshold, tenant_id, broker_account_id, strategy_id,
            )
        return snap

    def get_account_total_pnl_checked(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        *,
        threshold_seconds: Optional[float] = None,
        fail_closed: bool = True,
    ) -> Optional[float]:
        """
        Sum realized + unrealized PnL across all strategies for an account.

        Returns None if *any* snapshot is stale and *fail_closed* is True.
        """
        snaps = self._state_store.list_account_snapshots(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
        )
        if not snaps:
            return None

        threshold = threshold_seconds if threshold_seconds is not None else self._default_freshness_threshold_seconds()
        now = self._clock.now_utc()

        for snap in snaps:
            self._reset_if_new_day(snap, now)
            self._state_store.upsert_snapshot(snap)

            if snap.freshness_updated_at is None:
                if fail_closed:
                    logger.warning(
                        "Account PnL check: snapshot has no freshness timestamp – "
                        "fail-closed for %s/%s/%s",
                        tenant_id, broker_account_id, snap.key.strategy_id,
                    )
                    return None
                continue

            age = (now - snap.freshness_updated_at).total_seconds()
            if age > threshold:
                if fail_closed:
                    logger.warning(
                        "Account PnL check: snapshot stale (%.1fs > %.1fs) – "
                        "fail-closed for %s/%s/%s",
                        age, threshold, tenant_id, broker_account_id,
                        snap.key.strategy_id,
                    )
                    return None

        return float(
            sum((s.realized_pnl or 0.0) + (s.unrealized_pnl or 0.0) for s in snaps)
        )


    # ── Per-instrument-class stale thresholds (H4 / Architecture §12) ──

    @staticmethod
    def get_freshness_threshold_for_class(instrument_class: str) -> float:
        """Return the freshness threshold in seconds for an instrument class.

        Configurable via environment variables:
            QUOTE_FRESHNESS_THRESHOLD_OPTIONS_SECONDS  (default: 120)
            QUOTE_FRESHNESS_THRESHOLD_FUTURES_SECONDS   (default: 60)
            QUOTE_FRESHNESS_THRESHOLD_EQUITY_SECONDS    (default: 300)

        Falls back to PNL_FRESHNESS_THRESHOLD_SECONDS (default: 300) for
        unknown instrument classes.
        """
        cls = str(instrument_class or "").strip().upper()
        if cls in ("OPTIONS", "OPTION", "OPT", "CE", "PE"):
            return float(os.environ.get(
                "QUOTE_FRESHNESS_THRESHOLD_OPTIONS_SECONDS", "120"
            ))
        if cls in ("FUTURES", "FUTURE", "FUT"):
            return float(os.environ.get(
                "QUOTE_FRESHNESS_THRESHOLD_FUTURES_SECONDS", "60"
            ))
        if cls in ("EQUITY", "EQ", "CASH"):
            return float(os.environ.get(
                "QUOTE_FRESHNESS_THRESHOLD_EQUITY_SECONDS", "300"
            ))
        return float(os.environ.get("PNL_FRESHNESS_THRESHOLD_SECONDS", "300"))


__all__ = ["PnLEngine"]
