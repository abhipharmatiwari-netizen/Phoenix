"""
Position ownership and contract-lock helpers for order routing.

This module provides:
- Canonical contract-key derivation (meta-first, symbol-parse fallback).
- Ownership store with submit-time pending locks and fill updates.
- Optional durable Postgres persistence for ownership net state.
- Reconciliation against broker-net position snapshots.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import os
import re
import threading
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional, Protocol

from app.brokers.base import OrderRequest
from app.config.boot_config import RuntimeConfig
from app.config.settings import get_settings
from app.core.operating_mode import (
    OperatingMode,
    resolve_operating_mode_from_runtime,
)
from app.core.dashboard_bus import dashboard_bus
from app.core.identifiers import BrokerAccountId, StrategyId, TenantId
from app.core.underlyings import canonical_underlying_key
from app.data.postgres import connect_with_retry, get_control_plane_dsn
from app.core.audit_log import emit_audit_event
from app.orders.ownership_state import (
    InvalidOwnershipTransition,
    OrphanReviewDecision,
    OwnershipRecord,
    OwnershipState,
)
from app.orders.scope_serializer import (
    MutationPriority,
    ScopedMutation,
    scope_serializer,
)

logger = logging.getLogger(__name__)

UNKNOWN_OWNER = "UNKNOWN"
UNKNOWN_STRATEGY_ID = "__UNKNOWN__"
_PENDING_STRATEGY_ID_PREFIX = "__pending__:"
UNKNOWN_MODE_BLOCK_ENTRIES = "block_entries"
UNKNOWN_MODE_ALLOW_ENTRIES = "allow_entries"
_UNKNOWN_MODES = {UNKNOWN_MODE_BLOCK_ENTRIES, UNKNOWN_MODE_ALLOW_ENTRIES}

_OPTION_SYMBOL_PATTERN = re.compile(
    r"^(?P<underlying>[A-Z]+)(?P<day>\d{1,2})(?P<mon>[A-Z]{3})(?P<year>\d{2})(?P<strike>\d+(?:\.\d+)?)(?P<right>CE|PE)$"
)
_NUMERIC_OPTION_SYMBOL_PATTERN = re.compile(
    r"^(?P<underlying>[A-Z]+)(?P<body>\d+)(?P<right>CE|PE)$"
)
_OPTION_STRIKE_SUFFIX_PATTERN = re.compile(r"(?P<strike>\d+(?:\.\d+)?)(?P<right>CE|PE)$")


@dataclass(frozen=True)
class ContractKey:
    underlying: str
    expiry: str
    strike: str
    option_right: str
    product_type: str
    # ── Metadata fields (P2-15) — not part of the storage key ──
    exchange: str = ""
    broker_symbol: str = ""
    broker_token: str = ""
    instrument_version: str = ""

    def as_storage_key(self) -> tuple[str, str, str, str, str]:
        """Return only the original 5 identity fields (backwards-compatible)."""
        return (
            self.underlying,
            self.expiry,
            self.strike,
            self.option_right,
            self.product_type,
        )

    def as_log_text(self) -> str:
        base = (
            f"{self.underlying}|{self.expiry}|{self.strike}|"
            f"{self.option_right}|{self.product_type}"
        )
        extras = []
        if self.exchange:
            extras.append(f"exch={self.exchange}")
        if self.broker_symbol:
            extras.append(f"bsym={self.broker_symbol}")
        if self.broker_token:
            extras.append(f"tok={self.broker_token}")
        if self.instrument_version:
            extras.append(f"ver={self.instrument_version}")
        if extras:
            base += f" [{', '.join(extras)}]"
        return base


@dataclass(frozen=True)
class OwnershipKey:
    tenant_id: str
    account_id: str
    broker_account_id: str
    product_type: str
    normalized_contract_key: tuple[str, str, str, str, str]

    def as_scope_key(self) -> tuple[str, str, str, tuple[str, str, str, str, str]]:
        return (
            self.tenant_id,
            self.account_id,
            self.broker_account_id,
            self.normalized_contract_key,
        )


@dataclass(frozen=True)
class PositionOwnershipDecision:
    allowed: bool
    owner: Optional[str]
    reason: str
    acquired_pending: bool = False


@dataclass(frozen=True)
class PositionOwnershipReconcileResult:
    added_unknown: int = 0
    converted_to_unknown: int = 0
    removed_stale: int = 0
    parse_errors: int = 0

    @property
    def changed(self) -> bool:
        return bool(
            self.added_unknown
            or self.converted_to_unknown
            or self.removed_stale
            or self.parse_errors
        )


@dataclass
class _OwnershipEntry:
    pending_by_strategy: dict[str, int] = field(default_factory=dict)
    net_by_strategy: dict[str, int] = field(default_factory=dict)
    unknown_net_qty: int = 0
    authority_path: str = ""
    zero_broker_poll_count: int = 0  # consecutive zero-position polls; >= 2 required to delete


class OwnershipAuthorityViolation(RuntimeError):
    """Raised when a non-authoritative path attempts to mutate ownership."""

    def __init__(
        self,
        *,
        ownership_key: str,
        existing_authority_path: str,
        requested_authority_path: str,
        action: str,
    ) -> None:
        self.ownership_key = ownership_key
        self.existing_authority_path = existing_authority_path
        self.requested_authority_path = requested_authority_path
        self.action = action
        super().__init__(
            "Cross-authority mutation forbidden for "
            f"{ownership_key}: existing={existing_authority_path} "
            f"requested={requested_authority_path} action={action}"
        )


class OwnershipPersistenceBackend(Protocol):
    def load_account_entries(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
    ) -> dict[tuple[str, str, str, str, str], _OwnershipEntry]: ...

    def save_contract_state(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        contract_key: ContractKey,
        entry: Optional[_OwnershipEntry],
    ) -> None: ...


def _is_true(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _safe_identifier(raw: str, default: str) -> str:
    text = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in str(raw or ""))
    text = text.strip("_")
    return text or default


def _encode_pending_strategy_id(strategy_id: object) -> str:
    return f"{_PENDING_STRATEGY_ID_PREFIX}{str(strategy_id or '').strip()}"


def _decode_pending_strategy_id(raw_strategy_id: object) -> Optional[str]:
    text = str(raw_strategy_id or "").strip()
    if not text.startswith(_PENDING_STRATEGY_ID_PREFIX):
        return None
    pending_strategy_id = text[len(_PENDING_STRATEGY_ID_PREFIX) :].strip()
    return pending_strategy_id or None


def _normalize_authority_path(value: object, *, default: str = "") -> str:
    text = str(value or "").strip().lower()
    if text in {"hub", "legacy"}:
        return text
    return str(default or "").strip().lower()


class _NoopPersistenceBackend:
    def load_account_entries(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
    ) -> dict[tuple[str, str, str, str, str], _OwnershipEntry]:
        _ = (tenant_id, broker_account_id)
        return {}

    def save_contract_state(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        contract_key: ContractKey,
        entry: Optional[_OwnershipEntry],
    ) -> None:
        _ = (tenant_id, broker_account_id, contract_key, entry)
        return


class PostgresPositionOwnershipBackend:
    def __init__(
        self,
        *,
        dsn: str,
        table_name: str = "position_ownership_ledger",
        persist_pending_locks: bool = False,
    ) -> None:
        self._dsn = dsn
        self._table = _safe_identifier(table_name, "position_ownership_ledger")
        self._persist_pending_locks = bool(persist_pending_locks)
        self._assert_schema_exists()

    def _conn(self):
        return connect_with_retry(self._dsn, autocommit=False)

    def _assert_schema_exists(self) -> None:
        """Assert the ownership table was created by migration 008. Never create it here."""
        try:
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT 1 FROM information_schema.tables "
                        "WHERE table_name = %s LIMIT 1",
                        (self._table,),
                    )
                    if not cur.fetchone():
                        raise RuntimeError(
                            f"position_ownership: table '{self._table}' does not exist. "
                            f"Run migrations/008_position_ownership_ledger.sql before starting LIVE."
                        )
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"position_ownership: could not verify table '{self._table}': {exc}"
            ) from exc

    def load_account_entries(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
    ) -> dict[tuple[str, str, str, str, str], _OwnershipEntry]:
        query = f"""
            SELECT
                underlying,
                expiry,
                strike,
                option_right,
                product_type,
                strategy_id,
                authority_path,
                net_qty
            FROM {self._table}
            WHERE tenant_id = %(tenant_id)s
              AND broker_account_id = %(broker_account_id)s
              AND net_qty <> 0
        """
        payload = {
            "tenant_id": str(tenant_id),
            "broker_account_id": str(broker_account_id),
        }
        entries: dict[tuple[str, str, str, str, str], _OwnershipEntry] = {}
        pending_rows_loaded = 0
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(query, payload)
                rows = cur.fetchall() or []
            conn.commit()
        for row in rows:
            (
                underlying,
                expiry,
                strike,
                option_right,
                product_type,
                strategy_id,
                authority_path,
                net_qty,
            ) = row
            key = (
                str(underlying),
                str(expiry),
                str(strike),
                str(option_right),
                str(product_type),
            )
            entry = entries.get(key)
            if entry is None:
                entry = _OwnershipEntry()
                entries[key] = entry
            if not entry.authority_path:
                entry.authority_path = _normalize_authority_path(
                    authority_path,
                    default="hub",
                )
            qty = int(net_qty or 0)
            if qty == 0:
                continue
            sid = str(strategy_id or "").strip()
            pending_sid = (
                _decode_pending_strategy_id(sid)
                if self._persist_pending_locks
                else None
            )
            if pending_sid is not None:
                entry.pending_by_strategy[pending_sid] = int(
                    entry.pending_by_strategy.get(pending_sid, 0)
                ) + abs(qty)
                pending_rows_loaded += 1
            elif sid == UNKNOWN_STRATEGY_ID:
                entry.unknown_net_qty += qty
            elif sid:
                entry.net_by_strategy[sid] = int(entry.net_by_strategy.get(sid, 0)) + qty
        if pending_rows_loaded > 0:
            logger.info(
                "position_ownership_pending_rows_loaded tenant_id=%s broker_account_id=%s rows=%d",
                tenant_id,
                broker_account_id,
                pending_rows_loaded,
            )
        return entries

    def save_contract_state(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        contract_key: ContractKey,
        entry: Optional[_OwnershipEntry],
    ) -> None:
        storage_key = contract_key.as_storage_key()
        delete_sql = f"""
            DELETE FROM {self._table}
            WHERE tenant_id = %(tenant_id)s
              AND broker_account_id = %(broker_account_id)s
              AND underlying = %(underlying)s
              AND expiry = %(expiry)s
              AND strike = %(strike)s
              AND option_right = %(option_right)s
              AND product_type = %(product_type)s
        """
        insert_sql = f"""
            INSERT INTO {self._table} (
                tenant_id,
                broker_account_id,
                underlying,
                expiry,
                strike,
                option_right,
                product_type,
                strategy_id,
                authority_path,
                net_qty,
                updated_at
            ) VALUES (
                %(tenant_id)s,
                %(broker_account_id)s,
                %(underlying)s,
                %(expiry)s,
                %(strike)s,
                %(option_right)s,
                %(product_type)s,
                %(strategy_id)s,
                %(authority_path)s,
                %(net_qty)s,
                %(updated_at)s
            )
        """
        base_payload = {
            "tenant_id": str(tenant_id),
            "broker_account_id": str(broker_account_id),
            "underlying": storage_key[0],
            "expiry": storage_key[1],
            "strike": storage_key[2],
            "option_right": storage_key[3],
            "product_type": storage_key[4],
        }
        write_rows: list[dict[str, Any]] = []
        pending_rows_persisted = 0
        if entry is not None:
            authority_path = _normalize_authority_path(entry.authority_path, default="hub")
            for sid, qty in (entry.net_by_strategy or {}).items():
                net_qty = int(qty or 0)
                if net_qty == 0:
                    continue
                write_rows.append(
                    {
                        **base_payload,
                        "strategy_id": str(sid),
                        "authority_path": authority_path,
                        "net_qty": net_qty,
                        "updated_at": datetime.now(timezone.utc),
                    }
                )
            unknown_qty = int(entry.unknown_net_qty or 0)
            if unknown_qty != 0:
                write_rows.append(
                    {
                        **base_payload,
                        "strategy_id": UNKNOWN_STRATEGY_ID,
                        "authority_path": authority_path,
                        "net_qty": unknown_qty,
                        "updated_at": datetime.now(timezone.utc),
                    }
                )
            if self._persist_pending_locks:
                for sid, pending_count in (entry.pending_by_strategy or {}).items():
                    pending_qty = int(pending_count or 0)
                    if pending_qty <= 0:
                        continue
                    write_rows.append(
                        {
                            **base_payload,
                            "strategy_id": _encode_pending_strategy_id(sid),
                            "authority_path": authority_path,
                            "net_qty": abs(pending_qty),
                            "updated_at": datetime.now(timezone.utc),
                        }
                    )
                    pending_rows_persisted += 1
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(delete_sql, base_payload)
                for row in write_rows:
                    cur.execute(insert_sql, row)
            conn.commit()
        if pending_rows_persisted > 0:
            logger.info(
                "position_ownership_pending_rows_persisted tenant_id=%s broker_account_id=%s contract=%s rows=%d",
                tenant_id,
                broker_account_id,
                contract_key.as_log_text(),
                pending_rows_persisted,
            )


def _normalize_text(value: object) -> str:
    return str(value or "").strip().upper()


def _normalize_product_type(value: object) -> str:
    product = getattr(value, "value", value)
    text = _normalize_text(product)
    return text or "UNKNOWN_PRODUCT"


def _normalize_option_right(value: object) -> Optional[str]:
    text = _normalize_text(value)
    if text in {"CE", "PE"}:
        return text
    return None


def _normalize_underlying(value: object) -> Optional[str]:
    canonical = canonical_underlying_key(value)
    return canonical or None


def _normalize_strike(value: object) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        dec = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if dec <= 0:
        return None
    normalized = format(dec.normalize(), "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or None


def _normalize_expiry(value: object) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).date().isoformat()
    except Exception:
        pass
    for fmt in ("%d%b%Y", "%d%b%y", "%d-%b-%Y", "%d-%b-%y"):
        try:
            return datetime.strptime(text.upper(), fmt).date().isoformat()
        except Exception:
            continue
    return None


def _normalize_numeric_expiry_segment(value: object) -> Optional[str]:
    digits = str(value or "").strip()
    if len(digits) == 5:
        month_text = digits[2]
        day_text = digits[3:]
    elif len(digits) == 6:
        month_text = digits[2:4]
        day_text = digits[4:]
    else:
        return None
    try:
        year = 2000 + int(digits[:2])
        month = int(month_text)
        day = int(day_text)
        return datetime(year, month, day, tzinfo=timezone.utc).date().isoformat()
    except Exception:
        return None


def _extract_from_numeric_expiry_symbol(
    symbol: str,
) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    match = _NUMERIC_OPTION_SYMBOL_PATTERN.match(symbol)
    if not match:
        return None, None, None, None

    underlying = _normalize_underlying(match.group("underlying"))
    option_right = _normalize_option_right(match.group("right"))
    body = str(match.group("body") or "").strip()
    if len(body) <= 5:
        return None, None, None, None

    expiry_lengths: list[int] = [5]
    if len(body) >= 6:
        month_two_digits = body[2:4]
        if month_two_digits in {"10", "11", "12"}:
            expiry_lengths.insert(0, 6)
        else:
            expiry_lengths.append(6)

    for expiry_len in expiry_lengths:
        if len(body) <= expiry_len:
            continue
        expiry = _normalize_numeric_expiry_segment(body[:expiry_len])
        strike = _normalize_strike(body[expiry_len:])
        if expiry and strike:
            return underlying, expiry, strike, option_right
    return underlying, None, None, option_right


def _normalize_symbol(symbol: object) -> str:
    raw = _normalize_text(symbol)
    return re.sub(r"[^A-Z0-9]", "", raw)


def _right_from_kind(kind: object) -> Optional[str]:
    text = _normalize_text(kind)
    if text.endswith("_CE") or text == "CE":
        return "CE"
    if text.endswith("_PE") or text == "PE":
        return "PE"
    return None


def _extract_from_meta(meta: Optional[dict]) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    if not isinstance(meta, dict):
        return None, None, None, None
    underlying = (
        _normalize_underlying(meta.get("underlying"))
        or _normalize_underlying(meta.get("underlying_name"))
        or _normalize_underlying(meta.get("name"))
    )
    expiry = (
        _normalize_expiry(meta.get("expiry"))
        or _normalize_expiry(meta.get("expiry_dt"))
        or _normalize_expiry(meta.get("expiryDate"))
    )
    strike = _normalize_strike(meta.get("strike"))
    option_right = _right_from_kind(meta.get("kind"))
    return underlying, expiry, strike, option_right


def _meta_text(meta: Optional[dict], *keys: str) -> str:
    if not isinstance(meta, dict):
        return ""
    for key in keys:
        value = meta.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _derive_instrument_version(
    *,
    meta: Optional[dict],
    normalized_symbol: str,
    broker_token: str,
) -> str:
    explicit_version = _meta_text(
        meta,
        "instrument_version",
        "version",
        "instrument_hash",
        "instrumentVersion",
    )
    if explicit_version:
        return explicit_version
    seed = "|".join(
        value
        for value in (
            normalized_symbol,
            broker_token,
            _meta_text(meta, "exchange", "exchange_segment", "exchangeType"),
            _meta_text(meta, "expiry", "expiry_dt", "expiryDate"),
            _meta_text(meta, "strike"),
            _meta_text(meta, "kind", "option_type"),
        )
        if value
    )
    if not seed:
        return ""
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]


def _extract_contract_metadata(
    *,
    meta: Optional[dict],
    symbol: object,
    symbol_token: Optional[object],
) -> tuple[str, str, str, str]:
    exchange = _meta_text(meta, "exchange", "exchange_segment", "exchangeType")
    broker_symbol = (
        _meta_text(meta, "symbol", "tradingsymbol", "trading_symbol")
        or _normalize_text(symbol)
    )
    broker_token = str(symbol_token or "").strip() or _meta_text(
        meta,
        "token",
        "symboltoken",
        "instrument_token",
        "broker_token",
    )
    instrument_version = _derive_instrument_version(
        meta=meta,
        normalized_symbol=_normalize_symbol(symbol),
        broker_token=broker_token,
    )
    return exchange, broker_symbol, broker_token, instrument_version


def _extract_from_symbol(symbol: str) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    if not symbol:
        return None, None, None, None
    match = _OPTION_SYMBOL_PATTERN.match(symbol)
    if match:
        underlying = _normalize_underlying(match.group("underlying"))
        option_right = _normalize_option_right(match.group("right"))
        strike = _normalize_strike(match.group("strike"))
        expiry = _normalize_expiry(
            f"{match.group('day')}{match.group('mon')}{match.group('year')}"
        )
        return underlying, expiry, strike, option_right
    numeric_underlying, numeric_expiry, numeric_strike, numeric_right = (
        _extract_from_numeric_expiry_symbol(symbol)
    )
    if numeric_underlying or numeric_expiry or numeric_strike or numeric_right:
        return numeric_underlying, numeric_expiry, numeric_strike, numeric_right
    tail = _OPTION_STRIKE_SUFFIX_PATTERN.search(symbol)
    if not tail:
        return None, None, None, None
    option_right = _normalize_option_right(tail.group("right"))
    strike = _normalize_strike(tail.group("strike"))
    head = symbol[: tail.start()]
    underlying = _normalize_underlying(re.sub(r"\d+$", "", head))
    return underlying, None, strike, option_right


def _derive_contract_key_from_values(
    *,
    symbol: object,
    product_type: object,
    symbol_token: Optional[object] = None,
    meta_resolver: Optional[Callable[..., Optional[dict]]] = None,
) -> tuple[Optional[ContractKey], Optional[str]]:
    resolver = meta_resolver or dashboard_bus.resolve_instrument_meta
    meta = resolver(symbol=symbol, token=symbol_token)

    normalized_symbol = _normalize_symbol(symbol)
    normalized_product_type = _normalize_product_type(product_type)
    (
        exchange,
        broker_symbol,
        broker_token,
        instrument_version,
    ) = _extract_contract_metadata(
        meta=meta,
        symbol=symbol,
        symbol_token=symbol_token,
    )

    meta_underlying, meta_expiry, meta_strike, meta_right = _extract_from_meta(meta)
    sym_underlying, sym_expiry, sym_strike, sym_right = _extract_from_symbol(normalized_symbol)

    option_right = meta_right or sym_right
    if option_right in {"CE", "PE"}:
        underlying = meta_underlying or sym_underlying
        expiry = meta_expiry or sym_expiry
        strike = meta_strike or sym_strike
        if not underlying:
            return None, "missing_underlying"
        if not expiry:
            return None, "missing_expiry"
        if not strike:
            return None, "missing_strike"
        return (
            ContractKey(
                underlying=underlying,
                expiry=expiry,
                strike=strike,
                option_right=option_right,
                product_type=normalized_product_type,
                exchange=exchange,
                broker_symbol=broker_symbol,
                broker_token=broker_token,
                instrument_version=instrument_version,
            ),
            None,
        )

    underlying = meta_underlying or _normalize_underlying(normalized_symbol)
    if not underlying:
        return None, "missing_underlying"
    return (
        ContractKey(
            underlying=underlying,
            expiry="NA",
            strike="NA",
            option_right="NA",
            product_type=normalized_product_type,
            exchange=exchange,
            broker_symbol=broker_symbol,
            broker_token=broker_token,
            instrument_version=instrument_version,
        ),
        None,
    )


def derive_contract_key(
    order_req: OrderRequest,
    *,
    meta_resolver: Optional[Callable[..., Optional[dict]]] = None,
) -> tuple[Optional[ContractKey], Optional[str]]:
    return _derive_contract_key_from_values(
        symbol=order_req.symbol,
        product_type=order_req.product_type,
        symbol_token=order_req.symbol_token,
        meta_resolver=meta_resolver,
    )


def _position_field(position: object, key: str, default: object = None) -> object:
    if isinstance(position, dict):
        return position.get(key, default)
    return getattr(position, key, default)


def derive_contract_key_from_position(
    position: object,
    *,
    meta_resolver: Optional[Callable[..., Optional[dict]]] = None,
) -> tuple[Optional[ContractKey], Optional[str]]:
    symbol = _position_field(position, "symbol")
    product_type = _position_field(position, "product_type")
    symbol_token = (
        _position_field(position, "symbol_token")
        or _position_field(position, "symboltoken")
        or _position_field(position, "token")
        or _position_field(position, "instrument_token")
    )
    return _derive_contract_key_from_values(
        symbol=symbol,
        product_type=product_type,
        symbol_token=symbol_token,
        meta_resolver=meta_resolver,
    )


def normalize_contract_key(contract_key: ContractKey) -> ContractKey:
    underlying = _normalize_underlying(contract_key.underlying)
    if not underlying:
        raise ValueError(f"Invalid contract underlying: {contract_key.underlying!r}")

    expiry = _normalize_expiry(contract_key.expiry)
    if not expiry:
        raise ValueError(f"Invalid contract expiry: {contract_key.expiry!r}")

    strike = _normalize_strike(contract_key.strike)
    if not strike:
        raise ValueError(f"Invalid contract strike: {contract_key.strike!r}")

    option_right = _normalize_option_right(contract_key.option_right)
    if not option_right:
        raise ValueError(
            f"Invalid contract option_right: {contract_key.option_right!r}"
        )

    product_type = _normalize_product_type(contract_key.product_type)
    if not product_type or product_type == "UNKNOWN_PRODUCT":
        raise ValueError(
            f"Invalid contract product_type: {contract_key.product_type!r}"
        )

    return ContractKey(
        underlying=underlying,
        expiry=expiry,
        strike=strike,
        option_right=option_right,
        product_type=product_type,
        exchange=str(contract_key.exchange or "").strip(),
        broker_symbol=str(contract_key.broker_symbol or "").strip(),
        broker_token=str(contract_key.broker_token or "").strip(),
        instrument_version=str(contract_key.instrument_version or "").strip(),
    )


def render_contract(contract_key: Optional[ContractKey]) -> str:
    if contract_key is None:
        return UNKNOWN_OWNER
    return contract_key.as_log_text()


def derive_ownership_key(
    *,
    tenant_id: str,
    account_id: str,
    broker_account_id: str,
    contract_key: ContractKey,
) -> OwnershipKey:
    """Derive the canonical OwnershipKey per Architecture section 4.2.

    The OwnershipKey uniquely identifies the nettable scope for a contract
    within a tenant/account/broker-account boundary.  Strategy is NOT part
    of the key -- it is metadata stored against the key.
    """
    return OwnershipKey(
        tenant_id=str(tenant_id).strip(),
        account_id=str(account_id).strip(),
        broker_account_id=str(broker_account_id).strip(),
        product_type=contract_key.product_type,
        normalized_contract_key=contract_key.as_storage_key(),
    )


class PositionOwnershipStore:
    """
    Ownership ledger by (tenant, broker account, contract key).

    Ownership is inferred from:
    - non-zero net fills tracked per strategy,
    - unknown broker net quantities from reconciliation, and
    - pending submit-time locks.
    """

    def __init__(
        self,
        *,
        backend: Optional[OwnershipPersistenceBackend] = None,
        operating_mode: OperatingMode = OperatingMode.HUB_AUTHORITATIVE,
    ) -> None:
        self._lock = threading.RLock()
        self._backend: OwnershipPersistenceBackend = backend or _NoopPersistenceBackend()
        self._operating_mode = operating_mode
        self._authority_path = operating_mode.authority_path
        self._entries: dict[tuple[str, str, str, tuple[str, str, str, str, str]], _OwnershipEntry] = {}
        self._ownership_records: dict[
            tuple[str, str, str, tuple[str, str, str, str, str]],
            OwnershipRecord,
        ] = {}
        self._loaded_accounts: set[tuple[str, str]] = set()

    @property
    def operating_mode(self) -> OperatingMode:
        return self._operating_mode

    @property
    def authority_path(self) -> str:
        return self._authority_path

    def mark_all_recovery_pending(self) -> int:
        """Mark all non-terminal ownership records as RECOVERY_PENDING.

        Called during startup before broker reconciliation (Architecture §11.1
        step 4). Records in OWNED or PENDING_LOCK are transitioned to
        RECONCILING to indicate they need broker evidence confirmation.

        Returns the number of records marked.
        """
        marked = 0
        with self._lock:
            for key, record in self._ownership_records.items():
                if record.state in (
                    OwnershipState.OWNED,
                    OwnershipState.PENDING_LOCK,
                ):
                    record.state = OwnershipState.RECONCILING
                    marked += 1
        if marked:
            logger.info(
                "Marked %d ownership records as RECONCILING (recovery pending)",
                marked,
            )
        return marked

    @staticmethod
    def _ownership_key_text(
        scoped_key: tuple[str, str, str, tuple[str, str, str, str, str]]
    ) -> str:
        tenant_id, account_id, broker_account_id, contract_storage_key = scoped_key
        underlying, expiry, strike, option_right, product_type = contract_storage_key
        return (
            f"{tenant_id}:{account_id}:{broker_account_id}:{product_type}:"
            f"{underlying}:{expiry}:{strike}:{option_right}"
        )

    @staticmethod
    def _owner_candidates(entry: _OwnershipEntry) -> tuple[list[str], list[str]]:
        net_owners = [
            sid for sid, qty in (entry.net_by_strategy or {}).items() if int(qty) != 0
        ]
        pending_owners = [
            sid
            for sid, count in (entry.pending_by_strategy or {}).items()
            if int(count) > 0
        ]
        return net_owners, pending_owners

    @classmethod
    def _infer_record_target(
        cls,
        entry: _OwnershipEntry,
    ) -> tuple[OwnershipState, Optional[str], str]:
        if int(entry.unknown_net_qty or 0) != 0:
            return (
                OwnershipState.RECONCILING,
                None,
                "unknown_broker_net_requires_reconciliation",
            )
        net_owners, pending_owners = cls._owner_candidates(entry)
        if len(net_owners) > 1 or len(pending_owners) > 1:
            return (
                OwnershipState.ORPHAN_REVIEW,
                None,
                "ambiguous_multi_strategy_ownership",
            )
        if len(net_owners) == 1:
            return OwnershipState.OWNED, net_owners[0], "confirmed_net_owner"
        if len(pending_owners) == 1:
            return OwnershipState.PENDING_LOCK, pending_owners[0], "pending_lock_owner"
        return OwnershipState.NONE, None, "no_active_ownership"

    def _entry_authority_path(
        self,
        entry: Optional[_OwnershipEntry],
        authority_path: str | None = None,
    ) -> str:
        return _normalize_authority_path(
            getattr(entry, "authority_path", "") if entry is not None else authority_path,
            default=authority_path or self._authority_path,
        )

    def _assert_mutation_authority(
        self,
        *,
        scoped_key: tuple[str, str, str, tuple[str, str, str, str, str]],
        entry: Optional[_OwnershipEntry],
        actor: str,
        action: str,
        request_id: str,
    ) -> str:
        requested_authority_path = self._authority_path
        record = self._ownership_records.get(scoped_key)
        existing_authority_path = _normalize_authority_path(
            (
                record.authority_path
                if record is not None
                else getattr(entry, "authority_path", "")
            ),
        )
        if (
            existing_authority_path
            and existing_authority_path != requested_authority_path
        ):
            ownership_key = (
                record.ownership_key
                if record is not None
                else self._ownership_key_text(scoped_key)
            )
            emit_audit_event(
                actor=actor,
                action="ownership_cross_authority_mutation_blocked",
                resource_type="ownership_record",
                resource_id=ownership_key,
                before={
                    "authority_path": existing_authority_path,
                },
                after={
                    "authority_path": existing_authority_path,
                    "requested_authority_path": requested_authority_path,
                    "blocked_action": action,
                },
                request_id=request_id or None,
            )
            raise OwnershipAuthorityViolation(
                ownership_key=ownership_key,
                existing_authority_path=existing_authority_path,
                requested_authority_path=requested_authority_path,
                action=action,
            )
        if entry is not None:
            entry.authority_path = requested_authority_path
        if record is not None:
            record.authority_path = requested_authority_path
        return requested_authority_path

    def _ensure_record_for_scoped_key(
        self,
        *,
        scoped_key: tuple[str, str, str, tuple[str, str, str, str, str]],
        entry: Optional[_OwnershipEntry],
        authority_path: str = "",
    ) -> Optional[OwnershipRecord]:
        record = self._ownership_records.get(scoped_key)
        if record is not None:
            return record
        if entry is None or self._entry_empty(entry):
            return None
        record = OwnershipRecord(
            ownership_key=self._ownership_key_text(scoped_key),
            authority_path=self._entry_authority_path(entry, authority_path),
        )
        self._ownership_records[scoped_key] = record
        return record

    def _apply_record_transition(
        self,
        *,
        record: OwnershipRecord,
        target: OwnershipState,
        reason: str,
    ) -> None:
        now = datetime.now(timezone.utc)
        if record.state == target:
            record.state_reason = reason
            record.updated_at = now
            if target != OwnershipState.NONE:
                record.last_evidence_at = now
            return
        try:
            record.transition_to(target, reason)
        except InvalidOwnershipTransition:
            if record.state != OwnershipState.RECONCILING:
                try:
                    record.transition_to(
                        OwnershipState.RECONCILING,
                        f"{reason}_reconciling_handoff",
                    )
                    if target != OwnershipState.RECONCILING:
                        record.transition_to(target, reason)
                    return
                except InvalidOwnershipTransition:
                    pass
            logger.warning(
                "PositionOwnershipStore: replacing invalid ownership transition ownership_key=%s current=%s target=%s reason=%s",
                record.ownership_key,
                record.state.value,
                target.value,
                reason,
            )
            replacement = OwnershipRecord(
                ownership_key=record.ownership_key,
                state=target,
                state_reason=reason,
                authority_path=record.authority_path,
                owner_strategy_id=record.owner_strategy_id,
                position_id=record.position_id,
                opened_by_order_id=record.opened_by_order_id,
                acquired_at=record.acquired_at,
                last_evidence_at=record.last_evidence_at,
                break_glass_override_id=record.break_glass_override_id,
            )
            record.state = replacement.state
            record.state_reason = replacement.state_reason
            record.authority_path = replacement.authority_path
            record.updated_at = replacement.updated_at
        if target != OwnershipState.NONE:
            record.last_evidence_at = now

    def _sync_ownership_record_from_entry(
        self,
        *,
        scoped_key: tuple[str, str, str, tuple[str, str, str, str, str]],
        entry: Optional[_OwnershipEntry],
        reason: str,
        authority_path: str = "",
    ) -> None:
        if entry is None or self._entry_empty(entry):
            record = self._ownership_records.get(scoped_key)
            if record is None:
                return
            self._apply_record_transition(
                record=record,
                target=OwnershipState.NONE,
                reason=reason,
            )
            self._ownership_records.pop(scoped_key, None)
            return

        record = self._ensure_record_for_scoped_key(
            scoped_key=scoped_key,
            entry=entry,
            authority_path=authority_path,
        )
        if record is None:
            return
        target, owner_strategy_id, inferred_reason = self._infer_record_target(entry)
        self._apply_record_transition(
            record=record,
            target=target,
            reason=str(reason or inferred_reason),
        )
        record.owner_strategy_id = owner_strategy_id
        if target == OwnershipState.OWNED and record.acquired_at is None:
            record.acquired_at = datetime.now(timezone.utc)

    def _update_ownership_record_state(
        self,
        *,
        scoped_key: tuple[str, str, str, tuple[str, str, str, str, str]],
        entry: Optional[_OwnershipEntry],
        target: OwnershipState,
        reason: str,
        owner_strategy_id: Optional[str] = None,
        authority_path: str = "",
    ) -> None:
        record = self._ensure_record_for_scoped_key(
            scoped_key=scoped_key,
            entry=entry,
            authority_path=authority_path,
        )
        if record is None:
            return
        if owner_strategy_id is not None:
            record.owner_strategy_id = owner_strategy_id
        self._apply_record_transition(record=record, target=target, reason=reason)
        if target == OwnershipState.OWNED and record.acquired_at is None:
            record.acquired_at = datetime.now(timezone.utc)

    def _execute_scoped_mutation(
        self,
        *,
        scoped_key: tuple[str, str, str, tuple[str, str, str, str, str]],
        priority: MutationPriority,
        actor: str,
        request_id: str,
        mutation_fn: Callable[[], Any],
    ) -> Any:
        return scope_serializer.execute(
            ScopedMutation(
                ownership_key=self._ownership_key_text(scoped_key),
                priority=priority,
                mutation_fn=mutation_fn,
                request_id=request_id,
                actor=actor,
            )
        )

    @staticmethod
    def _account_scope(
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
    ) -> tuple[str, str]:
        return str(tenant_id), str(broker_account_id)

    @staticmethod
    def _scope_key(
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        contract_key: ContractKey,
        account_id: Optional[str] = None,
    ) -> tuple[str, str, str, tuple[str, str, str, str, str]]:
        return derive_ownership_key(
            tenant_id=str(tenant_id),
            account_id=str(account_id or broker_account_id),
            broker_account_id=str(broker_account_id),
            contract_key=contract_key,
        ).as_scope_key()

    @staticmethod
    def _normalize_unknown_mode(mode: object) -> str:
        text = str(mode or "").strip().lower()
        if text in _UNKNOWN_MODES:
            return text
        return UNKNOWN_MODE_BLOCK_ENTRIES

    @staticmethod
    def _owner_for_entry(entry: _OwnershipEntry) -> Optional[str]:
        if int(entry.unknown_net_qty or 0) != 0:
            return UNKNOWN_OWNER
        net_owners = [sid for sid, qty in entry.net_by_strategy.items() if int(qty) != 0]
        if len(net_owners) == 1:
            return net_owners[0]
        if len(net_owners) > 1:
            return UNKNOWN_OWNER

        pending_owners = [
            sid for sid, count in entry.pending_by_strategy.items() if int(count) > 0
        ]
        if len(pending_owners) == 1:
            return pending_owners[0]
        if len(pending_owners) > 1:
            return UNKNOWN_OWNER
        return None

    @staticmethod
    def _cleanup_entry(entry: _OwnershipEntry) -> None:
        entry.pending_by_strategy = {
            sid: int(count)
            for sid, count in (entry.pending_by_strategy or {}).items()
            if int(count) > 0
        }
        entry.net_by_strategy = {
            sid: int(qty)
            for sid, qty in (entry.net_by_strategy or {}).items()
            if int(qty) != 0
        }
        entry.unknown_net_qty = int(entry.unknown_net_qty or 0)

    @staticmethod
    def _entry_empty(entry: _OwnershipEntry) -> bool:
        return (
            int(entry.unknown_net_qty or 0) == 0
            and not entry.pending_by_strategy
            and not entry.net_by_strategy
        )

    def _ensure_account_loaded(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
    ) -> None:
        account_scope = self._account_scope(tenant_id, broker_account_id)
        if account_scope in self._loaded_accounts:
            return
        loaded = self._backend.load_account_entries(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
        )
        for contract_storage_key, entry in (loaded or {}).items():
            self._cleanup_entry(entry)
            if self._entry_empty(entry):
                continue
            scoped_key = (account_scope[0], account_scope[1], account_scope[1], contract_storage_key)
            self._entries[scoped_key] = entry
            self._sync_ownership_record_from_entry(
                scoped_key=scoped_key,
                entry=entry,
                reason="loaded_from_persistence",
            )
        self._loaded_accounts.add(account_scope)

    def _persist_contract_state(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        contract_key: ContractKey,
        entry: Optional[_OwnershipEntry],
    ) -> None:
        self._backend.save_contract_state(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            contract_key=contract_key,
            entry=entry,
        )

    def get_owner(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        contract_key: Optional[ContractKey],
    ) -> Optional[str]:
        if contract_key is None:
            return UNKNOWN_OWNER
        with self._lock:
            self._ensure_account_loaded(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
            )
            entry = self._entries.get(
                self._scope_key(tenant_id, broker_account_id, contract_key)
            )
            if entry is None:
                return None
            return self._owner_for_entry(entry)

    def get_ownership_record(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        contract_key: Optional[ContractKey],
    ) -> Optional[OwnershipRecord]:
        if contract_key is None:
            return None
        with self._lock:
            self._ensure_account_loaded(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
            )
            scoped = self._scope_key(tenant_id, broker_account_id, contract_key)
            entry = self._entries.get(scoped)
            if entry is not None and scoped not in self._ownership_records:
                self._sync_ownership_record_from_entry(
                    scoped_key=scoped,
                    entry=entry,
                    reason="ownership_record_lookup",
                )
            record = self._ownership_records.get(scoped)
            return copy.deepcopy(record) if record is not None else None

    def get_net_quantity(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        contract_key: Optional[ContractKey],
    ) -> int:
        """Return the net owned quantity for a contract across all strategies.

        Returns 0 if the contract is not tracked.  Positive means net long;
        negative means net short.  This is a read-only helper used for
        duplicate-order guards (e.g. EOD square-off).
        """
        if contract_key is None:
            return 0
        with self._lock:
            self._ensure_account_loaded(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
            )
            entry = self._entries.get(
                self._scope_key(tenant_id, broker_account_id, contract_key)
            )
            if entry is None:
                return 0
            net = sum(int(v or 0) for v in entry.net_by_strategy.values())
            net += int(entry.unknown_net_qty or 0)
            return net

    def observe_fill_progress(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        contract_key: Optional[ContractKey],
        strategy_id: StrategyId,
        is_exit_order: bool,
        filled_qty: int,
    ) -> None:
        if contract_key is None or int(filled_qty or 0) <= 0:
            return
        scoped = self._scope_key(tenant_id, broker_account_id, contract_key)

        def _mutate() -> None:
            with self._lock:
                self._ensure_account_loaded(
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                )
                entry = self._entries.get(scoped)
                if entry is None:
                    entry = _OwnershipEntry()
                    self._entries[scoped] = entry
                authority_path = self._assert_mutation_authority(
                    scoped_key=scoped,
                    entry=entry,
                    actor="order_lifecycle",
                    action="observe_fill_progress",
                    request_id=(
                        f"observe_fill_progress:{str(strategy_id)}:{int(filled_qty)}"
                    ),
                )
                target = (
                    OwnershipState.RELEASING
                    if bool(is_exit_order)
                    else OwnershipState.OWNED
                )
                self._update_ownership_record_state(
                    scoped_key=scoped,
                    entry=entry,
                    target=target,
                    reason=(
                        "partial_exit_fill_observed"
                        if bool(is_exit_order)
                        else "partial_entry_fill_observed"
                    ),
                    owner_strategy_id=str(strategy_id),
                    authority_path=authority_path,
                )

        self._execute_scoped_mutation(
            scoped_key=scoped,
            priority=MutationPriority.TERMINAL_EVIDENCE,
            actor="order_lifecycle",
            request_id=f"observe_fill_progress:{str(strategy_id)}:{int(filled_qty)}",
            mutation_fn=_mutate,
        )

    def try_acquire(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        contract_key: Optional[ContractKey],
        strategy_id: StrategyId,
        is_exit_order: bool,
        unknown_mode: object,
    ) -> PositionOwnershipDecision:
        strategy = str(strategy_id)
        mode = self._normalize_unknown_mode(unknown_mode)
        if contract_key is None:
            if is_exit_order or mode == UNKNOWN_MODE_ALLOW_ENTRIES:
                return PositionOwnershipDecision(
                    allowed=True,
                    owner=UNKNOWN_OWNER,
                    reason="unknown_contract_allowed",
                    acquired_pending=False,
                )
            return PositionOwnershipDecision(
                allowed=False,
                owner=UNKNOWN_OWNER,
                reason="unknown_contract_block_entries",
                acquired_pending=False,
            )
        scoped = self._scope_key(tenant_id, broker_account_id, contract_key)

        def _mutate() -> PositionOwnershipDecision:
            with self._lock:
                self._ensure_account_loaded(
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                )
                entry = self._entries.get(scoped)
                if entry is None:
                    entry = _OwnershipEntry()
                    self._entries[scoped] = entry
                authority_path = self._assert_mutation_authority(
                    scoped_key=scoped,
                    entry=entry,
                    actor="order_router",
                    action="try_acquire",
                    request_id=(
                        f"try_acquire:{strategy}:{'exit' if bool(is_exit_order) else 'entry'}"
                    ),
                )
                self._sync_ownership_record_from_entry(
                    scoped_key=scoped,
                    entry=entry,
                    reason="try_acquire_precheck",
                    authority_path=authority_path,
                )
                record = self._ownership_records.get(scoped)
                owner = self._owner_for_entry(entry)
                allow_unknown_exit = owner == UNKNOWN_OWNER and bool(is_exit_order)
                if (
                    not bool(is_exit_order)
                    and record is not None
                    and record.blocks_new_entry()
                ):
                    return PositionOwnershipDecision(
                        allowed=False,
                        owner=owner or record.owner_strategy_id or UNKNOWN_OWNER,
                        reason=f"contract_{record.state.value.lower()}",
                        acquired_pending=False,
                    )
                if owner is None or owner == strategy or allow_unknown_exit:
                    entry.pending_by_strategy[strategy] = (
                        int(entry.pending_by_strategy.get(strategy, 0)) + 1
                    )
                    self._persist_contract_state(
                        tenant_id=tenant_id,
                        broker_account_id=broker_account_id,
                        contract_key=contract_key,
                        entry=entry,
                    )
                    self._sync_ownership_record_from_entry(
                        scoped_key=scoped,
                        entry=entry,
                        reason="try_acquire_persisted",
                        authority_path=authority_path,
                    )
                    if bool(is_exit_order):
                        target = (
                            OwnershipState.RECONCILING
                            if owner == UNKNOWN_OWNER
                            else OwnershipState.RELEASING
                        )
                        self._update_ownership_record_state(
                            scoped_key=scoped,
                            entry=entry,
                            target=target,
                            reason="exit_lock_acquired",
                            owner_strategy_id=(None if owner == UNKNOWN_OWNER else strategy),
                            authority_path=authority_path,
                        )
                    elif owner is None:
                        self._update_ownership_record_state(
                            scoped_key=scoped,
                            entry=entry,
                            target=OwnershipState.PENDING_LOCK,
                            reason="entry_lock_acquired",
                            owner_strategy_id=strategy,
                            authority_path=authority_path,
                        )
                    return PositionOwnershipDecision(
                        allowed=True,
                        owner=owner,
                        reason="acquired",
                        acquired_pending=True,
                    )
                return PositionOwnershipDecision(
                    allowed=False,
                    owner=owner,
                    reason="contract_locked",
                    acquired_pending=False,
                )

        priority = (
            MutationPriority.ROUTINE_EXIT
            if bool(is_exit_order)
            else MutationPriority.FRESH_ENTRY
        )
        return self._execute_scoped_mutation(
            scoped_key=scoped,
            priority=priority,
            actor="order_router",
            request_id=f"try_acquire:{strategy}:{'exit' if bool(is_exit_order) else 'entry'}",
            mutation_fn=_mutate,
        )

    def record_break_glass_override(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        contract_key: ContractKey,
        actor: str,
        override_id: str,
        reason: str,
        request_id: str = "",
    ) -> Optional[OwnershipRecord]:
        normalized_contract_key = normalize_contract_key(contract_key)
        scoped = self._scope_key(
            tenant_id,
            broker_account_id,
            normalized_contract_key,
        )

        def _mutate() -> Optional[OwnershipRecord]:
            with self._lock:
                self._ensure_account_loaded(
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                )
                entry = self._entries.get(scoped)
                authority_path = self._assert_mutation_authority(
                    scoped_key=scoped,
                    entry=entry,
                    actor=actor,
                    action="record_break_glass_override",
                    request_id=request_id or f"record_break_glass_override:{override_id}",
                )
                if entry is not None and scoped not in self._ownership_records:
                    self._sync_ownership_record_from_entry(
                        scoped_key=scoped,
                        entry=entry,
                        reason="break_glass_override_lookup",
                        authority_path=authority_path,
                    )
                record = self._ownership_records.get(scoped)
                if record is None:
                    return None
                now = datetime.now(timezone.utc)
                override_note = (
                    f"break_glass_override:{override_id}:{str(reason or '').strip()}"
                )
                existing_reason = str(record.state_reason or "").strip()
                if override_note not in existing_reason:
                    record.state_reason = (
                        f"{existing_reason}; {override_note}"
                        if existing_reason
                        else override_note
                    )
                record.break_glass_override_id = override_id
                record.updated_at = now
                if record.last_evidence_at is None:
                    record.last_evidence_at = now
                return copy.deepcopy(record)

        return self._execute_scoped_mutation(
            scoped_key=scoped,
            priority=MutationPriority.BREAK_GLASS,
            actor=actor,
            request_id=request_id or f"record_break_glass_override:{override_id}",
            mutation_fn=_mutate,
        )

    def release_pending(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        contract_key: Optional[ContractKey],
        strategy_id: StrategyId,
    ) -> None:
        if contract_key is None:
            return
        strategy = str(strategy_id)
        scoped = self._scope_key(tenant_id, broker_account_id, contract_key)

        def _mutate() -> None:
            with self._lock:
                self._ensure_account_loaded(
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                )
                entry = self._entries.get(scoped)
                if entry is None:
                    return
                authority_path = self._assert_mutation_authority(
                    scoped_key=scoped,
                    entry=entry,
                    actor="order_lifecycle",
                    action="release_pending",
                    request_id=f"release_pending:{strategy}",
                )
                if strategy in entry.pending_by_strategy:
                    entry.pending_by_strategy[strategy] = int(
                        entry.pending_by_strategy.get(strategy, 0)
                    ) - 1
                self._cleanup_entry(entry)
                if self._entry_empty(entry):
                    self._entries.pop(scoped, None)
                    self._persist_contract_state(
                        tenant_id=tenant_id,
                        broker_account_id=broker_account_id,
                        contract_key=contract_key,
                        entry=None,
                    )
                    self._sync_ownership_record_from_entry(
                        scoped_key=scoped,
                        entry=None,
                        reason="release_pending_cleared",
                    )
                else:
                    self._persist_contract_state(
                        tenant_id=tenant_id,
                        broker_account_id=broker_account_id,
                        contract_key=contract_key,
                        entry=entry,
                    )
                    self._sync_ownership_record_from_entry(
                        scoped_key=scoped,
                        entry=entry,
                        reason="release_pending_updated",
                        authority_path=authority_path,
                    )

        self._execute_scoped_mutation(
            scoped_key=scoped,
            priority=MutationPriority.TERMINAL_EVIDENCE,
            actor="order_lifecycle",
            request_id=f"release_pending:{strategy}",
            mutation_fn=_mutate,
        )

    def apply_fill(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        contract_key: Optional[ContractKey],
        strategy_id: StrategyId,
        signed_qty: int,
    ) -> None:
        if contract_key is None:
            return
        strategy = str(strategy_id)
        delta = int(signed_qty or 0)
        scoped = self._scope_key(tenant_id, broker_account_id, contract_key)

        def _mutate() -> None:
            with self._lock:
                self._ensure_account_loaded(
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                )
                entry = self._entries.get(scoped)
                if entry is None:
                    entry = _OwnershipEntry()
                    self._entries[scoped] = entry
                authority_path = self._assert_mutation_authority(
                    scoped_key=scoped,
                    entry=entry,
                    actor="order_lifecycle",
                    action="apply_fill",
                    request_id=f"apply_fill:{strategy}:{delta}",
                )

                if entry.pending_by_strategy.get(strategy, 0) > 0:
                    entry.pending_by_strategy[strategy] = (
                        int(entry.pending_by_strategy.get(strategy, 0)) - 1
                    )

                if delta != 0:
                    if int(entry.unknown_net_qty or 0) != 0:
                        entry.unknown_net_qty = int(entry.unknown_net_qty or 0) + delta
                    else:
                        entry.net_by_strategy[strategy] = int(
                            entry.net_by_strategy.get(strategy, 0)
                        ) + delta

                self._cleanup_entry(entry)
                if self._entry_empty(entry):
                    self._entries.pop(scoped, None)
                    self._persist_contract_state(
                        tenant_id=tenant_id,
                        broker_account_id=broker_account_id,
                        contract_key=contract_key,
                        entry=None,
                    )
                    self._sync_ownership_record_from_entry(
                        scoped_key=scoped,
                        entry=None,
                        reason="apply_fill_flattened",
                    )
                else:
                    self._persist_contract_state(
                        tenant_id=tenant_id,
                        broker_account_id=broker_account_id,
                        contract_key=contract_key,
                        entry=entry,
                    )
                    self._sync_ownership_record_from_entry(
                        scoped_key=scoped,
                        entry=entry,
                        reason="apply_fill_updated",
                        authority_path=authority_path,
                    )

        self._execute_scoped_mutation(
            scoped_key=scoped,
            priority=MutationPriority.TERMINAL_EVIDENCE,
            actor="order_lifecycle",
            request_id=f"apply_fill:{strategy}:{delta}",
            mutation_fn=_mutate,
        )

    def resolve_orphan_review(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        contract_key: ContractKey,
        decision: str,
        actor: str,
        reason: str,
        request_id: str = "",
    ) -> dict[str, Any]:
        """Resolve an ORPHAN_REVIEW position per operator decision (Architecture S11.5).

        Parameters
        ----------
        decision:
            One of ``"ADOPT"``, ``"FLATTEN"``, ``"SUPPRESS"``,
            ``"CONTINUE_OBSERVING"``.
        actor:
            Identity of the operator making the decision.
        reason:
            Free-text justification recorded in audit trail.

        Returns
        -------
        dict with ``status``, ``previous_state``, ``new_state``, and
        ``decision`` fields.
        """
        try:
            parsed_decision = OrphanReviewDecision(decision)
        except ValueError:
            valid = ", ".join(d.value for d in OrphanReviewDecision)
            raise ValueError(
                f"Invalid orphan review decision: {decision!r}. "
                f"Valid decisions: {valid}"
            )

        scoped = self._scope_key(tenant_id, broker_account_id, contract_key)

        def _mutate() -> dict[str, Any]:
            with self._lock:
                self._ensure_account_loaded(
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                )
                record = self._ownership_records.get(scoped)
                entry = self._entries.get(scoped)
                self._assert_mutation_authority(
                    scoped_key=scoped,
                    entry=entry,
                    actor=actor,
                    action="resolve_orphan_review",
                    request_id=request_id or f"resolve_orphan_review:{actor}:{decision}",
                )
                if record is None:
                    raise ValueError(
                        f"No ownership record found for contract "
                        f"{contract_key.as_log_text()} on account "
                        f"{broker_account_id}"
                    )
                if record.state != OwnershipState.ORPHAN_REVIEW:
                    raise ValueError(
                        f"Ownership record is in {record.state.value}, "
                        f"not ORPHAN_REVIEW. Cannot resolve."
                    )

                previous_state = record.state.value
                now = datetime.now(timezone.utc)

                if parsed_decision == OrphanReviewDecision.ADOPT:
                    # Transition to OWNED; assign the operator as the adopter
                    # so downstream routing can attribute the position.
                    record.transition_to(
                        OwnershipState.OWNED,
                        f"orphan_adopted: {reason}",
                        override_id=request_id or None,
                    )
                    record.owner_strategy_id = actor
                    record.acquired_at = now
                    record.last_evidence_at = now

                elif parsed_decision == OrphanReviewDecision.FLATTEN:
                    # Operator wants the position flattened.  If there is
                    # still net quantity we transition to NONE (the valid
                    # transition from ORPHAN_REVIEW) and clear ownership.
                    record.transition_to(
                        OwnershipState.NONE,
                        f"orphan_flatten: {reason}",
                        override_id=request_id or None,
                    )
                    record.owner_strategy_id = None
                    if entry is not None:
                        entry.net_by_strategy.clear()
                        entry.unknown_net_qty = 0
                        self._cleanup_entry(entry)
                        if self._entry_empty(entry):
                            self._entries.pop(scoped, None)
                    self._ownership_records.pop(scoped, None)
                    self._persist_contract_state(
                        tenant_id=tenant_id,
                        broker_account_id=broker_account_id,
                        contract_key=contract_key,
                        entry=None,
                    )

                elif parsed_decision == OrphanReviewDecision.SUPPRESS:
                    # Suppress: discard the record entirely.
                    record.transition_to(
                        OwnershipState.NONE,
                        f"orphan_suppressed: {reason}",
                        override_id=request_id or None,
                    )
                    record.owner_strategy_id = None
                    if entry is not None:
                        entry.net_by_strategy.clear()
                        entry.unknown_net_qty = 0
                        self._cleanup_entry(entry)
                        if self._entry_empty(entry):
                            self._entries.pop(scoped, None)
                    self._ownership_records.pop(scoped, None)
                    self._persist_contract_state(
                        tenant_id=tenant_id,
                        broker_account_id=broker_account_id,
                        contract_key=contract_key,
                        entry=None,
                    )

                elif parsed_decision == OrphanReviewDecision.CONTINUE_OBSERVING:
                    # Stay in ORPHAN_REVIEW, just bump the evidence timestamp.
                    record.last_evidence_at = now
                    record.state_reason = f"continue_observing: {reason}"
                    record.updated_at = now

                new_state = record.state.value

                emit_audit_event(
                    actor=actor,
                    action="resolve_orphan_review",
                    resource_type="ownership_record",
                    resource_id=record.ownership_key,
                    before={"state": previous_state},
                    after={
                        "state": new_state,
                        "decision": parsed_decision.value,
                        "reason": reason,
                        "tenant_id": str(tenant_id),
                        "broker_account_id": str(broker_account_id),
                        "contract_key": contract_key.as_log_text(),
                    },
                    request_id=request_id or None,
                )

                return {
                    "status": "resolved",
                    "decision": parsed_decision.value,
                    "previous_state": previous_state,
                    "new_state": new_state,
                    "ownership_key": record.ownership_key,
                }

        return self._execute_scoped_mutation(
            scoped_key=scoped,
            priority=MutationPriority.BREAK_GLASS,
            actor=actor,
            request_id=request_id or f"resolve_orphan_review:{actor}:{decision}",
            mutation_fn=_mutate,
        )

    @staticmethod
    def _extract_position_quantity(position: object) -> int:
        raw = (
            _position_field(position, "quantity")
            if _position_field(position, "quantity") is not None
            else _position_field(position, "netqty")
        )
        try:
            return int(raw or 0)
        except Exception:
            return 0

    def reconcile_broker_positions(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        positions: list[object],
    ) -> PositionOwnershipReconcileResult:
        with self._lock:
            self._ensure_account_loaded(
                tenant_id=tenant_id,
                broker_account_id=broker_account_id,
            )
            account_scope = self._account_scope(tenant_id, broker_account_id)
            broker_net_by_contract: dict[tuple[str, str, str, str, str], int] = {}
            parse_errors = 0
            for position in positions or []:
                qty = self._extract_position_quantity(position)
                if qty == 0:
                    continue
                contract_key, reason = derive_contract_key_from_position(position)
                if contract_key is None:
                    parse_errors += 1
                    logger.warning(
                        "PositionOwnershipStore reconcile: failed to derive contract key reason=%s symbol=%s",
                        reason,
                        _position_field(position, "symbol"),
                    )
                    continue
                storage_key = contract_key.as_storage_key()
                broker_net_by_contract[storage_key] = int(
                    broker_net_by_contract.get(storage_key, 0)
                ) + qty

            added_unknown = 0
            converted_to_unknown = 0
            removed_stale = 0

            scoped_contract_keys = [
                key for key in self._entries.keys()
                if (key[0], key[2]) == account_scope
            ]
            for scoped in scoped_contract_keys:
                contract_storage_key = scoped[3]
                entry = self._entries.get(scoped)
                if entry is None:
                    continue
                authority_path = self._assert_mutation_authority(
                    scoped_key=scoped,
                    entry=entry,
                    actor="account_runner",
                    action="reconcile_broker_positions",
                    request_id=(
                        f"reconcile_broker_positions:{str(tenant_id)}:{str(broker_account_id)}"
                    ),
                )
                broker_net = int(broker_net_by_contract.pop(contract_storage_key, 0))
                if broker_net == 0:
                    self._cleanup_entry(entry)
                    if entry.pending_by_strategy:
                        # Always retain entries that have pending submission locks.
                        entry.zero_broker_poll_count = 0
                        self._sync_ownership_record_from_entry(
                            scoped_key=scoped,
                            entry=entry,
                            reason="reconcile_pending_lock_retained",
                            authority_path=authority_path,
                        )
                        continue
                    # §69: Require corroboration — at least 2 consecutive zero-position
                    # broker polls — before destructive cleanup. A single false-empty
                    # snapshot (API glitch, rate-limit stale data) must not delete
                    # ownership or authoritative position state.
                    # Also protect records in RECONCILING state (marked during startup
                    # recovery) until broker evidence confirms the flat position.
                    ownership_rec = self._ownership_records.get(scoped)
                    in_reconciling = (
                        ownership_rec is not None
                        and getattr(ownership_rec, "state", None) == OwnershipState.RECONCILING
                    )
                    entry.zero_broker_poll_count = int(entry.zero_broker_poll_count or 0) + 1
                    if entry.zero_broker_poll_count < 2 or in_reconciling:
                        logger.info(
                            "PositionOwnershipStore reconcile: zero-position evidence for "
                            "%s count=%d/%s — deferring cleanup (corroboration required)",
                            contract_storage_key,
                            entry.zero_broker_poll_count,
                            "2 (RECONCILING — needs extra poll)" if in_reconciling else "2",
                        )
                        continue
                    if not self._entry_empty(entry):
                        removed_stale += 1
                    self._entries.pop(scoped, None)
                    self._persist_contract_state(
                        tenant_id=tenant_id,
                        broker_account_id=broker_account_id,
                        contract_key=ContractKey(*contract_storage_key),
                        entry=None,
                    )
                    self._sync_ownership_record_from_entry(
                        scoped_key=scoped,
                        entry=None,
                        reason="reconcile_removed_stale",
                    )
                    continue

                # Non-zero broker net: reset the corroboration counter.
                entry.zero_broker_poll_count = 0
                known_net = int(sum(int(v) for v in entry.net_by_strategy.values()))
                unknown_net = int(entry.unknown_net_qty or 0)
                if unknown_net == broker_net:
                    continue
                if known_net == broker_net and unknown_net == 0:
                    continue
                entry.net_by_strategy.clear()
                entry.unknown_net_qty = broker_net
                self._cleanup_entry(entry)
                self._persist_contract_state(
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    contract_key=ContractKey(*contract_storage_key),
                    entry=entry,
                )
                self._sync_ownership_record_from_entry(
                    scoped_key=scoped,
                    entry=entry,
                    reason="reconcile_converted_to_unknown",
                    authority_path=authority_path,
                )
                converted_to_unknown += 1

            for contract_storage_key, broker_net in broker_net_by_contract.items():
                if int(broker_net or 0) == 0:
                    continue
                scoped = (account_scope[0], account_scope[1], account_scope[1], contract_storage_key)
                entry = _OwnershipEntry(
                    unknown_net_qty=int(broker_net),
                    authority_path=self._authority_path,
                )
                self._entries[scoped] = entry
                authority_path = self._assert_mutation_authority(
                    scoped_key=scoped,
                    entry=entry,
                    actor="account_runner",
                    action="reconcile_broker_positions",
                    request_id=(
                        f"reconcile_broker_positions:{str(tenant_id)}:{str(broker_account_id)}"
                    ),
                )
                self._persist_contract_state(
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    contract_key=ContractKey(*contract_storage_key),
                    entry=entry,
                )
                self._sync_ownership_record_from_entry(
                    scoped_key=scoped,
                    entry=entry,
                    reason="reconcile_added_unknown",
                    authority_path=authority_path,
                )
                added_unknown += 1

            return PositionOwnershipReconcileResult(
                added_unknown=added_unknown,
                converted_to_unknown=converted_to_unknown,
                removed_stale=removed_stale,
                parse_errors=parse_errors,
            )


def build_position_ownership_store() -> PositionOwnershipStore:
    settings = get_settings()
    runtime_cfg = RuntimeConfig.from_env(dict(os.environ))
    mode_resolution = resolve_operating_mode_from_runtime(
        settings=settings,
        runtime_cfg=runtime_cfg,
    )
    operating_mode = mode_resolution.mode or OperatingMode.HUB_AUTHORITATIVE
    trade_mode = str(os.getenv("TRADE_MODE", "PAPER") or "PAPER").strip().upper()
    is_live = trade_mode == "LIVE"

    persist_enabled = _is_true(os.getenv("POSITION_OWNERSHIP_PERSIST", "true"))
    persist_required = _is_true(
        os.getenv(
            "POSITION_OWNERSHIP_PERSIST_REQUIRED",
            "true" if is_live else "false",
        )
    )

    # In LIVE, in-memory fallbacks are forbidden -- treat as persist_required.
    if is_live:
        persist_required = True

    if not persist_enabled:
        if persist_required:
            raise RuntimeError(
                "POSITION_OWNERSHIP_PERSIST_REQUIRED=true requires POSITION_OWNERSHIP_PERSIST=true"
            )
        return PositionOwnershipStore(operating_mode=operating_mode)

    backend = str(
        os.getenv(
            "POSITION_OWNERSHIP_PERSIST_BACKEND",
            settings.control_plane_backend,
        )
        or ""
    ).strip().lower()
    if backend in {"postgres", "pg", "db", "database"}:
        table_name = os.getenv(
            "POSITION_OWNERSHIP_PERSIST_TABLE",
            "position_ownership_ledger",
        )
        try:
            dsn = get_control_plane_dsn(settings)
            return PositionOwnershipStore(
                backend=PostgresPositionOwnershipBackend(
                    dsn=dsn,
                    table_name=table_name,
                    persist_pending_locks=bool(
                        getattr(settings, "ownership_persist_pending_locks", False)
                    ),
                ),
                operating_mode=operating_mode,
            )
        except Exception as exc:
            if persist_required:
                raise RuntimeError(
                    f"Position ownership persistence initialization failed (backend=postgres): {exc}"
                ) from exc
            logger.warning(
                "Position ownership persistence fallback to in-memory (postgres init failed): %s",
                exc,
            )
            return PositionOwnershipStore(operating_mode=operating_mode)
    if persist_required:
        raise RuntimeError(
            f"Position ownership persistence backend unsupported: {backend or '(empty)'}"
        )
    return PositionOwnershipStore(operating_mode=operating_mode)


__all__ = [
    "ContractKey",
    "OwnershipKey",
    "PositionOwnershipDecision",
    "PositionOwnershipReconcileResult",
    "PositionOwnershipStore",
    "OwnershipAuthorityViolation",
    "UNKNOWN_OWNER",
    "UNKNOWN_STRATEGY_ID",
    "UNKNOWN_MODE_BLOCK_ENTRIES",
    "UNKNOWN_MODE_ALLOW_ENTRIES",
    "OwnershipPersistenceBackend",
    "PostgresPositionOwnershipBackend",
    "derive_contract_key",
    "derive_contract_key_from_position",
    "normalize_contract_key",
    "derive_ownership_key",
    "render_contract",
    "build_position_ownership_store",
]
