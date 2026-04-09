"""Trade decision lineage — PHX-AUD-002.

Every order emitted by the system links back to the full decision context:
  - strategy_id / strategy_version
  - signal values that triggered the decision
  - regime at decision time
  - risk policy results (checks passed/failed)
  - execution outcome

A lineage record is created when an order is created (CREATED state)
and updated as the order reaches terminal state.

Records are written to the audit_log and optionally Postgres
(table: trade_decision_lineage).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from app.core.audit_log import emit_audit_event

logger = logging.getLogger(__name__)

# In-memory cache of recent lineage records (for fast API queries)
_LINEAGE_CACHE: dict[str, "DecisionLineage"] = {}
_LINEAGE_LOCK = threading.Lock()
_MAX_CACHE = 2000


@dataclass
class RiskCheckResult:
    check_name: str
    passed: bool
    value: Optional[Any] = None
    limit: Optional[Any] = None
    message: str = ""


@dataclass
class DecisionLineage:
    """Full decision lineage record for a single order intent."""

    lineage_id: str
    order_intent_id: str
    tenant_id: str
    account_id: str
    symbol: str

    # Strategy context
    strategy_id: str = ""
    strategy_version: str = ""

    # Signal context
    signal_values: dict[str, Any] = field(default_factory=dict)
    signal_timestamp: Optional[str] = None

    # Regime at decision time
    regime: str = ""
    regime_confidence: float = 0.0

    # Risk gate results
    risk_checks: list[RiskCheckResult] = field(default_factory=list)
    risk_approved: bool = False
    risk_denial_reason: str = ""

    # Execution context
    execution_style: str = "MARKET"
    intended_price: Optional[float] = None
    intended_qty: int = 0
    side: str = ""

    # Outcome (filled in when order reaches terminal state)
    fill_price: Optional[float] = None
    fill_qty: int = 0
    slippage_bps: Optional[float] = None
    final_state: str = ""
    broker_order_id: str = ""

    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    resolved_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["risk_checks"] = [asdict(r) for r in self.risk_checks]
        return d


def create_lineage(
    *,
    order_intent_id: str,
    tenant_id: str,
    account_id: str,
    symbol: str,
    strategy_id: str = "",
    strategy_version: str = "",
    signal_values: Optional[dict[str, Any]] = None,
    signal_timestamp: Optional[str] = None,
    regime: str = "",
    regime_confidence: float = 0.0,
    risk_checks: Optional[list[RiskCheckResult]] = None,
    risk_approved: bool = False,
    risk_denial_reason: str = "",
    execution_style: str = "MARKET",
    intended_price: Optional[float] = None,
    intended_qty: int = 0,
    side: str = "",
) -> DecisionLineage:
    """Create and persist a decision lineage record for an order."""
    lineage_id = uuid4().hex
    rec = DecisionLineage(
        lineage_id=lineage_id,
        order_intent_id=order_intent_id,
        tenant_id=tenant_id,
        account_id=account_id,
        symbol=symbol,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        signal_values=dict(signal_values or {}),
        signal_timestamp=signal_timestamp,
        regime=regime,
        regime_confidence=regime_confidence,
        risk_checks=list(risk_checks or []),
        risk_approved=risk_approved,
        risk_denial_reason=risk_denial_reason,
        execution_style=execution_style,
        intended_price=intended_price,
        intended_qty=intended_qty,
        side=side,
    )
    _store(rec)
    emit_audit_event(
        actor=f"strategy:{strategy_id}",
        action="order_decision_created",
        resource_type="order_intent",
        resource_id=order_intent_id,
        after=rec.to_dict(),
        metadata={
            "lineage_id": lineage_id,
            "strategy_id": strategy_id,
            "regime": regime,
            "risk_approved": risk_approved,
        },
    )
    return rec


def resolve_lineage(
    *,
    order_intent_id: str,
    final_state: str,
    fill_price: Optional[float] = None,
    fill_qty: int = 0,
    slippage_bps: Optional[float] = None,
    broker_order_id: str = "",
) -> Optional[DecisionLineage]:
    """Update lineage with execution outcome when order reaches terminal state."""
    with _LINEAGE_LOCK:
        rec = _LINEAGE_CACHE.get(order_intent_id)
    if rec is None:
        rec = _load_from_postgres(order_intent_id)
    if rec is None:
        logger.warning("decision_lineage_not_found order_intent_id=%s", order_intent_id)
        return None

    rec.final_state = final_state
    rec.fill_price = fill_price
    rec.fill_qty = fill_qty
    rec.slippage_bps = slippage_bps
    rec.broker_order_id = broker_order_id
    rec.resolved_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    _store(rec, update=True)
    emit_audit_event(
        actor=f"strategy:{rec.strategy_id}",
        action="order_decision_resolved",
        resource_type="order_intent",
        resource_id=order_intent_id,
        after={
            "final_state": final_state,
            "fill_price": fill_price,
            "fill_qty": fill_qty,
            "slippage_bps": slippage_bps,
            "broker_order_id": broker_order_id,
        },
        metadata={"lineage_id": rec.lineage_id},
    )
    return rec


def get_lineage(order_intent_id: str) -> Optional[DecisionLineage]:
    with _LINEAGE_LOCK:
        rec = _LINEAGE_CACHE.get(order_intent_id)
    if rec is not None:
        return rec
    return _load_from_postgres(order_intent_id)


def _store(rec: DecisionLineage, update: bool = False) -> None:
    """Write to in-memory cache and Postgres."""
    with _LINEAGE_LOCK:
        if len(_LINEAGE_CACHE) >= _MAX_CACHE and rec.order_intent_id not in _LINEAGE_CACHE:
            # Evict oldest entry
            oldest = next(iter(_LINEAGE_CACHE))
            del _LINEAGE_CACHE[oldest]
        _LINEAGE_CACHE[rec.order_intent_id] = rec
    _try_postgres_upsert(rec, update=update)


def _try_postgres_upsert(rec: DecisionLineage, update: bool = False) -> None:
    import json
    try:
        from app.data.postgres import connect_with_retry, get_control_plane_dsn
        dsn = get_control_plane_dsn()
        with connect_with_retry(dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                if update:
                    cur.execute(
                        """
                        UPDATE trade_decision_lineage
                        SET final_state = %s,
                            fill_price = %s,
                            fill_qty = %s,
                            slippage_bps = %s,
                            broker_order_id = %s,
                            resolved_at = %s
                        WHERE order_intent_id = %s
                        """,
                        (
                            rec.final_state,
                            rec.fill_price,
                            rec.fill_qty,
                            rec.slippage_bps,
                            rec.broker_order_id,
                            rec.resolved_at,
                            rec.order_intent_id,
                        ),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO trade_decision_lineage
                            (lineage_id, order_intent_id, tenant_id, account_id, symbol,
                             strategy_id, strategy_version, signal_values, signal_timestamp,
                             regime, regime_confidence, risk_checks, risk_approved,
                             risk_denial_reason, execution_style, intended_price,
                             intended_qty, side, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (order_intent_id) DO NOTHING
                        """,
                        (
                            rec.lineage_id,
                            rec.order_intent_id,
                            rec.tenant_id,
                            rec.account_id,
                            rec.symbol,
                            rec.strategy_id,
                            rec.strategy_version,
                            json.dumps(rec.signal_values),
                            rec.signal_timestamp,
                            rec.regime,
                            rec.regime_confidence,
                            json.dumps([asdict(r) for r in rec.risk_checks]),
                            rec.risk_approved,
                            rec.risk_denial_reason,
                            rec.execution_style,
                            rec.intended_price,
                            rec.intended_qty,
                            rec.side,
                            rec.created_at,
                        ),
                    )
    except Exception:
        logger.debug("Postgres decision lineage upsert unavailable", exc_info=True)


