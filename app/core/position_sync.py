"""Synchronize broker positions with internal risk tracking and report changes.

This module maps broker tokens to instrument labels, reconciles quantities/sides,
and records adds, updates, and closes for visibility.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, Optional, Tuple

from app.config.settings import get_settings
from app.core.anti_pattern_guards import assert_not_single_poll_mutation
from app.core.dashboard_bus import dashboard_bus
from app.core.position_flat_registry import position_flat_registry
from app.core.degraded_scope_manager import degraded_scope_manager, DegradedReason
from app.core.lot_size import lot_size_for_symbol_optional
from app.core.logging_utils import log_event
from app.core.position_state import InternalPosition, PositionState
from app.orders.scope_serializer import (
    MutationPriority,
    ScopedMutation,
    scope_serializer,
)

try:
    from app.core.order_client import AngelHTTPBlockedError
except Exception:  # pragma: no cover - defensive fallback
    class AngelHTTPBlockedError(Exception):  # type: ignore[no-redef]
        pass

logger = logging.getLogger(__name__)

_LAST_GOOD_POSITIONS_BY_ACCOUNT: Dict[str, Dict[str, Any]] = {}
_LAST_GOOD_POSITIONS_LOCK = threading.RLock()
_POSITION_SYNC_EVIDENCE_BY_ACCOUNT: Dict[str, Dict[str, Dict[str, int]]] = {}
_POSITION_SYNC_EVIDENCE_LOCK = threading.RLock()


class PositionSyncEvidenceClass(str, Enum):
    WEAK = "WEAK"
    MEDIUM = "MEDIUM"
    STRONG = "STRONG"


class PositionSyncReconciliationState(str, Enum):
    IN_SYNC = "IN_SYNC"
    RECONCILING = "RECONCILING"
    DEGRADED = "DEGRADED"
    ORPHAN_REVIEW = "ORPHAN_REVIEW"


_RECON_STATE_SEVERITY = {
    PositionSyncReconciliationState.IN_SYNC: 0,
    PositionSyncReconciliationState.RECONCILING: 1,
    PositionSyncReconciliationState.DEGRADED: 2,
    PositionSyncReconciliationState.ORPHAN_REVIEW: 3,
}
_EVIDENCE_SEVERITY = {
    PositionSyncEvidenceClass.WEAK: 0,
    PositionSyncEvidenceClass.MEDIUM: 1,
    PositionSyncEvidenceClass.STRONG: 2,
}


def _stale_close_guard_enabled() -> bool:
    try:
        return bool(getattr(get_settings(), "position_sync_stale_close_guard", False))
    except Exception:
        return False


def _coerce_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _pick_nonzero_price(*values: Any) -> Optional[float]:
    for value in values:
        price = _coerce_float(value)
        if price is None or price == 0.0:
            continue
        return price
    return None


def _extract_broker_avg_price(pos: Dict[str, Any], qty: int) -> Optional[float]:
    avg_price = _pick_nonzero_price(
        pos.get("avgprice"),
        pos.get("avgPrice"),
        pos.get("average_price"),
        pos.get("averageprice"),
        pos.get("netprice"),
        pos.get("netPrice"),
        pos.get("netavgprice"),
        pos.get("netAvgPrice"),
    )
    if avg_price is not None:
        return avg_price
    if qty > 0:
        return _pick_nonzero_price(
            pos.get("buyavgprice"),
            pos.get("buyAvgPrice"),
            pos.get("buyaverageprice"),
            pos.get("buyAveragePrice"),
        )
    if qty < 0:
        return _pick_nonzero_price(
            pos.get("sellavgprice"),
            pos.get("sellAvgPrice"),
            pos.get("sellaverageprice"),
            pos.get("sellAveragePrice"),
        )
    return None


def _normalize_identity_token(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _normalize_identity_symbol(value: Any) -> Optional[str]:
    text = str(value or "").strip().upper()
    return text or None


def _position_identity_keys(*, token: Any = None, symbol: Any = None) -> set[str]:
    keys: set[str] = set()
    normalized_token = _normalize_identity_token(token)
    normalized_symbol = _normalize_identity_symbol(symbol)
    if normalized_token:
        keys.add(f"token:{normalized_token}")
    if normalized_symbol:
        keys.add(f"symbol:{normalized_symbol}")
    return keys


def _snapshot_ttl_seconds() -> float:
    raw = os.getenv("POSITION_SYNC_LAST_GOOD_TTL_SECONDS", "180")
    try:
        return max(1.0, float(raw))
    except (TypeError, ValueError):
        return 180.0


def _default_retry_after_seconds() -> float:
    raw = os.getenv("POSITION_SYNC_RATE_LIMIT_RETRY_AFTER_SECONDS", "15")
    try:
        return max(1.0, float(raw))
    except (TypeError, ValueError):
        return 15.0


def _reconciliation_confirm_polls() -> int:
    raw = os.getenv("POSITION_SYNC_RECON_CONFIRM_POLLS", "2")
    try:
        return max(2, int(raw))
    except (TypeError, ValueError):
        return 2


def _orphan_review_threshold_polls() -> int:
    raw = os.getenv("POSITION_SYNC_ORPHAN_REVIEW_POLLS", "3")
    try:
        return max(_reconciliation_confirm_polls(), int(raw))
    except (TypeError, ValueError):
        return 3


def _log_noop_summary_at_info() -> bool:
    raw = str(os.getenv("POSITION_SYNC_LOG_NOOP_SUMMARY", "false")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _scope_mutation_key(account_key: str, label: str) -> str:
    return f"legacy_position_sync:{str(account_key)}:{str(label)}"


def _execute_reconciliation_mutation(
    *,
    account_key: str,
    label: str,
    actor: str,
    request_id: str,
    mutation_fn: Callable[[], Any],
) -> Any:
    return scope_serializer.execute(
        ScopedMutation(
            ownership_key=_scope_mutation_key(account_key, label),
            priority=MutationPriority.RECONCILIATION,
            mutation_fn=mutation_fn,
            request_id=request_id,
            actor=actor,
        )
    )


def _evidence_state_for_scope(account_key: str, scope_key: str) -> Dict[str, int]:
    with _POSITION_SYNC_EVIDENCE_LOCK:
        account_state = _POSITION_SYNC_EVIDENCE_BY_ACCOUNT.setdefault(account_key, {})
        return account_state.setdefault(
            scope_key,
            {
                "broker_present_only_polls": 0,
                "local_missing_polls": 0,
                "ambiguous_polls": 0,
            },
        )


def _mark_scope_event(account_key: str, scope_key: str, *, event: str) -> int:
    with _POSITION_SYNC_EVIDENCE_LOCK:
        state = _evidence_state_for_scope(account_key, scope_key)
        if event == "broker_present_only":
            state["broker_present_only_polls"] = int(
                state.get("broker_present_only_polls", 0)
            ) + 1
            state["local_missing_polls"] = 0
        elif event == "local_missing":
            state["local_missing_polls"] = int(state.get("local_missing_polls", 0)) + 1
            state["broker_present_only_polls"] = 0
        elif event == "ambiguous":
            state["ambiguous_polls"] = int(state.get("ambiguous_polls", 0)) + 1
        return int(
            state.get(
                {
                    "broker_present_only": "broker_present_only_polls",
                    "local_missing": "local_missing_polls",
                    "ambiguous": "ambiguous_polls",
                }[event],
                0,
            )
        )


def _clear_scope_evidence(account_key: str, scope_key: str) -> None:
    with _POSITION_SYNC_EVIDENCE_LOCK:
        account_state = _POSITION_SYNC_EVIDENCE_BY_ACCOUNT.get(account_key)
        if not account_state:
            return
        account_state.pop(scope_key, None)
        if not account_state:
            _POSITION_SYNC_EVIDENCE_BY_ACCOUNT.pop(account_key, None)


def _position_sync_account_key(
    order_client: Any,
    *,
    broker_account_id: Optional[str] = None,
) -> str:
    explicit_broker_account_id = str(broker_account_id or "").strip()
    if explicit_broker_account_id:
        return explicit_broker_account_id
    for attr in (
        "broker_account_id",
        "account_id",
        "client_code",
        "clientCode",
        "client_id",
        "account",
    ):
        try:
            value = getattr(order_client, attr, None)
        except Exception:
            value = None
        if value is not None and str(value).strip():
            return str(value).strip()
    for method_name in ("get_broker_account_id", "get_account_id"):
        method = getattr(order_client, method_name, None)
        if callable(method):
            try:
                value = method()
            except Exception:
                value = None
            if value is not None and str(value).strip():
                return str(value).strip()
    return "legacy-default"


def _lookup_entry_submission_context(
    *,
    entry_submission_lookup: Optional[Callable[..., Optional[Dict[str, Any]]]],
    broker_account_id: str,
    label: str,
    symbol: str,
    side: str,
) -> Optional[Dict[str, Any]]:
    if not callable(entry_submission_lookup):
        return None
    try:
        hint = entry_submission_lookup(
            broker_account_id=broker_account_id,
            label=label,
            symbol=symbol,
            side=side,
        )
    except Exception as exc:
        logger.debug(
            "[POS_SYNC] Entry submission lookup failed for %s: %s",
            label,
            exc,
        )
        return None
    return hint if isinstance(hint, dict) and hint else None


def _synthetic_position_label(*, tradingsymbol: str, token: str) -> Optional[str]:
    symbol = str(tradingsymbol or "").strip().upper()
    if symbol:
        return symbol
    tok = str(token or "").strip()
    if tok:
        return f"BROKER_POS_{tok}"
    return None


def _reconcile_pending_close_for_label(
    *,
    risk_manager: Any,
    label: str,
) -> Tuple[bool, bool]:
    reconcile = getattr(risk_manager, "reconcile_pending_closing_orders_for_label", None)
    if not callable(reconcile):
        return False, False
    try:
        outcome = reconcile(label)
    except Exception as exc:
        logger.debug(
            "[POS_SYNC] Pending close reconcile failed for %s: %s",
            label,
            exc,
        )
        return False, False
    if not isinstance(outcome, dict):
        return False, False
    return bool(outcome.get("closed")), bool(outcome.get("pending"))


def _upsert_broker_position_meta(
    *,
    instrument_meta: Dict[str, Dict[str, Any]],
    risk_manager: Any,
    label: str,
    tradingsymbol: str,
    token: str,
    exchange: Optional[str],
    lot_size: float,
    underlying: Optional[str],
) -> None:
    meta_update = {
        "symbol": tradingsymbol or label,
        "token": token or None,
        "symboltoken": token or None,
        "exchange": exchange or "NSE",
        "lot_size": float(lot_size or 1),
    }
    if underlying:
        meta_update["underlying"] = underlying
    if label:
        instrument_meta[label] = {
            **dict(instrument_meta.get(label, {})),
            **meta_update,
        }
        try:
            dashboard_bus.upsert_instrument_meta(label, instrument_meta[label])
        except Exception:
            pass
        try:
            risk_meta = getattr(risk_manager, "instrument_meta", None)
            if isinstance(risk_meta, dict):
                risk_meta[label] = {
                    **dict(risk_meta.get(label, {})),
                    **meta_update,
                }
        except Exception:
            pass


def _store_last_good_positions_snapshot(
    account_key: str,
    broker_positions: list[Dict[str, Any]],
) -> None:
    snapshot = [dict(pos) for pos in broker_positions if isinstance(pos, dict)]
    with _LAST_GOOD_POSITIONS_LOCK:
        _LAST_GOOD_POSITIONS_BY_ACCOUNT[str(account_key)] = {
            "positions": snapshot,
            "ts_mono": time.monotonic(),
        }


def _load_last_good_positions_snapshot(
    account_key: str,
) -> Tuple[Optional[list[Dict[str, Any]]], Optional[float]]:
    key = str(account_key)
    with _LAST_GOOD_POSITIONS_LOCK:
        snapshot = _LAST_GOOD_POSITIONS_BY_ACCOUNT.get(key)
        if not snapshot:
            return None, None
        ts_mono = float(snapshot.get("ts_mono") or 0.0)
        age_seconds = max(0.0, time.monotonic() - ts_mono)
        if age_seconds > _snapshot_ttl_seconds():
            _LAST_GOOD_POSITIONS_BY_ACCOUNT.pop(key, None)
            return None, age_seconds
        positions = snapshot.get("positions") or []
        return [dict(pos) for pos in positions if isinstance(pos, dict)], age_seconds


def _active_snapshot_position_count(
    positions: Optional[list[Dict[str, Any]]],
) -> int:
    count = 0
    for pos in positions or []:
        if not isinstance(pos, dict):
            continue
        try:
            qty = int(pos.get("netqty") or pos.get("net") or 0)
        except Exception:
            qty = 0
        if qty != 0:
            count += 1
    return count


def _snapshot_payload_marked_stale(raw_payload: Any) -> bool:
    if not isinstance(raw_payload, dict):
        return False
    for key in ("stale", "is_stale", "snapshot_stale"):
        if raw_payload.get(key) is True:
            return True
    snapshot_age = _coerce_float(
        raw_payload.get("age_seconds") or raw_payload.get("snapshot_age_seconds")
    )
    return bool(
        snapshot_age is not None and snapshot_age > _snapshot_ttl_seconds()
    )


def _classify_positions_fetch_error(
    exc: Exception,
) -> Tuple[bool, str, Optional[float]]:
    status = getattr(exc, "status", None)
    retry_after = getattr(exc, "retry_after_seconds", None)
    message = str(exc).lower()
    rate_limited = (
        status == 403
        or "rate limit exceeded" in message
        or "exceeding access rate" in message
        or "status=403" in message
    )
    blocked = isinstance(exc, AngelHTTPBlockedError) or rate_limited
    reason = "rate_limited" if rate_limited else ("blocked" if blocked else "error")
    if reason == "rate_limited" and not retry_after:
        retry_after = _default_retry_after_seconds()
    return blocked, reason, retry_after


@dataclass
class PositionSyncResult:
    added: int = 0
    closed: int = 0
    updated: int = 0
    errors: list[str] = field(default_factory=list)
    blocked: bool = False
    blocked_reason: Optional[str] = None
    retry_after_seconds: Optional[float] = None
    used_cached_snapshot: bool = False
    cached_snapshot_age_seconds: Optional[float] = None
    reconciliation_state: str = PositionSyncReconciliationState.IN_SYNC.value
    evidence_class: Optional[str] = None
    position_state: Optional[str] = None
    reasons: list[str] = field(default_factory=list)

    def update_reconciliation_state(
        self,
        *,
        state: PositionSyncReconciliationState,
        evidence: PositionSyncEvidenceClass,
        reason: str,
    ) -> None:
        current_state = PositionSyncReconciliationState(str(self.reconciliation_state))
        current_evidence = (
            PositionSyncEvidenceClass(str(self.evidence_class))
            if self.evidence_class
            else None
        )
        if _RECON_STATE_SEVERITY[state] >= _RECON_STATE_SEVERITY[current_state]:
            self.reconciliation_state = state.value
        if current_evidence is None or _EVIDENCE_SEVERITY[evidence] >= _EVIDENCE_SEVERITY[current_evidence]:
            self.evidence_class = evidence.value
        reason_text = str(reason or "").strip()
        if reason_text and reason_text not in self.reasons:
            self.reasons.append(reason_text)

    # Return a short human-readable summary for logs and monitoring.
    def summary(self) -> str:
        return (
            f"added={self.added} closed={self.closed} updated={self.updated} "
            f"errors={len(self.errors)} reconciliation_state={self.reconciliation_state} "
            f"evidence={self.evidence_class or 'NONE'} "
            f"position_state={self.position_state or 'NONE'}"
        )


# Build lookup maps from broker tokens/symbols to internal instrument labels.
def _token_maps(instrument_meta: Dict[str, Dict[str, Any]]) -> Tuple[Dict[str, str], Dict[str, str]]:
    token_to_label: Dict[str, str] = {}
    symbol_to_label: Dict[str, str] = {}
    for label, meta in instrument_meta.items():
        token = _normalize_identity_token(
            meta.get("token") or meta.get("symboltoken") or meta.get("instrument_token")
        )
        if token:
            token_to_label[str(token)] = label
        symbol = _normalize_identity_symbol(meta.get("symbol"))
        if symbol:
            symbol_to_label[str(symbol)] = label
    return token_to_label, symbol_to_label


def _augment_maps_from_risk_positions(
    *,
    risk_positions: Dict[str, Dict[str, Any]],
    token_to_label: Dict[str, str],
    symbol_to_label: Dict[str, str],
) -> Dict[str, set[str]]:
    identity_by_label: Dict[str, set[str]] = {}
    for label, entry in (risk_positions or {}).items():
        if not isinstance(entry, dict):
            continue
        token = _normalize_identity_token(
            entry.get("symboltoken")
            or entry.get("token")
            or entry.get("instrument_token")
        )
        symbol = _normalize_identity_symbol(
            entry.get("tradingsymbol") or entry.get("symbol")
        )
        identity_by_label[str(label)] = _position_identity_keys(
            token=token,
            symbol=symbol,
        )
        if token:
            token_to_label[str(token)] = str(label)
        if symbol:
            symbol_to_label[str(symbol)] = str(label)
    return identity_by_label


# Pull live broker positions and reconcile them with risk manager state.
def sync_positions_with_broker(
    *,
    order_client: Any,
    risk_manager: Any,
    instrument_meta: Dict[str, Dict[str, Any]],
    ltp_lookup: Optional[Dict[str, float]] = None,
    broker_account_id: Optional[str] = None,
    entry_submission_lookup: Optional[Callable[..., Optional[Dict[str, Any]]]] = None,
    strategy_flat_callback: Optional[Callable[..., None]] = None,
) -> PositionSyncResult:
    """
    Fetch live positions from broker and reconcile with risk_manager.open_positions.

    - Adds any broker positions missing in risk_manager (BROKER_SYNC template).
    - Closes positions that risk_manager tracks but broker does not.
    - Updates qty/side for mismatches.
    """
    result = PositionSyncResult()
    used_cached_snapshot = False
    account_key = _position_sync_account_key(
        order_client,
        broker_account_id=broker_account_id,
    )
    previous_healthy_positions, _ = _load_last_good_positions_snapshot(account_key)
    try:
        raw = order_client.get_positions()
    except Exception as exc:
        blocked, reason, retry_after = _classify_positions_fetch_error(exc)
        result.blocked = blocked
        result.blocked_reason = reason
        result.retry_after_seconds = retry_after
        result.errors.append(str(exc))

        cached_positions, cached_age_seconds = _load_last_good_positions_snapshot(
            account_key
        )
        if cached_positions is None:
            logger.warning("[POS_SYNC] Failed to fetch broker positions: %s", exc)
            result.update_reconciliation_state(
                state=PositionSyncReconciliationState.DEGRADED
                if blocked
                else PositionSyncReconciliationState.RECONCILING,
                evidence=PositionSyncEvidenceClass.WEAK,
                reason=f"broker_positions_fetch_failed:{reason}",
            )
            return result

        used_cached_snapshot = True
        result.used_cached_snapshot = True
        result.cached_snapshot_age_seconds = cached_age_seconds
        raw = {"data": cached_positions}
        logger.warning(
            "[POS_SYNC] Failed to fetch broker positions (%s); using cached snapshot age=%.1fs",
            reason,
            float(cached_age_seconds or 0.0),
        )
        result.update_reconciliation_state(
            state=PositionSyncReconciliationState.RECONCILING,
            evidence=PositionSyncEvidenceClass.MEDIUM,
            reason=f"using_cached_snapshot:{reason}",
        )

    if isinstance(raw, dict) and "data" in raw:
        broker_positions = raw.get("data") or []
    else:
        broker_positions = raw or []
    broker_positions = [
        pos for pos in broker_positions if isinstance(pos, dict)
    ]
    if not used_cached_snapshot:
        _store_last_good_positions_snapshot(account_key, broker_positions)

    state_lock = getattr(risk_manager, "_state_lock", None)
    def _lock_ctx():
        return state_lock if state_lock is not None else nullcontext()

    with _lock_ctx():
        risk_positions = dict(risk_manager.open_positions)
    token_to_label, symbol_to_label = _token_maps(instrument_meta)
    risk_identity_by_label = _augment_maps_from_risk_positions(
        risk_positions=risk_positions,
        token_to_label=token_to_label,
        symbol_to_label=symbol_to_label,
    )

    logger.debug(
        "[POS_SYNC] Starting sync: broker_positions=%d, instruments=%d",
        len(broker_positions),
        len(instrument_meta)
    )

    active_broker_positions: Dict[str, Dict[str, Any]] = {}
    active_broker_identity_keys: set[str] = set()
    for pos in broker_positions:
        try:
            qty = int(pos.get("netqty") or pos.get("net") or 0)
        except Exception:
            qty = 0
        if qty == 0:
            continue
        token = _normalize_identity_token(pos.get("symboltoken") or pos.get("token")) or ""
        tradingsymbol = str(pos.get("tradingsymbol") or "")
        tradingsymbol_key = _normalize_identity_symbol(tradingsymbol) or ""
        label = token_to_label.get(token) or symbol_to_label.get(tradingsymbol_key)
        if not label:
            label = _synthetic_position_label(
                tradingsymbol=tradingsymbol,
                token=token,
            )
            if not label:
                result.errors.append(
                    f"unmapped position token={token} symbol={tradingsymbol}"
                )
                continue
        meta = instrument_meta.get(label, {})
        existing_entry = risk_positions.get(label, {})
        lot_size = float(
            existing_entry.get("lot_size")
            or meta.get("lot_size")
            or lot_size_for_symbol_optional(tradingsymbol)
            or 1
        )
        underlying = (
            existing_entry.get("underlying")
            or meta.get("underlying")
            or None
        )
        _upsert_broker_position_meta(
            instrument_meta=instrument_meta,
            risk_manager=risk_manager,
            label=label,
            tradingsymbol=tradingsymbol,
            token=token,
            exchange=pos.get("exchange") or existing_entry.get("exchange"),
            lot_size=lot_size,
            underlying=underlying,
        )
        qty_lots = abs(qty) / lot_size if lot_size else abs(qty)
        avg_price = _extract_broker_avg_price(pos, qty)
        ltp = _coerce_float(pos.get("ltp"))
        identity_keys = _position_identity_keys(token=token, symbol=tradingsymbol)
        active_broker_identity_keys.update(identity_keys)
        active_broker_positions[label] = {
            "qty": max(1, int(qty_lots)) if qty_lots else 0,
            "side": "BUY" if qty > 0 else "SELL",
            "avg_price": avg_price,
            "ltp": float(ltp or 0.0),
            "tradingsymbol": tradingsymbol or existing_entry.get("tradingsymbol") or label,
            "exchange": pos.get("exchange") or existing_entry.get("exchange") or meta.get("exchange", "NSE"),
            "symboltoken": token or existing_entry.get("symboltoken") or instrument_meta.get(label, {}).get("token"),
            "lot_size": lot_size,
            "raw_avgprice": pos.get("avgprice"),
            "raw_avgPrice": pos.get("avgPrice"),
            "raw_averageprice": pos.get("averageprice"),
            "raw_netprice": pos.get("netprice"),
            "raw_netavgprice": pos.get("netavgprice"),
            "raw_buyavgprice": pos.get("buyavgprice"),
            "raw_sellavgprice": pos.get("sellavgprice"),
            "raw_lp": pos.get("lp"),
            "raw_ltp": pos.get("ltp"),
            "identity_keys": identity_keys,
        }
        if ltp is not None and ltp > 0:
            try:
                dashboard_bus.record_tick(
                    label,
                    float(ltp),
                    datetime.now(timezone.utc),
                )
            except Exception:
                pass
    risk_labels = set(risk_positions.keys())
    broker_labels = set(active_broker_positions.keys())

    # Add or update positions present at broker
    for label, bpos in active_broker_positions.items():
        if label in risk_positions:
            _clear_scope_evidence(account_key, str(label))
        submission_hint = _lookup_entry_submission_context(
            entry_submission_lookup=entry_submission_lookup,
            broker_account_id=account_key,
            label=label,
            symbol=str(bpos.get("tradingsymbol") or label),
            side=str(bpos.get("side") or ""),
        )
        submission_strategy_name = None
        submission_strategy_context = None
        submission_template_name = None
        if submission_hint:
            strategy_name_raw = submission_hint.get("strategy_name")
            if strategy_name_raw is not None and str(strategy_name_raw).strip():
                submission_strategy_name = str(strategy_name_raw).strip()
            strategy_context_raw = submission_hint.get("strategy_context")
            if isinstance(strategy_context_raw, dict) and strategy_context_raw:
                submission_strategy_context = dict(strategy_context_raw)
            if submission_strategy_name:
                submission_template_name = submission_strategy_name
        # §RC1-FIX: When the ATM-refresh/universe builder relabels an instrument after
        # the strategy submitted its entry order, position_sync resolves the broker
        # position to the new label (e.g. OTM→ATM).  The submission hint still carries
        # the original order label.  Prefer the original label so we update the existing
        # risk-manager entry rather than creating a duplicate BROKER_SYNC entry under
        # the reclassified label.
        if submission_hint and label not in risk_positions:
            hint_label = str(submission_hint.get("position_label") or "").strip()
            if hint_label and hint_label != label:
                logger.info(
                    "[POS_SYNC] Universe-relabeled broker position resolved as %s but "
                    "submission hint points to %s; remapping to submission label",
                    label,
                    hint_label,
                )
                label = hint_label
        if label not in risk_positions:
            suppress_sync_entry = False
            suppress_sync_reason = ""
            suppressor = getattr(risk_manager, "should_suppress_broker_sync_entry", None)
            if callable(suppressor):
                try:
                    suppress_sync_entry = bool(suppressor(label))
                except Exception:
                    suppress_sync_entry = False
            kill_switch_active = bool(getattr(risk_manager, "kill_switch_activated", False))
            if not suppress_sync_entry and kill_switch_active:
                suppress_sync_entry = True
            if suppress_sync_entry:
                suppress_sync_reason = (
                    "kill_switch_active"
                    if kill_switch_active
                    else "forced_exit_in_flight"
                )
                logger.warning(
                    "[POS_SYNC] Suppressing BROKER_SYNC registration for %s reason=%s",
                    label,
                    suppress_sync_reason,
                )
                log_event(
                    logger,
                    event_type="BROKER_SYNC_SUPPRESSED",
                    message="Suppressed broker-synced position registration.",
                    broker_account_id=account_key,
                    instrument=label,
                    level=logging.WARNING,
                    reason=suppress_sync_reason,
                )
                continue
            present_only_polls = _mark_scope_event(
                account_key,
                str(label),
                event="broker_present_only",
            )
            if not submission_hint and present_only_polls < _reconciliation_confirm_polls():
                result.update_reconciliation_state(
                    state=PositionSyncReconciliationState.RECONCILING,
                    evidence=PositionSyncEvidenceClass.MEDIUM,
                    reason=f"broker_only_position_requires_confirm:{label}",
                )
                logger.info(
                    "[POS_SYNC] Deferring broker-only position registration for %s until confirming evidence arrives polls=%d/%d",
                    label,
                    present_only_polls,
                    _reconciliation_confirm_polls(),
                )
                continue
            # §19.3 anti-pattern guard: block single-poll position add
            # When a submission_hint exists, the entry was intentionally submitted
            # and the poll count gate does not apply (it's not a ghost position).
            if not submission_hint:
                assert_not_single_poll_mutation(
                    consecutive_polls=present_only_polls,
                    required_polls=_reconciliation_confirm_polls(),
                    action="broker_only_position_add",
                    scope_key=f"{account_key}:{label}",
                )
            try:
                entry_price = bpos.get("avg_price")
                if entry_price is None:
                    entry_price = bpos.get("ltp")

                def _register_broker_only_position() -> None:
                    risk_manager._register_entry(  # type: ignore[attr-defined]
                        label=label,
                        side=bpos["side"],
                        qty=bpos["qty"],
                        price=float(entry_price or 0.0),
                        template_name=submission_template_name or "BROKER_SYNC",
                        role=None,
                        underlying=risk_manager._infer_underlying(label),  # type: ignore[attr-defined]
                        strategy_name=submission_strategy_name or "BROKER_SYNC",
                        # §RC2-FIX: When we have a submission hint the order was placed
                        # by a known strategy — use FILL_SYNCED so the position is NOT
                        # marked recovery-owned and bracket/exit logic is not skipped.
                        reason="FILL_SYNCED" if submission_hint else "BROKER_SYNC",
                        ml_info=None,
                        strategy_context=submission_strategy_context,
                    )
                    if hasattr(risk_manager, "update_position_from_broker"):
                        risk_manager.update_position_from_broker(  # type: ignore[attr-defined]
                            label=label,
                            side=bpos["side"],
                            qty=bpos["qty"],
                            entry_price=float(entry_price or 0.0),
                            exchange=bpos.get("exchange"),
                            symboltoken=bpos.get("symboltoken"),
                            tradingsymbol=bpos.get("tradingsymbol"),
                            template_name=submission_template_name or "BROKER_SYNC",
                            strategy_name=submission_strategy_name or "BROKER_SYNC",
                            strategy_context=submission_strategy_context,
                        )

                _execute_reconciliation_mutation(
                    account_key=account_key,
                    label=str(label),
                    actor="position_sync",
                    request_id=f"register:{label}:{present_only_polls}",
                    mutation_fn=_register_broker_only_position,
                )
                result.added += 1
                result.position_state = PositionState.ADOPTED.value
                _clear_scope_evidence(account_key, str(label))
            except Exception as exc:
                logger.warning("[POS_SYNC] Failed to register broker position %s: %s", label, exc)
                result.errors.append(str(exc))
            continue

        # Existing position: check for mismatches
        existing = risk_positions[label]
        existing_side = str(existing.get("side", "")).upper()
        existing_qty = int(existing.get("qty", 0) or 0)
        target_qty = int(bpos["qty"] or 0)
        try:
            target_qty = int(risk_manager._normalize_qty_to_units(label, target_qty))  # type: ignore[attr-defined]
        except Exception:
            pass
        existing_entry = float(existing.get("entry_price", 0.0) or 0.0)
        broker_avg = bpos.get("avg_price")
        state_changed = (
            existing_side != bpos["side"]
            or existing_qty != target_qty
            or (broker_avg is not None and abs(existing_entry - float(broker_avg)) > 0.0001)
        )
        existing_strategy_name = str(existing.get("strategy_name", "") or "").strip()
        existing_strategy_context = existing.get("strategy_context")
        existing_template_name = str(existing.get("template_name", "") or "").strip()
        needs_enrichment = bool(submission_hint) and (
            (
                submission_template_name
                and existing_template_name in {"", "BROKER_SYNC"}
                and submission_template_name != existing_template_name
            )
            or (
                submission_strategy_name
                and existing_strategy_name in {"", "BROKER_SYNC"}
                and submission_strategy_name != existing_strategy_name
            )
            or (
                submission_strategy_context is not None
                and existing_strategy_context != submission_strategy_context
            )
        )
        if state_changed or needs_enrichment:
            target_entry = existing_entry if broker_avg is None else float(broker_avg)
            if state_changed:
                logger.info(
                    "[POS_SYNC] Entry mismatch %s | existing=%.2f broker_avg=%s raw_avg=%s raw_avgPrice=%s raw_averageprice=%s raw_netprice=%s raw_netavgprice=%s raw_buyavgprice=%s raw_sellavgprice=%s raw_lp=%s raw_ltp=%s",
                    label,
                    existing_entry,
                    (
                        f"{float(broker_avg):.2f}"
                        if broker_avg is not None
                        else "missing"
                    ),
                    bpos.get("raw_avgprice"),
                    bpos.get("raw_avgPrice"),
                    bpos.get("raw_averageprice"),
                    bpos.get("raw_netprice"),
                    bpos.get("raw_netavgprice"),
                    bpos.get("raw_buyavgprice"),
                    bpos.get("raw_sellavgprice"),
                    bpos.get("raw_lp"),
                    bpos.get("raw_ltp"),
                )
            try:
                def _update_existing_position() -> None:
                    if hasattr(risk_manager, "update_position_from_broker"):
                        risk_manager.update_position_from_broker(  # type: ignore[attr-defined]
                            label=label,
                            side=bpos["side"],
                            qty=target_qty,
                            entry_price=target_entry,
                            exchange=bpos.get("exchange"),
                            symboltoken=bpos.get("symboltoken"),
                            tradingsymbol=bpos.get("tradingsymbol"),
                            template_name=submission_template_name if needs_enrichment else None,
                            strategy_name=submission_strategy_name if needs_enrichment else None,
                            strategy_context=(
                                submission_strategy_context if needs_enrichment else None
                            ),
                        )
                    else:
                        with _lock_ctx():
                            risk_manager.open_positions[label]["side"] = bpos["side"]
                            risk_manager.open_positions[label]["qty"] = target_qty
                            risk_manager.open_positions[label]["entry_price"] = target_entry
                            risk_manager.open_positions[label]["exchange"] = bpos.get("exchange")
                            risk_manager.open_positions[label]["symboltoken"] = bpos.get("symboltoken")
                            risk_manager.open_positions[label]["tradingsymbol"] = bpos.get("tradingsymbol")
                            if bpos.get("lot_size") not in (None, 0, 0.0):
                                risk_manager.open_positions[label]["lot_size"] = bpos.get("lot_size")
                            if needs_enrichment:
                                if submission_template_name:
                                    risk_manager.open_positions[label]["template_name"] = submission_template_name
                                if submission_strategy_name:
                                    risk_manager.open_positions[label]["strategy_name"] = submission_strategy_name
                                if submission_strategy_context is not None:
                                    risk_manager.open_positions[label]["strategy_context"] = dict(submission_strategy_context)
                        try:
                            risk_manager._persist_state()  # type: ignore[attr-defined]
                        except Exception:
                            pass

                _execute_reconciliation_mutation(
                    account_key=account_key,
                    label=str(label),
                    actor="position_sync",
                    request_id=f"update:{label}",
                    mutation_fn=_update_existing_position,
                )
                result.updated += 1
                result.position_state = PositionState.RECONCILING.value
            except Exception as exc:
                logger.warning("[POS_SYNC] Failed to update position %s: %s", label, exc)
                result.errors.append(str(exc))

    # Close stale positions that broker no longer shows.
    # When running from cached data, stale-close can generate false exits, so skip it.
    stale_labels = risk_labels - broker_labels
    if stale_labels and active_broker_identity_keys:
        stale_labels = {
            label
            for label in stale_labels
            if not (
                risk_identity_by_label.get(str(label), set()) & active_broker_identity_keys
            )
        }
    if stale_labels and not used_cached_snapshot and _stale_close_guard_enabled():
        local_open_count = sum(
            1
            for entry in (risk_positions or {}).values()
            if int((entry or {}).get("qty", 0) or 0) > 0
        )
        broker_count = len(broker_labels)
        previous_broker_count = _active_snapshot_position_count(previous_healthy_positions)
        suspicious_reason = None
        if _snapshot_payload_marked_stale(raw):
            suspicious_reason = "broker_snapshot_marked_stale"
        elif broker_count == 0 and local_open_count > 0:
            suspicious_reason = "empty_broker_payload_with_local_open_positions"
        elif (
            previous_broker_count > 0
            and broker_count < previous_broker_count
            and local_open_count > 0
            and (previous_broker_count - broker_count)
            >= max(2, (previous_broker_count + 1) // 2)
        ):
            suspicious_reason = "sharp_broker_position_count_drop"
        if suspicious_reason:
            logger.warning(
                "[POS_SYNC] Skipping stale-close due to suspicious broker snapshot local_count=%d broker_count=%d previous_broker_count=%d reason=%s",
                local_open_count,
                broker_count,
                previous_broker_count,
                suspicious_reason,
            )
            result.blocked = True
            result.blocked_reason = suspicious_reason
            result.position_state = PositionState.DEGRADED.value
            result.update_reconciliation_state(
                state=PositionSyncReconciliationState.DEGRADED,
                evidence=PositionSyncEvidenceClass.MEDIUM,
                reason=suspicious_reason,
            )
            degraded_scope_manager.enter_degraded(
                scope_key=account_key,
                reason=DegradedReason.BROKER_EVIDENCE_DIVERGENCE,
                metadata={"suspicious_reason": suspicious_reason},
            )
            ambiguous_polls = _mark_scope_event(
                account_key,
                "__account__",
                event="ambiguous",
            )
            if ambiguous_polls >= _orphan_review_threshold_polls():
                result.position_state = PositionState.MANUAL_REVIEW.value
                result.update_reconciliation_state(
                    state=PositionSyncReconciliationState.ORPHAN_REVIEW,
                    evidence=PositionSyncEvidenceClass.MEDIUM,
                    reason=f"repeated_suspicious_snapshots:{ambiguous_polls}",
                )
            stale_labels = set()
    if stale_labels and not used_cached_snapshot:
        logger.info(
            "[POS_SYNC] Closing stale positions: %s",
            list(stale_labels)
        )
    if not used_cached_snapshot:
        for label in stale_labels:
            entry = risk_positions[label]
            qty = int(entry.get("qty", 0) or 0)
            if qty <= 0:
                continue
            # M3: Check DegradedScopeManager exit restriction before stale-close
            scope_key_for_exit = f"{account_key}:{label}"
            if degraded_scope_manager.is_exit_restricted(scope_key_for_exit):
                logger.warning(
                    "[POS_SYNC] Skipping stale close for %s — exit restricted by DegradedScopeManager (§13.2)",
                    label,
                )
                result.update_reconciliation_state(
                    state=PositionSyncReconciliationState.DEGRADED,
                    evidence=PositionSyncEvidenceClass.WEAK,
                    reason=f"exit_restricted_degraded:{label}",
                )
                continue
            missing_polls = _mark_scope_event(
                account_key,
                str(label),
                event="local_missing",
            )
            if missing_polls < _reconciliation_confirm_polls():
                result.update_reconciliation_state(
                    state=PositionSyncReconciliationState.RECONCILING,
                    evidence=PositionSyncEvidenceClass.MEDIUM,
                    reason=f"stale_close_requires_confirm:{label}",
                )
                logger.info(
                    "[POS_SYNC] Deferring stale close for %s until confirming broker-flat evidence arrives polls=%d/%d",
                    label,
                    missing_polls,
                    _reconciliation_confirm_polls(),
                )
                continue
            # §19.3 anti-pattern guard: block single-poll position close
            assert_not_single_poll_mutation(
                consecutive_polls=missing_polls,
                required_polls=_reconciliation_confirm_polls(),
                action="stale_position_close",
                scope_key=f"{account_key}:{label}",
            )
            closed_from_pending, pending_close_unresolved = _reconcile_pending_close_for_label(
                risk_manager=risk_manager,
                label=label,
            )
            if closed_from_pending:
                result.closed += 1
                _clear_scope_evidence(account_key, str(label))
                continue
            if pending_close_unresolved:
                logger.info(
                    "[POS_SYNC] Deferring stale close for %s while broker exit fill is still reconciling",
                    label,
                )
                result.update_reconciliation_state(
                    state=PositionSyncReconciliationState.RECONCILING,
                    evidence=PositionSyncEvidenceClass.MEDIUM,
                    reason=f"pending_close_reconciling:{label}",
                )
                continue
            exit_price = None
            if ltp_lookup and label in ltp_lookup:
                exit_price = float(ltp_lookup[label])
            elif label in active_broker_positions:
                exit_price = active_broker_positions[label].get("ltp") or active_broker_positions[label].get("avg_price")
            else:
                exit_price = float(entry.get("entry_price", 0.0))
            try:
                _execute_reconciliation_mutation(
                    account_key=account_key,
                    label=str(label),
                    actor="position_sync",
                    request_id=f"close:{label}:{missing_polls}",
                    mutation_fn=lambda: risk_manager._register_exit(  # type: ignore[attr-defined]
                        label,
                        float(exit_price or 0.0),
                        qty,
                    ),
                )
                result.closed += 1
                result.position_state = PositionState.FLAT_PENDING_CONFIRMATION.value
                _clear_scope_evidence(account_key, str(label))
                # Notify registered strategies of authoritative flat (STRONG evidence).
                # Two paths fire in sequence:
                # 1. Legacy strategy_flat_callback kwarg (routing-table based, kept for compat).
                # 2. position_flat_registry singleton (direct subscription, robust).
                _evidence = result.evidence_class or PositionSyncEvidenceClass.STRONG.value
                if _evidence == PositionSyncEvidenceClass.STRONG.value:
                    if callable(strategy_flat_callback):
                        try:
                            strategy_flat_callback(label=str(label), reason="pos_sync_strong_flat")
                        except Exception as _cb_exc:
                            logger.warning(
                                "[POS_SYNC] strategy_flat_callback failed label=%s: %s",
                                label, _cb_exc,
                            )
                    # Registry path: always fires, even if callback is None or routing table changes.
                    position_flat_registry.notify(label=str(label), reason="pos_sync_strong_flat")
            except Exception as exc:
                logger.warning("[POS_SYNC] Failed to close stale position %s: %s", label, exc)
                result.errors.append(str(exc))

    should_log_info = (
        result.added > 0
        or result.updated > 0
        or result.closed > 0
        or bool(result.errors)
        or bool(result.blocked)
        or bool(result.used_cached_snapshot)
        or result.reconciliation_state != PositionSyncReconciliationState.IN_SYNC.value
    )
    if not should_log_info and _log_noop_summary_at_info():
        should_log_info = True

    if result.errors and result.reconciliation_state == PositionSyncReconciliationState.IN_SYNC.value:
        result.update_reconciliation_state(
            state=PositionSyncReconciliationState.RECONCILING,
            evidence=PositionSyncEvidenceClass.WEAK,
            reason="sync_errors_present",
        )
    if result.evidence_class is None:
        result.evidence_class = (
            PositionSyncEvidenceClass.MEDIUM.value
            if used_cached_snapshot
            else PositionSyncEvidenceClass.STRONG.value
        )

    logger.log(
        logging.INFO if should_log_info else logging.DEBUG,
        "[POS_SYNC] Summary: %s",
        result.summary(),
    )

    return result


__all__ = [
    "PositionSyncEvidenceClass",
    "PositionSyncReconciliationState",
    "PositionSyncResult",
    "sync_positions_with_broker",
]
