"""Paper-only shadow lifecycle recording for OI/ML order intents."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
import json
import re
from typing import Any, Mapping, Protocol

from app.strategies.oi_ml.order_intents import OiMlOrderIntent


_POSTGRES_NAME_PART = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class OiMlShadowIntentStatus(str, Enum):
    """Allowed shadow lifecycle states."""

    STAGED = "STAGED"
    VIRTUAL_FILLED = "VIRTUAL_FILLED"
    FLAT = "FLAT"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class OiMlShadowLifecycleRecord:
    """Persisted paper-only lifecycle record."""

    record_id: int | None
    intent_id: str
    strategy_id: str
    tenant_id: str | None
    broker_account_id: str | None
    status: OiMlShadowIntentStatus
    structure: str
    underlying: str
    expiry: date
    short_strike: int
    quantity: int
    estimated_net_credit_points: float
    estimated_max_loss_rupees: float
    dry_run_only: bool
    decision_reason: str | None
    guard_reasons: tuple[str, ...]
    intent_payload: Mapping[str, Any]
    created_at: datetime
    recorded_at: datetime | None = None
    virtual_entry_at: datetime | None = None
    virtual_entry_credit_points: float | None = None
    virtual_exit_at: datetime | None = None
    virtual_exit_debit_points: float | None = None
    virtual_flat_at: datetime | None = None
    virtual_exit_reason: str | None = None
    realized_pnl_rupees: float | None = None
    lifecycle_events: tuple[Mapping[str, Any], ...] = ()


class OiMlShadowLifecycleStore(Protocol):
    """Store protocol used by the strategy staging path."""

    def record_intent(
        self,
        intent: OiMlOrderIntent,
        *,
        decision_reason: str | None = None,
        tenant_id: str | None = None,
        broker_account_id: str | None = None,
    ) -> OiMlShadowLifecycleRecord:
        """Record a dry-run intent and return the stored lifecycle row."""

    def mark_virtual_fill(
        self,
        record: OiMlShadowLifecycleRecord,
        *,
        filled_at: datetime,
        entry_credit_points: float,
    ) -> OiMlShadowLifecycleRecord:
        """Mark a staged dry-run intent as virtually filled."""

    def flatten_due_virtual_positions(
        self,
        *,
        now: datetime,
        provider: str | None,
        underlying: str | None = None,
        expiry: date | None = None,
        tenant_id: str | None = None,
        broker_account_id: str | None = None,
        exit_reason: str = "eod_virtual_flatten",
    ) -> int:
        """Mark due virtual positions flat and return updated row count."""

    def count_open_virtual_spreads(
        self,
        *,
        now: datetime,
        underlying: str | None = None,
        expiry: date | None = None,
        tenant_id: str | None = None,
        broker_account_id: str | None = None,
    ) -> int:
        """Return active dry-run spread count for the current IST session."""


class InMemoryOiMlShadowLifecycleStore:
    """Test/dry-run store that preserves the same record shape as Postgres."""

    def __init__(self) -> None:
        self.records: list[OiMlShadowLifecycleRecord] = []

    def record_intent(
        self,
        intent: OiMlOrderIntent,
        *,
        decision_reason: str | None = None,
        tenant_id: str | None = None,
        broker_account_id: str | None = None,
    ) -> OiMlShadowLifecycleRecord:
        record = record_from_intent(
            intent,
            record_id=len(self.records) + 1,
            decision_reason=decision_reason,
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            recorded_at=intent.created_at,
        )
        self.records.append(record)
        return record

    def mark_virtual_fill(
        self,
        record: OiMlShadowLifecycleRecord,
        *,
        filled_at: datetime,
        entry_credit_points: float,
    ) -> OiMlShadowLifecycleRecord:
        updated = replace(
            record,
            status=OiMlShadowIntentStatus.VIRTUAL_FILLED,
            virtual_entry_at=filled_at,
            virtual_entry_credit_points=float(entry_credit_points),
            lifecycle_events=record.lifecycle_events
            + (_event("VIRTUAL_FILLED", filled_at, entry_credit_points=entry_credit_points),),
        )
        self._replace_record(updated)
        return updated

    def flatten_due_virtual_positions(
        self,
        *,
        now: datetime,
        provider: str | None,
        underlying: str | None = None,
        expiry: date | None = None,
        tenant_id: str | None = None,
        broker_account_id: str | None = None,
        exit_reason: str = "eod_virtual_flatten",
    ) -> int:
        updated_count = 0
        for record in list(self.records):
            if record.status is not OiMlShadowIntentStatus.VIRTUAL_FILLED:
                continue
            if underlying and record.underlying != str(underlying).strip().upper():
                continue
            if expiry and record.expiry != expiry:
                continue
            if tenant_id and record.tenant_id != tenant_id:
                continue
            if broker_account_id and record.broker_account_id != broker_account_id:
                continue
            entry_credit = float(
                record.virtual_entry_credit_points
                if record.virtual_entry_credit_points is not None
                else record.estimated_net_credit_points
            )
            exit_debit = _exit_debit_from_payload(record.intent_payload)
            realized = (entry_credit - exit_debit) * int(record.quantity)
            updated = replace(
                record,
                status=OiMlShadowIntentStatus.FLAT,
                virtual_exit_at=now,
                virtual_exit_debit_points=exit_debit,
                virtual_flat_at=now,
                virtual_exit_reason=exit_reason,
                realized_pnl_rupees=realized,
                lifecycle_events=record.lifecycle_events
                + (
                    _event("VIRTUAL_EXITED", now, exit_debit_points=exit_debit),
                    _event("FLAT", now, realized_pnl_rupees=realized, reason=exit_reason),
                ),
            )
            self._replace_record(updated)
            updated_count += 1
        return updated_count

    def count_open_virtual_spreads(
        self,
        *,
        now: datetime,
        underlying: str | None = None,
        expiry: date | None = None,
        tenant_id: str | None = None,
        broker_account_id: str | None = None,
    ) -> int:
        day_start, day_end = _session_bounds(now)
        count = 0
        for record in self.records:
            if record.status not in {
                OiMlShadowIntentStatus.STAGED,
                OiMlShadowIntentStatus.VIRTUAL_FILLED,
            }:
                continue
            created_at = _aware_utc(record.created_at)
            if not (_aware_utc(day_start) <= created_at < _aware_utc(day_end)):
                continue
            if underlying and record.underlying != str(underlying).strip().upper():
                continue
            if expiry and record.expiry != expiry:
                continue
            if tenant_id and record.tenant_id != tenant_id:
                continue
            if broker_account_id and record.broker_account_id != broker_account_id:
                continue
            count += 1
        return count

    def _replace_record(self, record: OiMlShadowLifecycleRecord) -> None:
        for idx, existing in enumerate(self.records):
            if existing.intent_id == record.intent_id:
                self.records[idx] = record
                return
        self.records.append(record)


class PostgresOiMlShadowLifecycleStore:
    """Postgres-backed paper-only shadow lifecycle store."""

    def __init__(
        self,
        conn: Any,
        *,
        table_name: str = "public.oi_ml_shadow_order_intents",
    ) -> None:
        self.conn = conn
        self.table_name = _quoted_table_name(table_name)

    def record_intent(
        self,
        intent: OiMlOrderIntent,
        *,
        decision_reason: str | None = None,
        tenant_id: str | None = None,
        broker_account_id: str | None = None,
    ) -> OiMlShadowLifecycleRecord:
        record = record_from_intent(
            intent,
            record_id=None,
            decision_reason=decision_reason,
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
        )
        params = _record_params(record)
        sql = f"""
            INSERT INTO {self.table_name} (
                intent_id,
                strategy_id,
                tenant_id,
                broker_account_id,
                status,
                structure,
                underlying,
                expiry,
                short_strike,
                quantity,
                estimated_net_credit_points,
                estimated_max_loss_rupees,
                dry_run_only,
                decision_reason,
                guard_reasons,
                intent_payload,
                created_at,
                lifecycle_events
            ) VALUES (
                %(intent_id)s,
                %(strategy_id)s,
                %(tenant_id)s,
                %(broker_account_id)s,
                %(status)s,
                %(structure)s,
                %(underlying)s,
                %(expiry)s,
                %(short_strike)s,
                %(quantity)s,
                %(estimated_net_credit_points)s,
                %(estimated_max_loss_rupees)s,
                TRUE,
                %(decision_reason)s,
                %(guard_reasons)s::jsonb,
                %(intent_payload)s::jsonb,
                %(created_at)s,
                %(lifecycle_events)s::jsonb
            )
            ON CONFLICT (intent_id) DO UPDATE SET
                status = EXCLUDED.status,
                decision_reason = EXCLUDED.decision_reason,
                guard_reasons = EXCLUDED.guard_reasons,
                intent_payload = EXCLUDED.intent_payload,
                lifecycle_events = EXCLUDED.lifecycle_events,
                updated_at = NOW()
            RETURNING
                id,
                recorded_at
        """
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        record_id = None
        recorded_at = None
        if row:
            if isinstance(row, Mapping):
                record_id = row.get("id")
                recorded_at = row.get("recorded_at")
            else:
                record_id = row[0]
                recorded_at = row[1] if len(row) > 1 else None
        return record_from_intent(
            intent,
            record_id=int(record_id) if record_id is not None else None,
            decision_reason=decision_reason,
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            recorded_at=recorded_at,
        )

    def mark_virtual_fill(
        self,
        record: OiMlShadowLifecycleRecord,
        *,
        filled_at: datetime,
        entry_credit_points: float,
    ) -> OiMlShadowLifecycleRecord:
        sql = f"""
            UPDATE {self.table_name}
            SET status = 'VIRTUAL_FILLED',
                virtual_entry_at = %(filled_at)s,
                virtual_entry_credit_points = %(entry_credit_points)s,
                lifecycle_events = COALESCE(lifecycle_events, '[]'::jsonb) || %(event)s::jsonb,
                updated_at = NOW()
            WHERE dry_run_only = TRUE
              AND intent_id = %(intent_id)s
              AND status = 'STAGED'
            RETURNING id, recorded_at
        """
        params = {
            "intent_id": record.intent_id,
            "filled_at": filled_at,
            "entry_credit_points": float(entry_credit_points),
            "event": json.dumps([
                _event(
                    "VIRTUAL_FILLED",
                    filled_at,
                    entry_credit_points=float(entry_credit_points),
                )
            ], sort_keys=True),
        }
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        record_id = record.record_id
        recorded_at = record.recorded_at
        if row:
            mapped = _row_mapping(row, getattr(cur, "description", None))
            record_id = _optional_int(mapped.get("id")) or record_id
            recorded_at = mapped.get("recorded_at") or recorded_at
        return replace(
            record,
            record_id=record_id,
            recorded_at=recorded_at,
            status=OiMlShadowIntentStatus.VIRTUAL_FILLED,
            virtual_entry_at=filled_at,
            virtual_entry_credit_points=float(entry_credit_points),
            lifecycle_events=record.lifecycle_events
            + (
                _event(
                    "VIRTUAL_FILLED",
                    filled_at,
                    entry_credit_points=float(entry_credit_points),
                ),
            ),
        )

    def flatten_due_virtual_positions(
        self,
        *,
        now: datetime,
        provider: str | None,
        underlying: str | None = None,
        expiry: date | None = None,
        tenant_id: str | None = None,
        broker_account_id: str | None = None,
        exit_reason: str = "eod_virtual_flatten",
    ) -> int:
        records = self._fetch_open_virtual_records(
            now=now,
            underlying=underlying,
            expiry=expiry,
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
        )
        updated = 0
        for row in records:
            payload = _payload_mapping(row.get("intent_payload"))
            exit_debit, quote_sources = self._exit_debit_from_quotes(
                payload,
                provider=provider,
                now=now,
            )
            entry_credit = _float(row.get("virtual_entry_credit_points"))
            if entry_credit is None:
                entry_credit = _float(row.get("estimated_net_credit_points")) or 0.0
            quantity = int(row.get("quantity") or 0)
            realized = (entry_credit - exit_debit) * quantity
            event_payload = [
                _event(
                    "VIRTUAL_EXITED",
                    now,
                    exit_debit_points=exit_debit,
                    quote_sources=quote_sources,
                ),
                _event(
                    "FLAT",
                    now,
                    realized_pnl_rupees=realized,
                    reason=exit_reason,
                ),
            ]
            sql = f"""
                UPDATE {self.table_name}
                SET status = 'FLAT',
                    virtual_exit_at = %(exit_at)s,
                    virtual_exit_debit_points = %(exit_debit_points)s,
                    virtual_flat_at = %(flat_at)s,
                    virtual_exit_reason = %(exit_reason)s,
                    realized_pnl_rupees = %(realized_pnl_rupees)s,
                    lifecycle_events = COALESCE(lifecycle_events, '[]'::jsonb) || %(events)s::jsonb,
                    updated_at = NOW()
                WHERE dry_run_only = TRUE
                  AND id = %(id)s
                  AND status = 'VIRTUAL_FILLED'
            """
            params = {
                "id": row.get("id"),
                "exit_at": now,
                "exit_debit_points": exit_debit,
                "flat_at": now,
                "exit_reason": exit_reason,
                "realized_pnl_rupees": realized,
                "events": json.dumps(event_payload, sort_keys=True),
            }
            with self.conn.cursor() as cur:
                cur.execute(sql, params)
                rowcount = int(getattr(cur, "rowcount", 0) or 0)
            updated += rowcount
        return updated

    def count_open_virtual_spreads(
        self,
        *,
        now: datetime,
        underlying: str | None = None,
        expiry: date | None = None,
        tenant_id: str | None = None,
        broker_account_id: str | None = None,
    ) -> int:
        day_start, day_end = _session_bounds(now)
        clauses, params = _scope_clauses(
            underlying=underlying,
            expiry=expiry,
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
        )
        params.update({"day_start": day_start, "day_end": day_end})
        sql = f"""
            SELECT COUNT(*) AS open_count
            FROM {self.table_name}
            WHERE dry_run_only = TRUE
              AND status IN ('STAGED', 'VIRTUAL_FILLED')
              AND created_at >= %(day_start)s
              AND created_at < %(day_end)s
              {clauses}
        """
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            description = getattr(cur, "description", None)
        mapped = _row_mapping(row, description) if row is not None else {}
        return int(mapped.get("open_count") or 0)

    def _fetch_open_virtual_records(
        self,
        *,
        now: datetime,
        underlying: str | None,
        expiry: date | None,
        tenant_id: str | None,
        broker_account_id: str | None,
    ) -> list[dict[str, Any]]:
        day_start, day_end = _session_bounds(now)
        clauses, params = _scope_clauses(
            underlying=underlying,
            expiry=expiry,
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
        )
        params.update({"day_start": day_start, "day_end": day_end})
        sql = f"""
            SELECT id, intent_id, quantity, estimated_net_credit_points,
                   virtual_entry_credit_points, intent_payload, created_at
            FROM {self.table_name}
            WHERE dry_run_only = TRUE
              AND status = 'VIRTUAL_FILLED'
              AND created_at >= %(day_start)s
              AND created_at < %(day_end)s
              {clauses}
            ORDER BY created_at ASC
        """
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            description = getattr(cur, "description", None)
        return [_row_mapping(row, description) for row in rows or []]

    def _exit_debit_from_quotes(
        self,
        payload: Mapping[str, Any],
        *,
        provider: str | None,
        now: datetime,
    ) -> tuple[float, list[Mapping[str, Any]]]:
        debit = 0.0
        quote_sources: list[Mapping[str, Any]] = []
        for leg in _intent_legs(payload):
            quote = self._fetch_latest_leg_quote(leg, provider=provider, now=now)
            price = _exit_price(leg, quote)
            if price is None:
                price = _float(leg.get("price_hint")) or 0.0
                price_source = "intent_price_hint_fallback"
            else:
                price_source = "option_chain_quote"
            if _leg_side(leg) == "SELL":
                debit += price
            else:
                debit -= price
            quote_sources.append(
                {
                    "role": str(leg.get("role") or ""),
                    "side": _leg_side(leg),
                    "price": price,
                    "price_source": price_source,
                    "snapshot_ts": _iso_or_none((quote or {}).get("snapshot_ts")),
                }
            )
        return debit, quote_sources

    def _fetch_latest_leg_quote(
        self,
        leg: Mapping[str, Any],
        *,
        provider: str | None,
        now: datetime,
    ) -> dict[str, Any] | None:
        sql = """
            SELECT snapshot_ts, source_ts, bid, ask, ltp, quality_flags
            FROM public.option_chain_1m
            WHERE underlying = %(underlying)s
              AND expiry = %(expiry)s
              AND strike = %(strike)s
              AND option_type = %(option_type)s
              AND (%(provider)s::text IS NULL OR provider = %(provider)s::text)
              AND snapshot_ts <= %(now)s
            ORDER BY snapshot_ts DESC
            LIMIT 1
        """
        params = {
            "underlying": str(leg.get("underlying") or "").strip().upper()
            or str(leg.get("symbol") or "")[:5].strip().upper(),
            "expiry": _parse_date(leg.get("expiry")),
            "strike": _optional_int(leg.get("strike")),
            "option_type": str(leg.get("option_type") or "").strip().upper(),
            "provider": str(provider or "").strip().lower() or None,
            "now": now,
        }
        if not params["expiry"] or not params["strike"] or not params["option_type"]:
            return None
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            description = getattr(cur, "description", None)
        return _row_mapping(row, description) if row is not None else None


def record_from_intent(
    intent: OiMlOrderIntent,
    *,
    record_id: int | None,
    decision_reason: str | None = None,
    tenant_id: str | None = None,
    broker_account_id: str | None = None,
    recorded_at: datetime | None = None,
) -> OiMlShadowLifecycleRecord:
    """Build a record object from an inert dry-run intent."""

    if not bool(intent.dry_run_only):
        raise ValueError("OI/ML shadow lifecycle accepts dry_run_only intents only")
    payload = intent_payload(intent)
    return OiMlShadowLifecycleRecord(
        record_id=record_id,
        intent_id=intent.intent_id,
        strategy_id=intent.strategy_id,
        tenant_id=tenant_id,
        broker_account_id=broker_account_id,
        status=OiMlShadowIntentStatus.STAGED,
        structure=intent.structure.value,
        underlying=intent.underlying,
        expiry=intent.expiry,
        short_strike=int(intent.short_strike),
        quantity=int(intent.quantity),
        estimated_net_credit_points=float(intent.estimated_net_credit_points),
        estimated_max_loss_rupees=float(intent.estimated_max_loss_rupees),
        dry_run_only=True,
        decision_reason=decision_reason,
        guard_reasons=tuple(str(reason) for reason in intent.guard_reasons),
        intent_payload=payload,
        created_at=intent.created_at,
        recorded_at=recorded_at,
        lifecycle_events=(
            _event(
                "STAGED",
                intent.created_at,
                decision_reason=decision_reason,
            ),
        ),
    )


def intent_payload(intent: OiMlOrderIntent) -> dict[str, Any]:
    """Return a JSON-safe payload for one dry-run intent."""

    payload = _json_safe(intent)
    if not isinstance(payload, dict):
        raise ValueError("intent payload must serialize to an object")
    payload["dry_run_only"] = True
    return payload


def _record_params(record: OiMlShadowLifecycleRecord) -> dict[str, Any]:
    return {
        "intent_id": record.intent_id,
        "strategy_id": record.strategy_id,
        "tenant_id": record.tenant_id,
        "broker_account_id": record.broker_account_id,
        "status": record.status.value,
        "structure": record.structure,
        "underlying": record.underlying,
        "expiry": record.expiry,
        "short_strike": record.short_strike,
        "quantity": record.quantity,
        "estimated_net_credit_points": record.estimated_net_credit_points,
        "estimated_max_loss_rupees": record.estimated_max_loss_rupees,
        "decision_reason": record.decision_reason,
        "guard_reasons": json.dumps(list(record.guard_reasons), separators=(",", ":")),
        "intent_payload": json.dumps(record.intent_payload, sort_keys=True, separators=(",", ":")),
        "created_at": record.created_at,
        "lifecycle_events": json.dumps(
            list(record.lifecycle_events),
            sort_keys=True,
            separators=(",", ":"),
        ),
    }


def _json_safe(value: Any) -> Any:
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_safe(val) for key, val in value.items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _event(status: str, ts: datetime, **metadata: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "status": str(status),
        "ts": _aware_utc(ts).isoformat(),
    }
    clean_metadata = {key: _json_safe(value) for key, value in metadata.items()}
    if clean_metadata:
        event["metadata"] = clean_metadata
    return event


def _session_bounds(value: datetime) -> tuple[datetime, datetime]:
    # The sidecar runs on IST market sessions; use a fixed local offset by
    # avoiding a dependency loop back into shadow_runner. India has no DST.
    ist = timezone(timedelta(hours=5, minutes=30))
    local = value.astimezone(ist) if value.tzinfo else value.replace(tzinfo=ist)
    start = datetime.combine(local.date(), time.min, tzinfo=ist)
    return start, start + timedelta(days=1)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _scope_clauses(
    *,
    underlying: str | None,
    expiry: date | None,
    tenant_id: str | None,
    broker_account_id: str | None,
) -> tuple[str, dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if underlying:
        clauses.append("AND underlying = %(underlying)s")
        params["underlying"] = str(underlying).strip().upper()
    if expiry:
        clauses.append("AND expiry = %(expiry)s")
        params["expiry"] = expiry
    if tenant_id:
        clauses.append("AND tenant_id = %(tenant_id)s")
        params["tenant_id"] = tenant_id
    if broker_account_id:
        clauses.append("AND broker_account_id = %(broker_account_id)s")
        params["broker_account_id"] = broker_account_id
    return "\n              ".join(clauses), params


def _payload_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(payload) if isinstance(payload, Mapping) else {}
    return {}


def _intent_legs(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("legs")
    if not isinstance(rows, list):
        return []
    legs: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        leg = dict(row)
        leg.setdefault("underlying", payload.get("underlying"))
        legs.append(leg)
    return legs


def _exit_debit_from_payload(payload: Mapping[str, Any]) -> float:
    debit = 0.0
    for leg in _intent_legs(payload):
        price = _float(leg.get("price_hint")) or 0.0
        if _leg_side(leg) == "SELL":
            debit += price
        else:
            debit -= price
    return debit


def _exit_price(leg: Mapping[str, Any], quote: Mapping[str, Any] | None) -> float | None:
    if quote is None:
        return None
    if _leg_side(leg) == "SELL":
        return _first_positive(quote.get("ask"), _mid(quote), quote.get("ltp"))
    return _first_positive(quote.get("bid"), _mid(quote), quote.get("ltp"))


def _leg_side(leg: Mapping[str, Any]) -> str:
    return str(leg.get("side") or "").strip().upper()


def _mid(row: Mapping[str, Any]) -> float | None:
    bid = _float(row.get("bid"))
    ask = _float(row.get("ask"))
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return None
    return (bid + ask) / 2.0


def _first_positive(*values: Any) -> float | None:
    for value in values:
        parsed = _float(value)
        if parsed is not None and parsed > 0:
            return parsed
    return None


def _float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _row_mapping(row: Any, description: Any) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    columns = [desc[0] for desc in description or []]
    return dict(zip(columns, row))


def _iso_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _quoted_table_name(table_name: str) -> str:
    parts = [part.strip() for part in str(table_name).split(".") if part.strip()]
    if not parts:
        raise ValueError("table_name is required")
    for part in parts:
        if not _POSTGRES_NAME_PART.match(part):
            raise ValueError(f"Invalid postgres table identifier: {part!r}")
    return ".".join(f'"{part}"' for part in parts)


__all__ = [
    "InMemoryOiMlShadowLifecycleStore",
    "OiMlShadowIntentStatus",
    "OiMlShadowLifecycleRecord",
    "OiMlShadowLifecycleStore",
    "PostgresOiMlShadowLifecycleStore",
    "intent_payload",
    "record_from_intent",
]