def _load_from_postgres(order_intent_id: str) -> Optional[DecisionLineage]:
    import json as _json
    try:
        from psycopg.rows import dict_row  # type: ignore
        from app.data.postgres import connect_with_retry, get_control_plane_dsn
        dsn = get_control_plane_dsn()
        with connect_with_retry(dsn, autocommit=True) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT * FROM trade_decision_lineage WHERE order_intent_id = %s",
                    (order_intent_id,),
                )
                row = cur.fetchone()
        if row is None:
            return None
        row = dict(row)
        checks_raw = row.get("risk_checks") or "[]"
        if isinstance(checks_raw, str):
            checks_raw = _json.loads(checks_raw)
        rec = DecisionLineage(
            lineage_id=row["lineage_id"],
            order_intent_id=row["order_intent_id"],
            tenant_id=row["tenant_id"],
            account_id=row["account_id"],
            symbol=row["symbol"],
            strategy_id=row.get("strategy_id") or "",
            strategy_version=row.get("strategy_version") or "",
            signal_values=_json.loads(row.get("signal_values") or "{}"),
            signal_timestamp=row.get("signal_timestamp"),
            regime=row.get("regime") or "",
            regime_confidence=float(row.get("regime_confidence") or 0.0),
            risk_checks=[RiskCheckResult(**c) for c in checks_raw],
            risk_approved=bool(row.get("risk_approved")),
            risk_denial_reason=row.get("risk_denial_reason") or "",
            execution_style=row.get("execution_style") or "MARKET",
            intended_price=row.get("intended_price"),
            intended_qty=int(row.get("intended_qty") or 0),
            side=row.get("side") or "",
            fill_price=row.get("fill_price"),
            fill_qty=int(row.get("fill_qty") or 0),
            slippage_bps=row.get("slippage_bps"),
            final_state=row.get("final_state") or "",
            broker_order_id=row.get("broker_order_id") or "",
            created_at=str(row.get("created_at") or ""),
            resolved_at=row.get("resolved_at"),
        )
        with _LINEAGE_LOCK:
            _LINEAGE_CACHE[order_intent_id] = rec
        return rec
    except Exception:
        logger.debug("Postgres decision lineage load unavailable", exc_info=True)
        return None


__all__ = [
    "DecisionLineage",
    "RiskCheckResult",
    "create_lineage",
    "get_lineage",
    "resolve_lineage",
]
