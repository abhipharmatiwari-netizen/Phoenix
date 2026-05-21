"""Capital & risk controls (§5.2–§5.3) used by the execution hub AccountRunner."""

# Risk manager for order gating, PnL tracking, and kill switch enforcement.
# Tracks open positions, pending orders, and persistence for hub trading.
import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Any, Tuple
from datetime import datetime, timezone, timedelta, date
from app.core.dashboard_bus import dashboard_bus
from app.core.identifiers import BrokerAccountId, TenantId
from app.core.maintenance import MaintenanceManager
from app.core.instrument_control import InstrumentController
from app.core.order_client import (
    AngelHTTPBlockedError,
    AngelHTTPResponseError,
    AngelOrderClient,
)
from app.core.executed_tokens_tracker import executed_tokens_tracker
from app.core.trade_persister import TradePersister
from app.core.logging_utils import log_event
from app.risk.account_loss_guard import account_loss_guard
from app.core.p0_operational_guards import P0RuleViolation, assert_legacy_exit_allowed
from app.strategies.naming import canonicalize_strategy_name
from app.brokers.base import (
    OrderRequest,
    OrderSide,
    OrderType,
    ProductType,
    TimeInForce,
    OrderPurpose,
)

logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = APP_DIR.parent
CONFIG_DIR = APP_DIR / "config"
_LEGACY_STATE_PATH = CONFIG_DIR / "risk_positions.json"
_DEFAULT_STATE_PATH = REPO_ROOT / "logs" / "risk_positions.json"


def _resolve_risk_state_path(override: Optional[str] = None) -> Path:
    """Return the canonical risk state path, driven by RISK_STATE_PATH env var."""
    raw = override or os.getenv("RISK_STATE_PATH", "").strip()
    if raw:
        return Path(raw).resolve()
    return _DEFAULT_STATE_PATH
IST = timezone(timedelta(hours=5, minutes=30))

# Issue #245/#246: bounded, in-call retry policy for the legacy->durable
# kill-switch bridge. The bridge already retries on subsequent
# ``evaluate_account_loss`` cycles (see ``_propagate_kill_switch_to_durable_manager``
# and the retry block in ``_check_kill_switch``), but a transient Postgres
# blip in the SAME tick that flipped the legacy flag would leave the legacy
# and durable kill-switch state inconsistent until the next evaluation
# (~ ticks-to-seconds depending on cadence). For an auto-trip, "next
# evaluation" is too late — the hub OrderRouter could route fresh entries
# in that window. Add a brief same-call retry with exponential backoff so
# a transient blip never escapes a single auto-trip evaluation.
#
# Defaults: 3 attempts at 0.2s / 0.5s / 1.0s (worst-case ~1.7s extra
# latency on the auto-trip path, only on failure). Sleeps may be patched
# to zero in tests via ``RISK_BRIDGE_RETRY_DELAYS_SECONDS`` (comma
# separated floats; empty disables sleep). Idempotency: the trip call is
# guarded by a pre-trip ``get_record`` + ``post-trip re-read on ValueError``
# (already in place). ``ksm.save_state`` writes the snapshot of the
# in-memory manager state; re-issuing it on the same connection state is
# safe.
_BRIDGE_RETRY_DEFAULT_DELAYS: Tuple[float, ...] = (0.2, 0.5, 1.0)

# Issue #258 round-1 P1 (Codex): bound the bridge persistence path's total
# retry budget so emergency square-off in ``evaluate_account_loss`` is not
# blocked. The legacy implementation wrapped ``_do_save`` (which calls
# ``connect_with_retry`` with default ``max_attempts=3`` and 5s connect
# timeout plus 0.5/1.0s internal backoff) inside our outer 4-attempt
# retry. Worst-case effective attempts = 4 × 3 = 12 with up to ~60s of
# wall-clock — long enough to delay the subsequent ``square_off_all`` on
# the auto-trip path. The fix:
#   1. Pass ``max_attempts=1`` to ``connect_with_retry`` so the inner
#      retry loop is disabled — only the outer bridge retry runs.
#   2. Pass a tight ``connect_timeout_seconds`` so each attempt has a
#      bounded wall-clock cost.
# With 4 outer attempts × 2s connect timeout + 1.7s backoff between
# attempts (0.2+0.5+1.0), the worst-case total is ~9.7s — safely below
# the 10-second emergency-square-off budget.
_BRIDGE_POSTGRES_CONNECT_TIMEOUT_DEFAULT_SECONDS: int = 2

# Issue #258 round-2 P1 (Codex): hard wall-clock deadline that bounds the
# ENTIRE bridge call (get_record + trip+audit + save_state) from the
# top of ``_propagate_kill_switch_to_durable_manager`` until it returns.
# When the deadline is exceeded, the bridge logs
# ``kill_switch_bridge_deadline_exceeded`` ERROR and returns False so the
# caller's emergency ``square_off_all`` runs without further delay.
#
# Why this is necessary on top of the per-call ``connect_timeout`` and
# bounded outer retry: even with ``max_attempts=1``,
# ``connect_with_retry`` iterates ALL DSN candidates per call (the IPv4
# fast-path then the original host); the audit write inside
# ``KillSwitchManager.trip`` calls ``connect_with_retry`` with the
# DEFAULTS (``max_attempts=3``, 5s connect timeout); and any other code
# path the bridge depends on can leak retries. Defaulting to 5s gives
# the bridge a fail-closed-fast contract: it either persists within ~5s
# or it bails out and ``evaluate_account_loss`` flattens.
_BRIDGE_TOTAL_DEADLINE_DEFAULT_SECONDS: float = 5.0


def _bridge_postgres_connect_timeout_seconds() -> int:
    """Per-attempt Postgres connect timeout used by the bridge persistence
    path. Overridable via ``RISK_BRIDGE_POSTGRES_CONNECT_TIMEOUT_SECONDS``
    for ops tuning. Clamped to >= 1s.

    Issue #258 round-2 P2 (Codex): non-finite numeric overrides such as
    ``inf`` / ``1e309`` raise ``OverflowError`` from ``int(float(...))``
    rather than ``ValueError``. Catch ``Exception`` AND explicitly reject
    non-finite values so a malformed env var cannot raise out of the
    auto-trip path (``_propagate_kill_switch_to_durable_manager`` runs
    BEFORE ``square_off_all`` — any exception escaping this helper would
    block the emergency exit).
    """
    raw = os.getenv("RISK_BRIDGE_POSTGRES_CONNECT_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return _BRIDGE_POSTGRES_CONNECT_TIMEOUT_DEFAULT_SECONDS
    try:
        parsed = float(raw)
        if not math.isfinite(parsed):
            return _BRIDGE_POSTGRES_CONNECT_TIMEOUT_DEFAULT_SECONDS
        return max(1, int(parsed))
    except Exception:
        # Any parse / overflow failure -> fall back to defaults rather
        # than silently propagating into the live auto-trip path.
        return _BRIDGE_POSTGRES_CONNECT_TIMEOUT_DEFAULT_SECONDS


def _bridge_total_deadline_seconds() -> float:
    """Hard wall-clock deadline for the whole legacy->durable bridge call.

    Overridable via ``RISK_BRIDGE_TOTAL_DEADLINE_SECONDS``. Clamped to
    >= 1.0s so a misconfiguration can't disable the deadline entirely
    (which would re-introduce the unbounded-wait bug this guards against).

    Issue #258 round-2 P2 (Codex): the same non-finite / overflow safety
    applies here — fall back to the default rather than raise out of the
    auto-trip path.
    """
    raw = os.getenv("RISK_BRIDGE_TOTAL_DEADLINE_SECONDS", "").strip()
    if not raw:
        return _BRIDGE_TOTAL_DEADLINE_DEFAULT_SECONDS
    try:
        parsed = float(raw)
        if not math.isfinite(parsed):
            return _BRIDGE_TOTAL_DEADLINE_DEFAULT_SECONDS
        return max(1.0, parsed)
    except Exception:
        return _BRIDGE_TOTAL_DEADLINE_DEFAULT_SECONDS


def _bridge_dsn_candidate_count() -> int:
    """Count of DSN candidates ``connect_with_retry`` will iterate per call.

    PR #258 round-3 P2 (Codex line 1375): ``app/data/postgres.py``'s
    ``_dsn_connect_candidates`` returns 2 candidates when
    ``POSTGRES_FORCE_IPV4=true`` (or the host is
    ``host.docker.internal`` and IPv4 resolves), else 1. The bridge's
    per-candidate connect-timeout clamp MUST account for this — a
    naïve ``min(2s, remaining)`` clamp lets a single ``connect_with_
    retry`` call consume ``2 × 2s = 4s`` before returning, blowing the
    advertised hard deadline.

    We replicate the candidate-count logic conservatively here without
    importing the helper (no DNS resolution dependency at bridge entry
    — DNS itself can stall on a degraded resolver and the bridge MUST
    be cheap to enter). Assume 2 candidates whenever the env flag is
    truthy OR the DSN host might resolve to docker-internal; assume 1
    otherwise. The clamp is an upper bound: assuming 2 when only 1
    is iterated wastes a small amount of headroom but never permits a
    deadline overrun.
    """
    raw = os.getenv("POSTGRES_FORCE_IPV4", "")
    if str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}:
        return 2
    # Inspect the DSN host (docker-internal triggers the same
    # 2-candidate path inside _dsn_connect_candidates).
    try:
        from app.data.postgres import get_control_plane_dsn
        from psycopg.conninfo import conninfo_to_dict
        dsn = get_control_plane_dsn()
        params = conninfo_to_dict(dsn)
        host = str(params.get("host") or "").strip().lower()
        if host == "host.docker.internal":
            return 2
    except Exception:
        # Failure to resolve DSN/host -> assume conservative 2 so the
        # clamp never under-counts on a degraded import path.
        return 2
    return 1


def _bridge_retry_delays() -> Tuple[float, ...]:
    """Inter-attempt backoff delays for the bridge's bounded same-call
    retry. The retry count is ``1 + len(delays)``.

    Issue #258 round-2 P2 (Codex): the previous behaviour treated a
    single-token shorthand like ``RISK_BRIDGE_RETRY_DELAYS_SECONDS=0`` as
    "zero backoff", which collapsed the retry count from the documented
    4 attempts (1 + 3) down to 2 attempts (1 + 1). That regresses the
    transient-blip recovery the bridge is supposed to provide. The fix:
    when EVERY parsed delay is zero AND the parsed length is shorter
    than the default delays list, expand to a same-length all-zero
    tuple so the attempt count is preserved while the backoff is
    effectively disabled. Tests / ops can still set an explicit
    ``"0,0,0"`` to be unambiguous; the shorthand is preserved for
    convenience but no longer silently reduces the retry budget.
    """
    raw = os.getenv("RISK_BRIDGE_RETRY_DELAYS_SECONDS", "").strip()
    if not raw:
        return _BRIDGE_RETRY_DEFAULT_DELAYS
    parts: List[float] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            value = float(token)
            if not math.isfinite(value):
                # Non-finite override -> fall back to defaults rather
                # than treat as zero (which would silently disable
                # backoff on the live auto-trip path).
                return _BRIDGE_RETRY_DEFAULT_DELAYS
            parts.append(max(0.0, value))
        except Exception:
            # Malformed override -> fall back to defaults rather than
            # silently disabling retries on the live auto-trip path.
            return _BRIDGE_RETRY_DEFAULT_DELAYS
    if not parts:
        return _BRIDGE_RETRY_DEFAULT_DELAYS
    # Preserve attempt count when the shorthand zeroes out backoff but
    # provides fewer delays than the default (e.g. a single "0" token).
    # Without this, the bridge silently drops from 4 attempts to 2.
    if (
        all(delay == 0.0 for delay in parts)
        and len(parts) < len(_BRIDGE_RETRY_DEFAULT_DELAYS)
    ):
        return tuple(0.0 for _ in _BRIDGE_RETRY_DEFAULT_DELAYS)
    return tuple(parts)


class _BridgeConcurrentMutationAbort(Exception):
    """Sentinel raised by a ``pre_attempt_check`` to abort the retry loop
    early when the underlying record has been mutated by a concurrent
    operator action (Issue #258 round-1 P2). The bridge catches this
    explicitly and exits without marking ``_durable_kill_switch_bridge_
    succeeded`` true.
    """


class _BridgeDeadlineExceeded(Exception):
    """Sentinel raised by the deadline check to abort a retry loop or
    pre-step gate when the bridge's total wall-clock budget has been
    exhausted (Issue #258 round-2 P1 cluster). Caught by callers and the
    retry helper; the bridge then exits WITHOUT marking succeeded so
    ``evaluate_account_loss`` can proceed to ``square_off_all`` without
    further delay.
    """


class _BridgeDeadline:
    """Wall-clock deadline tracker shared by every step of the bridge.

    Issue #258 round-2 P1 cluster (Codex): the bridge has three separate
    retry / connection paths before ``square_off_all`` runs — ``get_
    record``, ``ksm.trip`` (whose audit write nests its own
    ``connect_with_retry`` with default ``max_attempts=3`` and a 5s
    connect timeout via ``app/core/audit_log.py``), and the
    ``save_state`` Postgres block (whose ``connect_with_retry`` iterates
    DSN candidates even with ``max_attempts=1``). Bounding each in
    isolation isn't enough — operators have reported real-world Postgres
    blips that compound across all three. A single hard deadline
    accumulating across every step is the only way to guarantee
    emergency square-off is not delayed past the legacy-broker action
    budget.

    Usage:
      deadline = _BridgeDeadline(timeout_seconds=5.0)
      deadline.check("before_get_record")  # raises if elapsed >= timeout
      deadline.connect_timeout_seconds(min_default=2)
      deadline.remaining()  # seconds left, 0 if past deadline
    """

    def __init__(self, *, timeout_seconds: float) -> None:
        self._timeout = float(timeout_seconds)
        self._start = time.monotonic()

    def elapsed(self) -> float:
        return time.monotonic() - self._start

    def remaining(self) -> float:
        return max(0.0, self._timeout - self.elapsed())

    def is_exceeded(self) -> bool:
        return self.elapsed() >= self._timeout

    def check(self, step: str) -> None:
        """Raise ``_BridgeDeadlineExceeded`` if the deadline has elapsed.
        ``step`` is a short tag used in the structured log emitted by
        the caller's exception handler."""
        if self.is_exceeded():
            raise _BridgeDeadlineExceeded(
                f"deadline_exceeded step={step} elapsed={self.elapsed():.2f}s "
                f"timeout={self._timeout:.2f}s"
            )

    def connect_timeout_seconds(
        self,
        *,
        min_default: int,
        candidate_count: int = 1,
    ) -> int:
        """Per-attempt connect timeout clamped to the remaining deadline
        budget. Used to ensure a single Postgres connect attempt cannot
        eat more time than the bridge has left.

        PR #258 round-3 P2 (Codex line 1375): ``connect_with_retry``
        iterates ``candidate_count`` DSN candidates per call (2 when
        ``POSTGRES_FORCE_IPV4=true`` or host=host.docker.internal,
        else 1). The clamp must be divided by candidate count so the
        total wall-clock of ALL candidate attempts stays within the
        remaining deadline budget — otherwise a single ``connect_with_
        retry`` call can consume ~``candidate_count * connect_timeout``
        before returning, pushing ``square_off_all`` past the
        advertised hard deadline.

        **PR #258 round-5 P1 (Codex line 343):** when the remaining
        budget is less than ``candidate_count`` seconds, we cannot
        sensibly start another ``connect_with_retry`` call — psycopg
        requires ``connect_timeout >= 1`` per candidate, so starting
        would let the call consume ``candidate_count * 1s`` before
        returning, pushing ``square_off_all`` past the advertised
        hard deadline. Raise ``_BridgeDeadlineExceeded`` so the caller
        bails out cleanly without ever entering psycopg.
        """
        remaining = self.remaining()
        try:
            n = max(1, int(candidate_count))
        except Exception:
            n = 1
        # PR #258 round-5 P1 (Codex line 343): refuse to start a
        # Postgres connect when the remaining budget cannot cover the
        # minimum wall-clock cost of one ``connect_with_retry`` call.
        # ``psycopg.connect`` clamps ``connect_timeout`` to >= 1 per
        # candidate, so the minimum cost of one call is
        # ``candidate_count * 1s``. If remaining < that, abort instead
        # of clamping up to 1 (which would let psycopg overrun the
        # deadline).
        if remaining < float(n):
            raise _BridgeDeadlineExceeded(
                f"deadline_exceeded step=connect_timeout_seconds "
                f"remaining={remaining:.2f}s candidates={n} "
                f"min_required={float(n):.2f}s"
            )
        per_candidate_budget = remaining / float(n)
        if per_candidate_budget <= 1.0:
            return 1
        return max(1, min(int(min_default), int(per_candidate_budget)))

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return (
            f"_BridgeDeadline(timeout={self._timeout:.2f}s "
            f"elapsed={self.elapsed():.2f}s)"
        )


def _retry_with_backoff(
    operation: Callable[[], Any],
    *,
    op_name: str,
    sleep: Callable[[float], None] = time.sleep,
    pre_attempt_check: Optional[Callable[[], None]] = None,
    deadline: Optional[_BridgeDeadline] = None,
) -> Tuple[bool, Optional[BaseException]]:
    """Run ``operation`` with bounded exponential backoff.

    Issue #245/#246: protects the legacy->durable kill-switch bridge
    against transient Postgres / hub-runtime blips that would otherwise
    leave the legacy ``kill_switch_activated`` flag set while the durable
    record is not persisted. Idempotency is the caller's responsibility
    (see ``_propagate_kill_switch_to_durable_manager`` doc).

    Issue #258 round-1 P2: ``pre_attempt_check`` is invoked BEFORE every
    attempt (including the first). If it raises
    ``_BridgeConcurrentMutationAbort`` the loop stops immediately,
    returning ``(False, <abort exc>)`` without further operation()
    calls. This lets callers detect that the underlying record has been
    mutated by a concurrent operator action between attempts and bail
    out cleanly rather than persisting a now-stale snapshot.

    Issue #258 round-2 P1 cluster: ``deadline`` (optional) bounds the
    total wall-clock budget across every step of the bridge. If the
    deadline is already exceeded BEFORE an attempt or BEFORE a backoff
    sleep, the loop stops immediately and returns
    ``(False, _BridgeDeadlineExceeded(...))``. The pre-attempt deadline
    check fires for the very first attempt too, so a bridge call that
    arrives already over budget exits without running ``operation`` at
    all (rather than letting ``connect_with_retry``'s DSN candidate
    iteration leak time).

    Returns ``(succeeded, last_exception)``. The last failure is logged
    inside this helper so callers can collapse error handling to a single
    success/fail branch.
    """
    delays = _bridge_retry_delays()
    # Total attempts = 1 (initial) + len(delays) (retries).
    attempts = 1 + len(delays)
    last_exc: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        # Issue #258 round-2 P1: hard deadline check BEFORE every attempt
        # (including attempt 1). A bridge call that arrives already over
        # budget — e.g. because an earlier step burned the entire window —
        # must NOT start a fresh ``connect_with_retry`` whose DSN-candidate
        # iteration would push us further past the square-off deadline.
        if deadline is not None and deadline.is_exceeded():
            abort = _BridgeDeadlineExceeded(
                f"deadline_exceeded op={op_name} attempt={attempt}/{attempts} "
                f"elapsed={deadline.elapsed():.2f}s"
            )
            logger.error(
                "kill_switch_bridge_deadline_exceeded op=%s attempt=%d/%d "
                "elapsed=%.2fs — abandoning retry; emergency square-off "
                "will proceed.",
                op_name, attempt, attempts, deadline.elapsed(),
            )
            return False, abort
        if pre_attempt_check is not None:
            try:
                pre_attempt_check()
            except _BridgeConcurrentMutationAbort as abort_exc:
                # Concurrent mutation observed — surface to the caller
                # WITHOUT marking the bridge succeeded. Logged loudly
                # so the operator can correlate the bridge's exit with
                # the operator-driven clear/rearm that intervened.
                logger.error(
                    "kill_switch_bridge_retry_aborted op=%s attempt=%d/%d "
                    "reason=concurrent_mutation_detected detail=%s — "
                    "bridge will NOT mark succeeded; next evaluate cycle "
                    "will re-validate.",
                    op_name, attempt, attempts, abort_exc,
                )
                return False, abort_exc
        try:
            operation()
            if attempt > 1:
                logger.info(
                    "kill_switch_bridge_retry_succeeded op=%s attempt=%d/%d",
                    op_name, attempt, attempts,
                )
            return True, None
        except _BridgeDeadlineExceeded as exc_dl:
            # PR #258 round-5 P1 (Codex line 343): the deadline tracker
            # raised mid-operation (e.g. ``connect_timeout_seconds``
            # refused to start a connect with subsecond budget). Treat
            # as terminal: do NOT retry, return immediately so the
            # caller falls through to emergency square-off.
            logger.error(
                "kill_switch_bridge_deadline_exceeded op=%s attempt=%d/%d "
                "exc=%s — abandoning retry; emergency square-off "
                "will proceed.",
                op_name, attempt, attempts, exc_dl,
            )
            return False, exc_dl
        except Exception as exc:  # noqa: BLE001 — bounded retry surface.
            last_exc = exc
            if attempt < attempts:
                # Issue #258 round-2 P1: clip the inter-attempt sleep to
                # the remaining deadline budget. Sleeping past the
                # deadline would waste square-off budget on a retry the
                # next iteration would just abandon anyway.
                delay = delays[attempt - 1]
                if deadline is not None:
                    remaining = deadline.remaining()
                    if remaining <= 0.0:
                        # Already over budget — exit the loop without
                        # sleeping (the next iteration's deadline check
                        # would fire anyway, but logging here gives a
                        # cleaner operator signal).
                        logger.error(
                            "kill_switch_bridge_deadline_exceeded op=%s "
                            "attempt=%d/%d elapsed=%.2fs (before backoff) — "
                            "abandoning retry; emergency square-off "
                            "will proceed.",
                            op_name, attempt, attempts, deadline.elapsed(),
                        )
                        return False, _BridgeDeadlineExceeded(
                            f"deadline_exceeded op={op_name} "
                            f"attempt={attempt}/{attempts} "
                            f"elapsed={deadline.elapsed():.2f}s before backoff"
                        )
                    if delay > remaining:
                        delay = remaining
                logger.warning(
                    "kill_switch_bridge_retry op=%s attempt=%d/%d delay=%.2fs exc=%s",
                    op_name, attempt, attempts, delay, exc,
                )
                if delay > 0:
                    try:
                        sleep(delay)
                    except Exception:  # pragma: no cover — sleep should not raise.
                        pass
            else:
                # Final failure surfaced loudly so the operator gets a
                # P0-grade signal rather than a silent durable/legacy split.
                logger.error(
                    "kill_switch_bridge_retry_exhausted op=%s attempts=%d "
                    "last_exc=%s — legacy flag will retry on next "
                    "evaluate cycle.",
                    op_name, attempts, exc,
                )
    return False, last_exc


# Simple container for risk decision outcomes.
@dataclass
class RiskDecision:
    allowed: bool
    status: str
    reason: str


@dataclass(frozen=True)
class _AuthoritativeAccountPnL:
    realized_pnl: float
    unrealized_pnl: float
    total_pnl: float
    source: str


# Manage risk checks, order submission, and position state.
class RiskManager:
    # Initialize risk limits, dependencies, and persisted state.
    def __init__(
        self,
        *,
        instrument_meta: Dict[str, Dict[str, Any]],
        order_client: AngelOrderClient,
        max_open_spreads_per_underlying: int = 1,
        per_underlying_max_open_spreads: Optional[Dict[str, int]] = None,
        day_loss_limit: float = 0.0,
        max_daily_loss: float = 0.0,
        max_intraday_drawdown: float = 0.0,
        kill_switch_square_off_open_positions: bool = True,
        risk_limits: Optional[Dict[str, Any]] = None,
        state_path: Optional[str] = None,
        trade_persister: Optional[TradePersister] = None,
        instrument_controller: Optional[InstrumentController] = None,
        tenant_id: Optional[TenantId] = None,
        broker_account_id: Optional[BrokerAccountId] = None,
    ) -> None:
        self.instrument_meta = instrument_meta
        self.order_client = order_client
        self.max_open_spreads = max_open_spreads_per_underlying
        # Normalize overrides to uppercase underlying keys for consistent lookup.
        self.per_underlying_max_open_spreads = {
            (k or "").upper(): int(v)
            for k, v in (per_underlying_max_open_spreads or {}).items()
            if v is not None
        }
        self.day_loss_limit = day_loss_limit
        self.max_daily_loss = float(max_daily_loss or 0.0)
        self.max_intraday_drawdown = float(max_intraday_drawdown or 0.0)
        self.kill_switch_square_off_open_positions = (
            True if kill_switch_square_off_open_positions else False
        )
        self.state_path = _resolve_risk_state_path(state_path)
        self.trade_persister = trade_persister
        self.current_trade_mode = "PAPER"

        # Order confirmation (prevents phantom entry/exit when broker rejects/doesn't fill).
        self.order_confirm_timeout_seconds = float(
            os.getenv("ANGEL_ORDER_CONFIRM_TIMEOUT_SECONDS", "8")
        )
        self.order_confirm_poll_seconds = float(
            os.getenv("ANGEL_ORDER_CONFIRM_POLL_SECONDS", "0.8")
        )
        # Pending orders (order_id -> context). We block duplicate actions for the same
        # label until the broker order reaches a terminal state.
        self._pending_orders: Dict[str, Dict[str, Any]] = {}
        self._state_lock = threading.RLock()
        self.tenant_id = str(tenant_id or "").strip() or None
        self.broker_account_id = str(broker_account_id or "").strip() or None
        self._forced_exit_suppression_seconds = max(
            5.0,
            float(os.getenv("RISK_FORCED_EXIT_SUPPRESSION_SECONDS", "45")),
        )
        self._forced_exit_suppression_until: Dict[str, float] = {}
        # Optional runtime hook used to route emergency/safety exits via hub OrderRouter.
        # When unset, legacy direct broker placement is used.
        self._hub_exit_submitter: Optional[Callable[..., bool]] = None
        # Operating mode string for authority-path enforcement.
        self._operating_mode: str = ""

        self.open_spreads_by_underlying: Dict[str, Dict[str, set[str]]] = {}
        self.open_positions: Dict[str, Dict[str, Any]] = {}
        self.realized_pnl = 0.0
        self.max_equity = 0.0
        self.daily_realized_pnl = 0.0
        self.daily_peak_equity = 0.0
        self.daily_peak_total_pnl = 0.0
        self.last_unrealized_pnl = 0.0
        self.last_total_pnl = 0.0
        self.kill_switch_activated = False
        self.kill_switch_date: Optional[date] = None
        # Issue #218 (PR #231 review): track the success of the durable-
        # kill-switch bridge separately from the legacy in-memory flag.
        # The bridge is invoked from inside the legacy auto-trip block,
        # but if the hub runtime / Postgres save fails on first attempt
        # the legacy flag is already True and ``should_activate`` will
        # not re-fire for subsequent evaluations — leaving the durable
        # manager INACTIVE and the hub OrderRouter still routing entries.
        # This flag drives a retry on every account-loss evaluation until
        # both the trip AND the persistence have succeeded.
        self._durable_kill_switch_bridge_succeeded = False
        # PR #258 round-3 P2 (Codex line 1266): persist the
        # "needs-audit-reemit" marker across bridge calls so a
        # partial-trip (in-memory TRIPPED but audit row missing) is
        # repaired on the NEXT evaluate cycle even when the current
        # bridge call exited early (deadline hit, persist failure, etc.)
        # before the local ``first_attempt_post_mutation_error`` flag
        # could drive the re-emit. Without this, the next cycle sees
        # ``get_record`` returning TRIPPED, sets ``already_tripped =
        # True``, skips the ``ksm.trip``/ValueError branch entirely,
        # and marks succeeded with NO audit row.
        self._pending_audit_reemit: bool = False
        self._pending_audit_reemit_reason: Optional[str] = None
        self.risk_limits = risk_limits or {}
        self.instrument_controller = instrument_controller
        self._underlying_name_to_label: Dict[str, str] = {}
        self.maintenance: Optional[MaintenanceManager] = None
        self._include_unrealized_in_kill_switch = (
            str(
                os.getenv("RISK_INCLUDE_UNREALIZED_IN_DAILY_LOSS", "true") or "true"
            )
            .strip()
            .lower()
            not in {"0", "false", "no", "off"}
        )
        self._account_loss_eval_interval_seconds = max(
            0.2,
            float(os.getenv("RISK_ACCOUNT_LOSS_EVAL_INTERVAL_SECONDS", "1.0")),
        )
        self._state_persist_min_interval_seconds = max(
            1.0,
            float(os.getenv("RISK_STATE_PERSIST_MIN_INTERVAL_SECONDS", "5.0")),
        )
        self._last_account_loss_eval_mono = 0.0
        self._last_state_persist_mono = 0.0
        self._maybe_migrate_legacy_state()
        trade_mode = str(os.getenv("TRADE_MODE", "") or "").strip().upper()
        if trade_mode == "LIVE":
            try:
                self.state_path.resolve().relative_to(APP_DIR)
                logger.warning(
                    "startup.risk_state_path_warning: RISK_STATE_PATH=%s resolves inside "
                    "the app/ package directory — this file will not persist across "
                    "container image rebuilds. Set RISK_STATE_PATH to a mounted volume path.",
                    self.state_path,
                )
            except ValueError:
                pass
        self._load_state()
        if self._reset_daily_if_new_day(datetime.now(timezone.utc)):
            self._persist_state()
        self._publish_account_loss_guard(source="startup")
        self.restored_positions: Dict[str, Dict[str, Any]] = dict(self.open_positions)
        # §147: In LIVE mode the hub/Postgres is authoritative for position state.
        # risk_positions.json is a stream-runtime shadow used for intra-session signal
        # gating only and must NOT be treated as authoritative (see anti_pattern_guards.py).
        # Warn operators when the file contains positions at startup so they can verify
        # the hub's internal_position_records are the ground truth.
        if trade_mode == "LIVE" and self.open_positions:
            logger.warning(
                "startup.risk_positions_file_has_positions: RISK_STATE_PATH=%s contains %d "
                "stream-side open_positions at startup. These are NON-AUTHORITATIVE — the hub's "
                "internal_position_records (Postgres) are the source of truth in LIVE mode. "
                "Positions: %s",
                self.state_path,
                len(self.open_positions),
                list(self.open_positions.keys()),
            )
        # Seed executed-token tracker from any restored open positions
        try:
            executed_tokens_tracker.bootstrap_from_positions(
                self.open_positions,
                self.instrument_meta,
            )
        except Exception as exc:
            logger.warning(
                "ExecutedTokensTracker.bootstrap_from_positions failed: %s", exc
            )

    @staticmethod
    def _resolve_tick_size(meta: Dict[str, Any]) -> Optional[float]:
        for key in (
            "tick_size",
            "tickSize",
            "ticksize",
            "price_step",
            "priceStep",
            "pricestep",
            "min_tick",
        ):
            raw_value = meta.get(key)
            try:
                tick_size = float(raw_value)
            except (TypeError, ValueError):
                continue
            if tick_size <= 0:
                continue
            if tick_size >= 1.0:
                tick_size /= 100.0
            return tick_size
        return None

    @staticmethod
    def _normalize_strategy_ratio(value: Any) -> Optional[float]:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if numeric <= 0:
            return numeric
        return numeric / 100.0 if numeric >= 1.0 else numeric

    @staticmethod
    def _normalize_env_percent(value: Any) -> Optional[float]:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if numeric <= 0:
            return numeric
        return numeric / 100.0

    @staticmethod
    def _snap_order_price(
        price: float,
        *,
        tick_size: Optional[float],
        side: str,
    ) -> float:
        if not tick_size or tick_size <= 0:
            return float(price)
        units = float(price) / float(tick_size)
        side_upper = str(side or "").upper()
        if side_upper == "BUY":
            snapped = math.floor(units + 1e-9) * float(tick_size)
        elif side_upper == "SELL":
            snapped = math.ceil(units - 1e-9) * float(tick_size)
        else:
            snapped = round(units) * float(tick_size)
        if snapped <= 0:
            snapped = float(tick_size)
        precision_text = f"{float(tick_size):.8f}".rstrip("0").rstrip(".")
        if "." in precision_text:
            precision = max(2, len(precision_text.split(".", 1)[1]))
        else:
            precision = 0
        return round(snapped, precision)

    @staticmethod
    def _parse_strategy_flag(value: Any) -> Optional[bool]:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "no", "n", "off"}:
            return False
        return None

    @classmethod
    def _broker_brackets_enabled(
        cls,
        strategy_context: Optional[Dict[str, Any]],
    ) -> bool:
        if not strategy_context:
            return True
        managed_exit_mode = str(
            strategy_context.get("managed_exit_mode") or ""
        ).strip().lower()
        if managed_exit_mode == "strategy":
            return False
        explicit = cls._parse_strategy_flag(
            strategy_context.get("broker_brackets_enabled")
        )
        if explicit is not None:
            return explicit
        return True

    @staticmethod
    def _is_invalid_slm_ordertype_error(value: Any) -> bool:
        text = str(value or "").lower()
        return "invalid value for field : ordertype" in text or (
            "vnd1001" in text and "ordertype" in text
        )

    def _fallback_stoplimit_price(
        self,
        *,
        trigger_price: float,
        exit_side: OrderSide,
        tick_size: Optional[float],
    ) -> float:
        tick = float(tick_size) if tick_size not in (None, 0, 0.0) else 0.0
        raw_price = float(trigger_price)
        if tick > 0:
            if exit_side == OrderSide.BUY:
                raw_price = float(trigger_price) + tick
            else:
                raw_price = float(trigger_price) - tick
        if raw_price <= 0:
            raw_price = float(trigger_price)
        if raw_price <= 0 and tick > 0:
            raw_price = tick
        return self._snap_order_price(
            raw_price,
            tick_size=tick_size,
            side=exit_side.value,
        )

    @staticmethod
    def _coerce_float(value: Any) -> Optional[float]:
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _coerce_int(value: Any) -> int:
        try:
            if value is None:
                return 0
            return int(float(value))
        except (TypeError, ValueError):
            return 0

    def _publish_account_loss_guard(
        self,
        *,
        source: str,
        unrealized_pnl: Optional[float] = None,
        total_pnl: Optional[float] = None,
    ) -> None:
        with self._state_lock:
            realized_pnl = self.realized_pnl
            daily_realized_pnl = self.daily_realized_pnl
            resolved_unrealized = (
                self.last_unrealized_pnl
                if unrealized_pnl is None
                else float(unrealized_pnl)
            )
            resolved_total = (
                self.last_total_pnl if total_pnl is None else float(total_pnl)
            )
            max_daily_loss = self.max_daily_loss
            max_intraday_drawdown = self.max_intraday_drawdown
            kill_switch_activated = self.kill_switch_activated
        account_loss_guard.update_snapshot(
            tenant_id=self.tenant_id,
            broker_account_id=self.broker_account_id,
            realized_pnl=realized_pnl,
            daily_realized_pnl=daily_realized_pnl,
            unrealized_pnl=resolved_unrealized,
            daily_total_pnl=resolved_total,
            max_daily_loss=max_daily_loss,
            max_intraday_drawdown=max_intraday_drawdown,
            kill_switch_activated=kill_switch_activated,
            source=source,
        )

    @staticmethod
    def _is_recovery_owned_entry(
        *,
        template_name: Optional[str],
        strategy_name: Optional[str],
        reason: Optional[str],
    ) -> bool:
        markers = {
            str(template_name or "").strip().upper(),
            str(strategy_name or "").strip().upper(),
            str(reason or "").strip().upper(),
        }
        recovery_markers = {
            "BROKER_SYNC",
            "POSITION_SYNC",
            "RECOVERY",
            "MANUAL_RECOVERY",
            "UNKNOWN",
            "__UNKNOWN__",
        }
        return bool(markers & recovery_markers)

    def _prune_forced_exit_suppressions_locked(self) -> None:
        now = time.monotonic()
        expired = [
            label
            for label, until_ts in self._forced_exit_suppression_until.items()
            if float(until_ts) <= now
        ]
        for label in expired:
            self._forced_exit_suppression_until.pop(label, None)

    def _mark_forced_exit_suppression(self, label: str) -> None:
        target_label = str(label or "").strip()
        if not target_label:
            return
        with self._state_lock:
            self._prune_forced_exit_suppressions_locked()
            self._forced_exit_suppression_until[target_label] = (
                time.monotonic() + self._forced_exit_suppression_seconds
            )

    def _clear_forced_exit_suppression(self, label: str) -> None:
        target_label = str(label or "").strip()
        if not target_label:
            return
        with self._state_lock:
            self._forced_exit_suppression_until.pop(target_label, None)

    def _has_forced_exit_suppression(self, label: str) -> bool:
        target_label = str(label or "").strip()
        if not target_label:
            return False
        with self._state_lock:
            self._prune_forced_exit_suppressions_locked()
            until_ts = self._forced_exit_suppression_until.get(target_label)
            return bool(until_ts and float(until_ts) > time.monotonic())

    def should_suppress_broker_sync_entry(self, label: str) -> bool:
        with self._state_lock:
            kill_switch_active = bool(self.kill_switch_activated)
        return kill_switch_active or self._has_forced_exit_suppression(label)

    def _propagate_kill_switch_to_durable_manager(
        self,
        *,
        reasons: List[str],
        source: Optional[str] = None,
    ) -> None:
        """Bridge legacy auto-trip to the hub-authoritative KillSwitchManager.

        Issue #218: ``RiskManager`` historically only flipped its in-memory
        ``kill_switch_activated`` flag, which the legacy stream-path consumes
        but the hub ``OrderRouter`` does not. The hub consults the durable
        ``KillSwitchManager`` (Postgres-backed). Without a bridge, an
        auto-trip on daily-loss / drawdown leaves the hub silently routing
        new entries until an operator manually trips.

        Idempotency / retry semantics (PR #231 review + issues #245/#246):

        - Once ``self._durable_kill_switch_bridge_succeeded`` is True, this
          method is a no-op (cheap fast-path).
        - On every call until success, the method tries the trip path AND
          the persistence path. The trip path is itself idempotent against
          an already-tripped durable record. Persistence is retried on
          every call until it succeeds.
        - **In-call bounded retry (issues #245/#246):** each of the three
          transient-failure surfaces — ``get_record``, ``ksm.trip``, and
          the Postgres ``save_state`` block — is wrapped in a bounded
          retry with brief exponential backoff (defaults 0.2/0.5/1.0s for
          3 retries on top of the initial attempt). This protects against
          a transient Postgres connection blip within a single auto-trip
          evaluate cycle. State-machine ``ValueError`` from ``ksm.trip``
          is NOT retried (it indicates the state is already not INACTIVE,
          which is handled by the post-trip race re-check). Sleeps may be
          tuned/zeroed via ``RISK_BRIDGE_RETRY_DELAYS_SECONDS`` (comma
          separated floats, e.g. ``"0"`` to disable backoff in tests).
        - **Bounded total retry budget (PR #258 round-1 P1, Codex):** the
          ``save_state`` path calls ``connect_with_retry`` with
          ``max_attempts=1`` and a tight per-attempt connect timeout
          (default 2s, overridable via
          ``RISK_BRIDGE_POSTGRES_CONNECT_TIMEOUT_SECONDS``).
        - **Hard wall-clock deadline (PR #258 round-2 P1 cluster, Codex):**
          a single ``_BridgeDeadline`` (default 5s, overridable via
          ``RISK_BRIDGE_TOTAL_DEADLINE_SECONDS``) bounds the ENTIRE
          bridge call across ``get_record`` + ``ksm.trip`` (whose audit
          write nests its own ``connect_with_retry(max_attempts=3,
          connect_timeout=5)`` in ``app/core/audit_log.py``) + the
          persistence path (whose ``connect_with_retry`` iterates DSN
          candidates even with ``max_attempts=1``). The retry helper
          checks the deadline before every attempt and clips
          inter-attempt sleeps to the remaining budget. The per-attempt
          connect timeout is also clamped to the remaining budget. Any
          step that would exceed the deadline logs
          ``kill_switch_bridge_deadline_exceeded`` ERROR and returns
          False so ``evaluate_account_loss`` can proceed to
          ``square_off_all`` without further delay.
        - **Concurrent-mutation guard (PR #258 round-1 P2, Codex):** the
          save loop captures an identity fingerprint
          ``(id, state, block_exits, updated_at)`` of the in-memory
          record before the first save attempt and re-checks it before
          every retry. If an operator clear/rearm mutates the same
          ``KillSwitchManager`` during the inter-attempt backoff, the
          bridge aborts the retry, logs
          ``kill_switch_bridge_save_state_aborted_concurrent_mutation``
          at ERROR, and leaves ``_durable_kill_switch_bridge_succeeded``
          False so the next evaluate cycle re-validates from scratch.
        - **Same-manager fingerprint recheck (PR #258 round-2 P2, Codex):**
          after a successful save, the bridge ALWAYS re-reads the
          record and matches its fingerprint against the entry
          snapshot — regardless of whether the active manager is the
          same instance. This catches a same-manager operator
          clear/rearm landing AFTER ``pre_attempt_check`` ran but
          BEFORE the save flushed, which would otherwise persist a
          stale TRIPPED snapshot and silently suppress retries.
        - **Unaudited partial-trip detection (PR #258 round-2 P2, Codex):**
          when the FIRST ``ksm.trip`` attempt mutates the in-memory
          record but raises in ``_emit_audit`` (or any post-mutation
          step), the bridge re-emits the audit through
          ``app.core.audit_log.emit_audit_event`` before marking
          succeeded so the trip remains durably accountable.
        - Lazy-imported to avoid circular dependency on the hub runtime.
        - Never raises into the legacy auto-trip path; all failures are
          logged at ERROR / WARNING and leave
          ``_durable_kill_switch_bridge_succeeded`` False so the next
          evaluate-account-loss cycle retries.
        - Operator-cleared only: trip is one-shot in TRIPPED/CLEAR_PENDING
          state; if the durable record is in CLEARED, the bridge logs
          ERROR (operator must rearm before a new auto-trip can register).
        """
        if self._durable_kill_switch_bridge_succeeded:
            return

        # Issue #258 round-2 P1 cluster (Codex): single hard wall-clock
        # deadline shared across every step of the bridge. See
        # ``_BridgeDeadline`` for why each-step bounding is insufficient
        # (audit-write inside ``ksm.trip`` calls ``connect_with_retry``
        # with default ``max_attempts=3`` × 5s, and ``connect_with_retry``
        # iterates DSN candidates per call even with ``max_attempts=1``).
        # Default 5s — fail-closed-fast so ``square_off_all`` always runs
        # within the legacy-broker action budget on a Postgres outage.
        deadline = _BridgeDeadline(
            timeout_seconds=_bridge_total_deadline_seconds(),
        )

        try:
            from app.hub.runtime import get_hub_runtime
            from app.risk.kill_switch import KillSwitchScope, KillSwitchState
        except Exception as exc:
            logger.error(
                "kill_switch_bridge_unavailable: import failed: %s", exc,
            )
            return

        try:
            runtime = get_hub_runtime()
        except Exception as exc:
            logger.error(
                "kill_switch_bridge_unavailable: hub runtime not initialised: %s",
                exc,
            )
            return

        ksm = getattr(runtime, "kill_switch_manager", None)
        if ksm is None:
            logger.error(
                "kill_switch_bridge_unavailable: KillSwitchManager not bound on hub runtime",
            )
            return

        # Deadline check after the import + runtime resolution. These
        # are cheap in steady state but lazy-imports can take 100s of ms
        # on a cold path; bail out if we've already burned the budget.
        if deadline.is_exceeded():
            logger.error(
                "kill_switch_bridge_deadline_exceeded step=post_resolve "
                "elapsed=%.2fs — emergency square-off will proceed.",
                deadline.elapsed(),
            )
            return

        # Step 1: ensure the durable manager is in TRIPPED state. The trip
        # path is idempotent — if a record already exists in TRIPPED or
        # CLEAR_PENDING, we skip the trip but still proceed to persistence.
        # If the record is in CLEARED state (operator confirm-clear done
        # but rearm not yet issued), we cannot re-trip without operator
        # action. Surface that as a structured ERROR and exit; the retry
        # will keep firing until either the operator rearms (allowing a
        # fresh trip) or clears the legacy flag (preventing the retry).
        # Issue #245: bounded same-call retry on transient blips so a
        # single failed lookup doesn't strand the legacy flag with no
        # durable counterpart until the next evaluate tick.
        already_tripped = False
        record_holder: Dict[str, Any] = {}

        def _do_get_record() -> None:
            record_holder["record"] = ksm.get_record(
                KillSwitchScope.GLOBAL, "GLOBAL",
            )

        get_ok, get_exc = _retry_with_backoff(
            _do_get_record, op_name="get_record", deadline=deadline,
        )
        if not get_ok:
            if isinstance(get_exc, _BridgeDeadlineExceeded):
                # Already logged loudly by the retry helper / deadline
                # check. Return without marking succeeded so the next
                # evaluate cycle retries; emergency square-off proceeds
                # immediately in this cycle.
                return
            logger.error(
                "kill_switch_bridge_unavailable: get_record query failed "
                "after retries: %s",
                get_exc,
            )
            return
        existing_record = record_holder.get("record")

        if existing_record is not None:
            existing_state = existing_record.state
            if existing_state in (
                KillSwitchState.TRIPPED,
                KillSwitchState.CLEAR_PENDING,
            ):
                already_tripped = True
            elif existing_state == KillSwitchState.CLEARED:
                # Distinct from the TRIPPED idempotent case: CLEARED means
                # the operator has confirmed-clear but not yet rearmed.
                # The state machine forbids a fresh trip here. The legacy
                # auto-trip is real (the loss/drawdown limit is breached)
                # but we cannot block hub entries until the operator
                # rearms. This is a P0 alert condition.
                logger.error(
                    "kill_switch_bridge_blocked_by_cleared_state: durable "
                    "GLOBAL kill switch is in CLEARED state pending operator "
                    "rearm — legacy auto-trip cannot propagate. Operator "
                    "MUST rearm or trip manually. record_id=%s",
                    existing_record.id,
                )
                return
            # INACTIVE falls through to the trip call below (unusual race
            # condition where the record exists but is already cleared
            # past CLEARED → INACTIVE).

        # PR #258 round-3 P2 (Codex line 1266): if a previous cycle
        # detected an unaudited partial-trip but exited before the
        # re-emit could write (deadline hit, persist failure, etc.),
        # the local ``first_attempt_post_mutation_error`` flag was
        # lost. On this cycle ``get_record`` shows TRIPPED so we'd
        # otherwise skip the re-emit entirely. Replay the re-emit now
        # against the existing TRIPPED record so the audit miss is
        # repaired. Clear the marker on success; leave it set on
        # failure so the next cycle retries.
        if (
            already_tripped
            and self._pending_audit_reemit
            and existing_record is not None
        ):
            stored_reason = (
                self._pending_audit_reemit_reason
                or f"risk_manager_auto: pending_audit_reemit_repair source={source}"
            )
            logger.warning(
                "kill_switch_bridge_pending_audit_reemit_replay record_id=%s "
                "— previous cycle detected unaudited partial trip but exited "
                "before audit was re-emitted; replaying now.",
                getattr(existing_record, "id", "unknown"),
            )
            replay_ok = self._reemit_kill_switch_bridge_audit(
                record=existing_record,
                bridge_reason=stored_reason,
                deadline=deadline,
            )
            if replay_ok:
                self._pending_audit_reemit = False
                self._pending_audit_reemit_reason = None
            else:
                # Leave the marker set; do NOT proceed to persist /
                # mark succeeded. Next cycle retries.
                logger.error(
                    "kill_switch_bridge_pending_audit_reemit_replay_failed "
                    "record_id=%s — bridge will NOT mark succeeded; next "
                    "evaluate cycle will retry the audit re-emit.",
                    getattr(existing_record, "id", "unknown"),
                )
                return

        if not already_tripped:
            reason_text = ",".join(reasons) if reasons else "unknown"
            if source:
                reason_text = f"{reason_text} source={source}"
            bridge_reason = f"risk_manager_auto: {reason_text}"
            # Issue #245: same-call bounded retry on transient connection
            # blips inside ``ksm.trip`` (the real implementation writes
            # through to Postgres). State-machine ValueError is NOT
            # transient and is re-raised immediately to the post-trip
            # race re-check below.
            #
            # Issue #258 round-2 P1 (Codex): the deadline check runs
            # before every attempt — ``ksm.trip`` calls ``_emit_audit``
            # which itself triggers a Postgres ``connect_with_retry``
            # with the default ``max_attempts=3`` and 5s connect timeout
            # (see ``app/core/audit_log.py``). A single audit write
            # during a Postgres outage can therefore consume the entire
            # square-off budget; the deadline guarantees we exit before
            # that happens.
            #
            # PR #258 round-5 P1 (Codex line 1208/1213): the deadline
            # check between attempts is necessary but not sufficient
            # because a SINGLE ``ksm.trip`` call can itself stall for
            # the full audit dual-write budget (``connect_with_retry``
            # with default ``max_attempts=3`` × 5s timeout = ~15s).
            # Apply env overrides for the duration of the trip loop so
            # the audit dual-write inside ``ksm.trip`` is bounded to one
            # attempt with a remaining-budget timeout, preventing a
            # single trip from blowing the bridge deadline. The JSONL
            # audit write is unchanged (durable, local-disk).
            #
            # Issue #258 round-2 P2 (Codex): track whether ANY
            # ksm.trip attempt mutated the in-memory record but then
            # raised in ``_emit_audit`` (or any post-mutation step).
            # If so, the in-memory state is TRIPPED but the audit row
            # was never written. The bridge MUST NOT silently mark this
            # as succeeded — we re-emit the audit through the bridge
            # before proceeding so the trip is durably accountable.
            #
            # PR #258 round-5 P1 (Codex line 1234): the post-mutation
            # detection runs on EVERY retry, not only attempt 1. If
            # attempt 1 fails before mutation but attempt 2 mutates
            # then fails the audit, attempt 3 would raise ValueError
            # (record already TRIPPED) and we'd silently mark success
            # with no audit row.
            #
            # PR #258 round-5 P1 (Codex line 1247): persist the
            # ``self._pending_audit_reemit`` marker AS SOON AS post-
            # mutation is detected (not only later in the ValueError
            # branch). If the deadline fires between detection and the
            # ValueError handler, the local flag is lost; on the next
            # cycle ``get_record`` sees TRIPPED and skips the trip
            # branch entirely, so the audit miss would never be repaired.
            delays = _bridge_retry_delays()
            attempts = 1 + len(delays)
            record: Any = None
            trip_value_error: Optional[ValueError] = None
            trip_other_error: Optional[BaseException] = None
            first_attempt_post_mutation_error: bool = False

            # PR #258 round-5 P1 (Codex line 1208/1213): bound the
            # nested audit dual-write inside ``ksm.trip`` via env
            # overrides for the duration of the loop. Restore the
            # original values in the finally block even if a retry
            # exception escapes.
            _POSTGRES_TIMEOUT_ENV = "POSTGRES_CONNECT_TIMEOUT_SECONDS"
            _AUDIT_PG_ATTEMPTS_ENV = "AUDIT_POSTGRES_MAX_ATTEMPTS"
            trip_prev_pg_timeout: Optional[str] = os.environ.get(_POSTGRES_TIMEOUT_ENV)
            trip_prev_audit_attempts: Optional[str] = os.environ.get(_AUDIT_PG_ATTEMPTS_ENV)
            trip_env_applied = False
            try:
                try:
                    trip_remaining = deadline.remaining()
                    trip_candidates = _bridge_dsn_candidate_count()
                    trip_per_candidate = max(
                        1.0,
                        trip_remaining / float(max(1, trip_candidates)),
                    )
                    trip_tight = max(1, int(trip_per_candidate))
                    os.environ[_POSTGRES_TIMEOUT_ENV] = str(trip_tight)
                    os.environ[_AUDIT_PG_ATTEMPTS_ENV] = "1"
                    trip_env_applied = True
                except Exception:  # pragma: no cover - defensive
                    pass

                for attempt in range(1, attempts + 1):
                    # Issue #258 round-2 P1: hard deadline check before
                    # EACH trip attempt. Even with the env caps applied,
                    # the JSONL audit write or other in-process steps
                    # can take time; the deadline keeps the loop bounded.
                    if deadline.is_exceeded():
                        logger.error(
                            "kill_switch_bridge_deadline_exceeded op=ksm_trip "
                            "attempt=%d/%d elapsed=%.2fs — emergency square-off "
                            "will proceed.",
                            attempt, attempts, deadline.elapsed(),
                        )
                        return
                    try:
                        record = ksm.trip(
                            KillSwitchScope.GLOBAL,
                            "GLOBAL",
                            reason=bridge_reason,
                            actor="risk_manager_auto",
                        )
                        if attempt > 1:
                            logger.info(
                                "kill_switch_bridge_retry_succeeded op=ksm_trip "
                                "attempt=%d/%d", attempt, attempts,
                            )
                        trip_other_error = None
                        trip_value_error = None
                        break
                    except ValueError as exc_ve:
                        trip_value_error = exc_ve
                        break  # Non-retriable; fall through to race re-check.
                    except Exception as exc_t:  # noqa: BLE001
                        trip_other_error = exc_t
                        # PR #258 round-5 P1 (Codex line 1234): check
                        # whether the in-memory state was mutated to
                        # TRIPPED on ANY attempt (not only attempt 1).
                        # If attempt 1 fails before mutation and
                        # attempt 2 mutates then fails audit, attempt 3
                        # would raise ValueError and we'd silently mark
                        # succeeded with no audit row.
                        #
                        # PR #258 round-5 P1 (Codex line 1247): set
                        # ``self._pending_audit_reemit`` IMMEDIATELY on
                        # detection so the marker survives a deadline
                        # hit between here and the ValueError handler.
                        if not first_attempt_post_mutation_error:
                            try:
                                mutation_record = ksm.get_record(
                                    KillSwitchScope.GLOBAL, "GLOBAL",
                                )
                                mutation_state = (
                                    mutation_record.state
                                    if mutation_record is not None else None
                                )
                                if mutation_state in (
                                    KillSwitchState.TRIPPED,
                                    KillSwitchState.CLEAR_PENDING,
                                ):
                                    first_attempt_post_mutation_error = True
                                    # PR #258 round-5 P1 (Codex line 1247):
                                    # persist the marker NOW so a later
                                    # deadline-exit cannot lose it. The
                                    # ValueError branch (or the next
                                    # cycle's already_tripped repair
                                    # path) will clear it on successful
                                    # re-emit.
                                    self._pending_audit_reemit = True
                                    self._pending_audit_reemit_reason = bridge_reason
                                    logger.error(
                                        "kill_switch_bridge_unaudited_partial_trip "
                                        "detected: ksm.trip raised %s on "
                                        "attempt=%d/%d AFTER mutating in-memory "
                                        "state to %s — audit row may be "
                                        "missing; pending_audit_reemit marker "
                                        "persisted; will re-emit audit via "
                                        "fallback path before marking succeeded.",
                                        type(exc_t).__name__,
                                        attempt, attempts,
                                        mutation_state.value,
                                    )
                            except Exception:
                                # Lookup failure — leave the flag unset
                                # and let the post-trip recheck path
                                # handle it.
                                pass
                        if attempt < attempts:
                            delay = delays[attempt - 1]
                            # Clip backoff to the remaining deadline so we
                            # never sleep past the square-off budget.
                            remaining = deadline.remaining()
                            if remaining <= 0.0:
                                logger.error(
                                    "kill_switch_bridge_deadline_exceeded "
                                    "op=ksm_trip attempt=%d/%d elapsed=%.2fs "
                                    "(before backoff) — emergency square-off "
                                    "will proceed.",
                                    attempt, attempts, deadline.elapsed(),
                                )
                                return
                            if delay > remaining:
                                delay = remaining
                            logger.warning(
                                "kill_switch_bridge_retry op=ksm_trip "
                                "attempt=%d/%d delay=%.2fs exc=%s",
                                attempt, attempts, delay, exc_t,
                            )
                            if delay > 0:
                                try:
                                    time.sleep(delay)
                                except Exception:  # pragma: no cover
                                    pass
                        else:
                            logger.error(
                                "kill_switch_bridge_retry_exhausted op=ksm_trip "
                                "attempts=%d last_exc=%s — will retry on next "
                                "evaluate cycle.",
                                attempts, exc_t,
                            )
            finally:
                # PR #258 round-5 P1 (Codex line 1208/1213): restore
                # the env vars regardless of how the trip loop exits
                # (success, ValueError, deadline-return, exception).
                if trip_env_applied:
                    try:
                        if trip_prev_pg_timeout is None:
                            os.environ.pop(_POSTGRES_TIMEOUT_ENV, None)
                        else:
                            os.environ[_POSTGRES_TIMEOUT_ENV] = trip_prev_pg_timeout
                    except Exception:  # pragma: no cover - defensive
                        pass
                    try:
                        if trip_prev_audit_attempts is None:
                            os.environ.pop(_AUDIT_PG_ATTEMPTS_ENV, None)
                        else:
                            os.environ[_AUDIT_PG_ATTEMPTS_ENV] = trip_prev_audit_attempts
                    except Exception:  # pragma: no cover - defensive
                        pass

            if trip_value_error is not None:
                # ValueError from trip() means "current state is not
                # INACTIVE" — could be TRIPPED, CLEAR_PENDING, or CLEARED.
                # Codex P2 (round 2 review): re-read the record AFTER the
                # rejected trip and ONLY treat as already-tripped if the
                # state is TRIPPED or CLEAR_PENDING. A race with an
                # operator clear could move INACTIVE → CLEARED between
                # our pre-trip get_record() and the trip() call; without
                # this re-check we would persist the CLEARED record and
                # mark the bridge succeeded while the hub OrderRouter is
                # NOT actually blocking entries.
                exc = trip_value_error
                post_trip_holder: Dict[str, Any] = {}

                def _do_post_trip_get() -> None:
                    post_trip_holder["record"] = ksm.get_record(
                        KillSwitchScope.GLOBAL, "GLOBAL",
                    )

                post_ok, post_exc = _retry_with_backoff(
                    _do_post_trip_get, op_name="post_trip_get_record",
                    deadline=deadline,
                )
                if not post_ok:
                    if isinstance(post_exc, _BridgeDeadlineExceeded):
                        return
                    logger.error(
                        "kill_switch_bridge_unavailable: post-trip "
                        "get_record failed after retries: %s",
                        post_exc,
                    )
                    return
                post_trip_record = post_trip_holder.get("record")
                post_trip_state = (
                    post_trip_record.state if post_trip_record is not None
                    else None
                )
                if post_trip_state in (
                    KillSwitchState.TRIPPED,
                    KillSwitchState.CLEAR_PENDING,
                ):
                    logger.info(
                        "kill_switch_bridge_skip_race: trip rejected, "
                        "post-trip state=%s — treating as already-tripped: %s",
                        post_trip_state.value, exc,
                    )
                    already_tripped = True
                elif post_trip_state == KillSwitchState.CLEARED:
                    logger.error(
                        "kill_switch_bridge_blocked_by_cleared_state: trip "
                        "rejected — post-trip state=CLEARED. Operator MUST "
                        "rearm or trip manually. Bridge NOT marked succeeded. "
                        "exc=%s",
                        exc,
                    )
                    return
                else:
                    logger.error(
                        "kill_switch_bridge_unexpected_state: trip rejected "
                        "but post-trip state=%s (expected TRIPPED / "
                        "CLEAR_PENDING / CLEARED). Bridge NOT marked "
                        "succeeded. exc=%s",
                        post_trip_state.value if post_trip_state else "None",
                        exc,
                    )
                    return
                # Issue #258 round-2 P2: if the FIRST attempt's exception
                # came from AFTER the in-memory mutation (e.g.
                # ``_emit_audit`` raised), the trip is now in-memory
                # TRIPPED but the audit row is missing. The subsequent
                # ``ksm.trip`` retries raised ValueError ("not INACTIVE")
                # without emitting an audit either. Re-emit the audit
                # event through ``emit_audit_event`` directly so the
                # trip is durably accountable before we mark succeeded.
                #
                # PR #258 round-3 P2 (Codex line 1608): capture the
                # bool result and refuse to mark succeeded when the
                # re-emit failed — otherwise an unwritable audit path
                # permanently suppresses retries with NO audit row.
                # PR #258 round-3 P2 (Codex line 1266): persist the
                # "needs re-emit" marker so the NEXT bridge cycle
                # repairs the audit even if this cycle exits early
                # (deadline hit, persist failure, etc.) before reaching
                # the re-emit call. The marker is cleared on successful
                # re-emit OR when the operator manually re-establishes
                # the trip on a fresh cycle.
                if first_attempt_post_mutation_error and post_trip_record is not None:
                    self._pending_audit_reemit = True
                    self._pending_audit_reemit_reason = bridge_reason
                    reemit_ok = self._reemit_kill_switch_bridge_audit(
                        record=post_trip_record,
                        bridge_reason=bridge_reason,
                        deadline=deadline,
                    )
                    if reemit_ok:
                        self._pending_audit_reemit = False
                        self._pending_audit_reemit_reason = None
                    else:
                        logger.error(
                            "kill_switch_bridge_audit_reemit_failed_post_value_error "
                            "record_id=%s — bridge will NOT mark "
                            "succeeded; next evaluate cycle will retry "
                            "the audit re-emit via the already_tripped "
                            "repair path.",
                            getattr(post_trip_record, "id", "unknown"),
                        )
                        return
            elif trip_other_error is not None:
                # All retries raised non-ValueError exceptions and the
                # state did not transition (no post-trip TRIPPED detected
                # via the first-attempt path either). Bail out — next
                # evaluate cycle retries.
                #
                # Issue #258 round-2 P2 corner case: if every attempt
                # raised AFTER mutation (i.e. the mutation IS in place
                # but every audit failed), ``first_attempt_post_mutation_error``
                # is True; we still bail out here because we never
                # successfully ran ksm.trip to completion. The next
                # evaluate cycle will see TRIPPED via get_record and
                # the bridge will proceed to persistence — at which
                # point the audit re-emit fires via the ValueError
                # branch above.
                logger.error(
                    "kill_switch_bridge_trip_failed (will retry next "
                    "evaluate cycle; first_attempt_post_mutation=%s): %s",
                    first_attempt_post_mutation_error, trip_other_error,
                )
                return
            else:
                logger.warning(
                    "kill_switch_bridge_tripped record_id=%s scope=GLOBAL "
                    "actor=risk_manager_auto reason=%s",
                    record.id,
                    bridge_reason,
                )

        # Step 2: persist. Issue #246: bounded same-call retry with brief
        # backoff so a transient Postgres connection blip doesn't leave
        # the durable trip in-memory only (a restart would lose it). The
        # save is idempotent — ``ksm.save_state(conn)`` writes the
        # current snapshot of the in-memory manager state; re-issuing
        # after a partial failure is safe (the manager's state is the
        # source of truth, the Postgres row is the cache). The outer
        # retry-on-next-evaluate-cycle remains as the ultimate fallback
        # if all in-call attempts fail.
        #
        # Issue #258 round-1 P1 (Codex): ``connect_with_retry`` is called
        # with ``max_attempts=1`` and a tight ``connect_timeout_seconds``
        # so the inner retry loop is disabled and each attempt has a
        # bounded wall-clock cost. The outer ``_retry_with_backoff`` is
        # now the SOLE retry surface for this path (no nested retries).
        # Worst-case total: 4 outer attempts × 2s connect timeout + 1.7s
        # backoff = ~9.7s — under the 10s emergency-square-off budget so
        # ``square_off_all`` is not delayed by bridge persistence retries.
        #
        # Issue #258 round-1 P2 (Codex): capture an identity fingerprint
        # of the in-memory manager record we intend to persist BEFORE
        # the first save attempt. Re-check the fingerprint before every
        # retry attempt. If an operator clear / rearm mutates the same
        # ``KillSwitchManager`` during the inter-attempt backoff, the
        # in-memory state we'd save no longer matches the TRIPPED record
        # that was validated upstream — persisting it (and then marking
        # the bridge succeeded) would silently suppress future retries
        # while the active record is no longer TRIPPED. Abort cleanly
        # instead and let the next evaluate cycle re-validate.
        try:
            from app.data.postgres import (
                connect_with_retry,
                get_control_plane_dsn,
            )
        except Exception as exc:
            logger.error(
                "kill_switch_bridge_save_state_failed: postgres helpers "
                "import failed (will retry next evaluate cycle): %s",
                exc,
            )
            return

        # Capture the pre-save fingerprint of the manager's GLOBAL
        # record. ``get_record`` may transiently fail; that's OK — we
        # fall back to a sentinel that disables the concurrent-mutation
        # check rather than aborting the save entirely (a transient
        # get_record blip is exactly what the bridge is designed to
        # tolerate; the outer cycle retry remains the safety net).
        def _record_fingerprint(rec: Any) -> Optional[Tuple[Any, ...]]:
            if rec is None:
                return None
            state = getattr(rec, "state", None)
            state_val = getattr(state, "value", state)
            return (
                getattr(rec, "id", None),
                state_val,
                bool(getattr(rec, "block_exits", False)),
                getattr(rec, "updated_at", None),
            )

        try:
            pre_save_record = ksm.get_record(
                KillSwitchScope.GLOBAL, "GLOBAL",
            )
            pre_save_fp: Optional[Tuple[Any, ...]] = _record_fingerprint(
                pre_save_record,
            )
            fingerprint_captured = True
        except Exception as exc:
            logger.warning(
                "kill_switch_bridge_pre_save_fingerprint_unavailable: %s "
                "— concurrent-mutation guard disabled for this cycle.",
                exc,
            )
            pre_save_fp = None
            fingerprint_captured = False

        configured_connect_timeout = _bridge_postgres_connect_timeout_seconds()
        # PR #258 round-3 P2 (Codex line 1375): probe candidate count
        # ONCE at bridge save entry so the deadline clamp divides the
        # remaining budget across all candidates a single
        # ``connect_with_retry`` call will iterate. Re-probing per
        # attempt would risk a DNS-resolver stall consuming budget
        # the clamp is meant to protect.
        dsn_candidate_count = _bridge_dsn_candidate_count()

        def _do_save() -> None:
            # Issue #258 round-2 P1: clip the per-attempt connect
            # timeout to the remaining deadline so a single Postgres
            # connect attempt cannot eat more time than the bridge has
            # left. ``connect_with_retry`` ALSO iterates DSN candidates
            # per call (POSTGRES_FORCE_IPV4=true / host=host.docker.internal
            # → 2 candidates) even with ``max_attempts=1``.
            #
            # PR #258 round-3 P2 (Codex line 1375): divide the clamp
            # by ``dsn_candidate_count`` so the SUM of all per-
            # candidate connect attempts within one
            # ``connect_with_retry`` call stays under the remaining
            # deadline. Without this division a single save attempt
            # consumes ~2 × clamp on the IPv4-prefer path, blowing the
            # advertised total deadline.
            effective_timeout = deadline.connect_timeout_seconds(
                min_default=configured_connect_timeout,
                candidate_count=dsn_candidate_count,
            )
            with connect_with_retry(
                get_control_plane_dsn(),
                autocommit=True,
                max_attempts=1,
                connect_timeout_seconds=effective_timeout,
            ) as conn:
                ksm.save_state(conn)

        def _abort_if_concurrent_mutation() -> None:
            if not fingerprint_captured:
                return
            try:
                current = ksm.get_record(KillSwitchScope.GLOBAL, "GLOBAL")
            except Exception:
                # Transient lookup failure — don't abort on it. The outer
                # save will surface the real Postgres failure if any.
                return
            current_fp = _record_fingerprint(current)
            if current_fp != pre_save_fp:
                raise _BridgeConcurrentMutationAbort(
                    f"expected={pre_save_fp} current={current_fp}"
                )

        save_ok, save_exc = _retry_with_backoff(
            _do_save,
            op_name="save_state",
            pre_attempt_check=_abort_if_concurrent_mutation,
            deadline=deadline,
        )
        if not save_ok:
            if isinstance(save_exc, _BridgeConcurrentMutationAbort):
                logger.error(
                    "kill_switch_bridge_save_state_aborted_concurrent_mutation "
                    "(operator clear/rearm intervened between save retries — "
                    "bridge will NOT mark succeeded; next evaluate cycle will "
                    "re-validate): %s",
                    save_exc,
                )
                return
            if isinstance(save_exc, _BridgeDeadlineExceeded):
                # Deadline already logged loudly by the retry helper.
                return
            logger.error(
                "kill_switch_bridge_save_state_failed (all in-call retries "
                "exhausted — will retry next evaluate cycle; durable trip is "
                "in-memory only and will NOT survive a restart): %s",
                save_exc,
            )
            return

        # Issue #258 round-2 P2 (Codex line 987): always re-verify the
        # current record fingerprint against the pre-save snapshot —
        # regardless of whether the active manager is a different
        # instance. The previous logic only checked the swap branch,
        # but a same-manager operator clear/rearm during the FINAL
        # backoff window (between the last pre_attempt_check and the
        # successful save) would otherwise persist a stale TRIPPED
        # snapshot and silently suppress future retries. Re-read and
        # match against the entry fingerprint here so a same-manager
        # mutation is detected too.
        if fingerprint_captured:
            try:
                post_save_record = ksm.get_record(
                    KillSwitchScope.GLOBAL, "GLOBAL",
                )
            except Exception as exc:
                logger.error(
                    "kill_switch_bridge_post_save_recheck_failed (will "
                    "retry next evaluate cycle): %s",
                    exc,
                )
                return
            post_save_fp = _record_fingerprint(post_save_record)
            if post_save_fp != pre_save_fp:
                logger.error(
                    "kill_switch_bridge_post_save_fingerprint_mismatch "
                    "expected=%s current=%s — operator clear/rearm "
                    "intervened on the same manager AFTER save succeeded; "
                    "bridge will NOT mark succeeded; attempting to "
                    "repair the stale row by re-saving the current "
                    "manager state so operator intent wins.",
                    pre_save_fp, post_save_fp,
                )
                # PR #258 round-3 P2 (Codex line 1445): the bridge has
                # ALREADY persisted its stale TRIPPED snapshot. The
                # operator's clear/rearm landed between the final
                # pre_attempt_check and the post-save read. Postgres
                # now holds the bridge's stale row on top of the
                # operator's intent. Without repair, the stale row
                # remains until the next save cycle and can be
                # observed by readers (KillSwitchManager.load_state
                # on restart will see the stale TRIPPED again).
                #
                # Repair: re-save the CURRENT manager state so the
                # operator's intent overwrites our stale snapshot.
                # Use the same deadline-bounded ``connect_with_retry``
                # path so this repair cannot blow the bridge budget.
                # On repair failure, leave succeeded=False so the next
                # evaluate cycle retries.
                if post_save_record is None:
                    # Operator-driven mutation may have left the
                    # record as None (rare; recovery path). Bail.
                    return
                try:
                    repair_timeout = deadline.connect_timeout_seconds(
                        min_default=configured_connect_timeout,
                        candidate_count=dsn_candidate_count,
                    )
                    if deadline.is_exceeded():
                        logger.error(
                            "kill_switch_bridge_post_save_repair_skipped_deadline "
                            "elapsed=%.2fs — stale row remains until next "
                            "evaluate cycle.",
                            deadline.elapsed(),
                        )
                        return
                    with connect_with_retry(
                        get_control_plane_dsn(),
                        autocommit=True,
                        max_attempts=1,
                        connect_timeout_seconds=repair_timeout,
                    ) as repair_conn:
                        ksm.save_state(repair_conn)
                    logger.warning(
                        "kill_switch_bridge_post_save_repair_succeeded "
                        "expected=%s current=%s — re-saved current "
                        "manager state to overwrite stale bridge "
                        "snapshot; operator intent now persisted.",
                        pre_save_fp, post_save_fp,
                    )
                except Exception as repair_exc:
                    logger.error(
                        "kill_switch_bridge_post_save_repair_failed "
                        "expected=%s current=%s exc=%s — stale row "
                        "may remain in Postgres until next evaluate "
                        "cycle re-saves the (then-current) state.",
                        pre_save_fp, post_save_fp, repair_exc,
                    )
                return

        # Codex P2 (round 2 review): verify the active KillSwitchManager
        # on the hub runtime sees the trip BEFORE we declare success. The
        # `ksm` we tripped/saved is the instance bound at the start of
        # this method; ``AppRuntime`` may replace ``runtime.kill_switch_
        # manager`` with a fresh instance loaded from Postgres after we
        # captured our reference (e.g. stream worker fires a legacy trip
        # before AppRuntime's startup recovery completes). If the active
        # manager is a different instance and is NOT tripped, the hub
        # OrderRouter's GlobalKillSwitchInterceptor will keep routing
        # entries — defeating the purpose of this bridge. Re-resolve and
        # apply the trip to the replacement if necessary.
        try:
            active_runtime = get_hub_runtime()
            active_ksm = getattr(active_runtime, "kill_switch_manager", None)
        except Exception as exc:
            logger.error(
                "kill_switch_bridge_post_save_resolve_failed: cannot "
                "verify active KillSwitchManager (will retry next "
                "evaluate cycle): %s",
                exc,
            )
            return

        if active_ksm is not ksm:
            try:
                active_is_tripped = active_ksm is not None and active_ksm.is_tripped(
                    KillSwitchScope.GLOBAL, "GLOBAL"
                )
            except Exception as exc:
                logger.error(
                    "kill_switch_bridge_active_query_failed (will retry "
                    "next evaluate cycle): %s",
                    exc,
                )
                return

            if not active_is_tripped:
                logger.warning(
                    "kill_switch_bridge_active_manager_swapped: hub runtime "
                    "kill_switch_manager was replaced after our trip; "
                    "re-applying trip on the active instance.",
                )
                try:
                    active_ksm.trip(
                        KillSwitchScope.GLOBAL,
                        "GLOBAL",
                        reason=f"risk_manager_auto: post-swap re-trip ({source})",
                        actor="risk_manager_auto",
                    )
                except ValueError:
                    # Active manager raised — re-read and verify.
                    try:
                        active_record = active_ksm.get_record(
                            KillSwitchScope.GLOBAL, "GLOBAL"
                        )
                    except Exception:
                        active_record = None
                    if active_record is None or active_record.state not in (
                        KillSwitchState.TRIPPED,
                        KillSwitchState.CLEAR_PENDING,
                    ):
                        logger.error(
                            "kill_switch_bridge_active_retrip_unverified "
                            "(will retry next evaluate cycle)",
                        )
                        return
                except Exception as exc:
                    logger.error(
                        "kill_switch_bridge_active_retrip_failed (will retry "
                        "next evaluate cycle): %s",
                        exc,
                    )
                    return

        # Both trip and persistence succeeded AND the active manager sees
        # the trip. Stop retrying.
        self._durable_kill_switch_bridge_succeeded = True

    def _reemit_kill_switch_bridge_audit(
        self,
        *,
        record: Any,
        bridge_reason: str,
        deadline: Optional["_BridgeDeadline"] = None,
    ) -> bool:
        """Re-emit a kill-switch trip audit when the first ksm.trip call
        mutated the in-memory record but raised AFTER the mutation
        (Issue #258 round-2 P2 / Codex line 803). Without this, the
        retry loop sees the existing TRIPPED record on the second
        attempt, raises ValueError, falls through the post-trip race
        recheck, and marks the bridge succeeded — leaving the trip
        durably in-memory but with NO audit row.

        Returns ``True`` on success (audit JSONL write + best-effort
        Postgres dual-write completed without raising) and ``False``
        otherwise.

        **PR #258 round-3 P2 (Codex line 1608):** the caller MUST check
        the return value and refuse to mark
        ``_durable_kill_switch_bridge_succeeded = True`` when this
        returns False. Otherwise an unwritable ``AUDIT_LOG_PATH`` or
        equivalent visibility-blocking failure permanently suppresses
        the bridge while the trip still has no audit row, defeating
        the new fallback.

        **PR #258 round-3 P2 (Codex line 1573):** when ``deadline`` is
        supplied, this helper:
          1. Pre-checks ``deadline.is_exceeded()`` and aborts (returns
             False) before doing any work — so a near-budget bridge
             call does NOT start a fresh ``emit_audit_event`` whose
             nested ``connect_with_retry`` could blow the budget.
          2. Tightens the Postgres dual-write timeout by temporarily
             overriding ``POSTGRES_CONNECT_TIMEOUT_SECONDS`` to the
             remaining deadline (clamped to >=1) for the duration of
             the call. The JSONL write is local-disk and bounded; only
             the dual-write Postgres path needs deadline enforcement.
             The JSONL append IS the durable record of the audit; the
             Postgres dual-write is best-effort (see
             ``app/core/audit_log.py``'s ``_try_postgres_persist``
             which swallows every exception). So bounding the
             dual-write timeout protects the bridge's wall-clock
             without losing audit durability.
        """
        if deadline is not None and deadline.is_exceeded():
            # Pre-check: do not start audit re-emit if we're already
            # over budget. Caller will leave the bridge unsucceeded so
            # next evaluate cycle retries.
            logger.error(
                "kill_switch_bridge_audit_reemit_skipped_deadline_exceeded "
                "elapsed=%.2fs — trip is durable in-memory but audit row "
                "may be missing; will retry on next evaluate cycle.",
                deadline.elapsed(),
            )
            return False
        try:
            from app.core.audit_log import emit_audit_event
            from app.risk.kill_switch import KillSwitchState
        except Exception as exc:
            logger.error(
                "kill_switch_bridge_audit_reemit_unavailable: import "
                "failed: %s", exc,
            )
            return False

        # PR #258 round-3 P2 (Codex line 1573): tighten the Postgres
        # dual-write timeout via env override scoped to this call.
        # ``emit_audit_event`` -> ``_append_audit_event`` writes JSONL
        # FIRST (durable, local-disk) and THEN best-effort
        # ``_try_postgres_persist`` (swallows exceptions). The Postgres
        # path uses ``connect_with_retry`` with default 3 attempts ×
        # 5s connect = up to 15s, which can blow the bridge deadline.
        # Override the env var so that single call respects the
        # remaining bridge budget; restore afterwards.
        #
        # PR #258 round-5 P2 (Codex line 1845): the timeout clamp alone
        # is insufficient because ``connect_with_retry`` defaults to
        # ``max_attempts=3`` with 0.5/1.0s internal backoffs. Even with
        # the per-attempt timeout tightened, three attempts × candidates
        # × per-candidate-timeout + 1.5s backoff can blow the budget.
        # Also set ``AUDIT_POSTGRES_MAX_ATTEMPTS=1`` (honored by
        # ``app.core.audit_log._try_postgres_persist``) so the audit
        # dual-write makes exactly one attempt for the duration of this
        # call; restore afterwards.
        _POSTGRES_TIMEOUT_ENV = "POSTGRES_CONNECT_TIMEOUT_SECONDS"
        _AUDIT_PG_ATTEMPTS_ENV = "AUDIT_POSTGRES_MAX_ATTEMPTS"
        prev_pg_timeout: Optional[str] = None
        prev_audit_attempts: Optional[str] = None
        env_override_applied = False
        if deadline is not None:
            try:
                remaining = deadline.remaining()
                # Account for the (worst-case) DSN candidate count so
                # the Postgres-side total is bounded by remaining.
                candidate_count = _bridge_dsn_candidate_count()
                per_candidate = max(
                    1.0,
                    remaining / float(max(1, candidate_count)),
                )
                tight = max(1, int(per_candidate))
                prev_pg_timeout = os.environ.get(_POSTGRES_TIMEOUT_ENV)
                os.environ[_POSTGRES_TIMEOUT_ENV] = str(tight)
                prev_audit_attempts = os.environ.get(_AUDIT_PG_ATTEMPTS_ENV)
                os.environ[_AUDIT_PG_ATTEMPTS_ENV] = "1"
                env_override_applied = True
            except Exception:  # pragma: no cover - defensive
                pass

        success = False
        try:
            state_val = getattr(
                getattr(record, "state", None), "value", "TRIPPED",
            )
            scope_obj = getattr(record, "scope", None)
            scope_val = getattr(scope_obj, "value", "GLOBAL")
            scope_id_val = getattr(record, "scope_id", "GLOBAL")
            record_id_val = getattr(record, "id", "unknown")
            emit_audit_event(
                actor="risk_manager_auto",
                action="kill_switch.trip",
                resource_type="kill_switch",
                resource_id=str(record_id_val),
                before={
                    "state": KillSwitchState.INACTIVE.value,
                    "scope": scope_val,
                    "scope_id": scope_id_val,
                },
                after={
                    "state": state_val,
                    "scope": scope_val,
                    "scope_id": scope_id_val,
                },
                metadata={
                    "reason": bridge_reason,
                    "bridge_reemit": True,
                    "bridge_reemit_cause": (
                        "first_attempt_post_mutation_audit_failure"
                    ),
                },
            )
            logger.warning(
                "kill_switch_bridge_audit_reemitted record_id=%s "
                "scope=%s scope_id=%s — original ksm.trip mutated the "
                "in-memory record but raised before audit; audit row "
                "has been written via the bridge fallback path.",
                record_id_val, scope_val, scope_id_val,
            )
            success = True
        except Exception as exc:
            logger.error(
                "kill_switch_bridge_audit_reemit_failed: %s — trip is "
                "durable but the audit row is missing; bridge will "
                "NOT mark succeeded so the next evaluate cycle "
                "retries (PR #258 round-3 P2).", exc,
            )
            success = False
        finally:
            if env_override_applied:
                try:
                    if prev_pg_timeout is None:
                        os.environ.pop(_POSTGRES_TIMEOUT_ENV, None)
                    else:
                        os.environ[_POSTGRES_TIMEOUT_ENV] = prev_pg_timeout
                except Exception:  # pragma: no cover - defensive
                    pass
                try:
                    if prev_audit_attempts is None:
                        os.environ.pop(_AUDIT_PG_ATTEMPTS_ENV, None)
                    else:
                        os.environ[_AUDIT_PG_ATTEMPTS_ENV] = prev_audit_attempts
                except Exception:  # pragma: no cover - defensive
                    pass
        return success

    def _publish_legacy_kill_switch_state_to_hub(self) -> None:
        """Publish the in-memory legacy kill-switch flag to the hub runtime
        so /readyz and admin endpoints can detect divergence with the
        durable KillSwitchManager (issue #222).

        PR #234 round-1 review P2: ``get_hub_runtime`` is an
        ``@lru_cache(maxsize=1)`` singleton. Calling it from the tick
        path BEFORE the hub runtime has been initialised by
        ``AppRuntime.start`` would lazily construct the full hub from
        an unintended call site (the stream worker starts before the
        hub runtime is fetched in the normal startup order). To avoid
        that, only publish when the singleton is already present.
        """
        try:
            from app.hub.runtime import get_hub_runtime
            # Inspect the lru_cache without triggering construction.
            cache_info = getattr(get_hub_runtime, "cache_info", None)
            if not callable(cache_info) or cache_info().currsize == 0:
                return
            runtime = get_hub_runtime()
        except Exception:
            return
        recorder = getattr(runtime, "record_legacy_kill_switch_state", None)
        if not callable(recorder):
            return
        try:
            with self._state_lock:
                active = bool(self.kill_switch_activated)
            recorder(
                active=active,
                reason=(
                    "legacy_risk_manager_active" if active
                    else "legacy_risk_manager_inactive"
                ),
            )
        except Exception:
            # Defensive: never let publishing break account-loss eval.
            pass

    @staticmethod
    def _truncate_log_value(value: Any, limit: int = 512) -> Optional[str]:
        text = " ".join(str(value or "").split())
        if not text:
            return None
        if len(text) <= limit:
            return text
        return f"{text[: max(1, limit - 3)]}..."

    def _stoploss_skip_reason(
        self,
        *,
        label: str,
        require_position: bool,
    ) -> Optional[str]:
        with self._state_lock:
            position_exists = label in self.open_positions
            kill_switch_active = bool(self.kill_switch_activated)
        if kill_switch_active:
            return "kill_switch_active"
        if self._has_forced_exit_suppression(label):
            return "forced_exit_in_flight"
        if require_position and not position_exists:
            return "position_already_closed"
        return None

    def _log_stoploss_order_failure(
        self,
        *,
        label: str,
        strategy_name: Optional[str],
        stoploss_price: Optional[float],
        detail: Any,
        fallback_mode: Optional[str] = None,
    ) -> None:
        broker_status = None
        broker_url = None
        broker_content_type = None
        broker_body = None
        if isinstance(detail, (AngelHTTPBlockedError, AngelHTTPResponseError)):
            broker_status = getattr(detail, "status", None)
            broker_url = getattr(detail, "url", None)
            broker_content_type = getattr(detail, "content_type", None)
            broker_body = getattr(detail, "body_head", None)
        elif isinstance(detail, dict):
            broker_status = detail.get("terminal_status") or detail.get("status")
            broker_body = (
                detail.get("place_order")
                or detail.get("order_row")
                or detail
            )

        extras: Dict[str, Any] = {
            "label": label,
            "strategy_name": strategy_name or "UNKNOWN",
            "stoploss_price": stoploss_price,
            "fallback_mode": fallback_mode or "none",
            "error": self._truncate_log_value(detail, 1024),
        }
        truncated_body = self._truncate_log_value(broker_body, 1024)
        if broker_status is not None:
            extras["broker_status"] = broker_status
        if broker_url:
            extras["broker_url"] = broker_url
        if broker_content_type:
            extras["broker_content_type"] = broker_content_type
        if truncated_body:
            extras["broker_body"] = truncated_body
        log_event(
            logger,
            event_type="BRACKET_STOPLOSS_ORDER_ERROR",
            message="Stoploss order placement failed.",
            level=logging.WARNING,
            **extras,
        )

    @classmethod
    def _pick_nonzero_price(cls, *values: Any) -> Optional[float]:
        for value in values:
            price = cls._coerce_float(value)
            if price is None or price == 0.0:
                continue
            return price
        return None

    def _normalize_broker_order_status(self, value: Any) -> str:
        normalizer = getattr(self.order_client, "_normalize_order_status", None)
        if callable(normalizer):
            try:
                normalized = normalizer(value)
            except Exception:
                normalized = None
            if normalized:
                return str(normalized)
        raw = " ".join(str(value or "").strip().split()).lower()
        if not raw:
            return "UNKNOWN"
        if raw in ("complete", "completed", "filled", "fill"):
            return "COMPLETE"
        if raw in ("rejected", "reject"):
            return "REJECTED"
        if raw in ("cancelled", "canceled", "cancel"):
            return "CANCELLED"
        if raw in ("expired",):
            return "EXPIRED"
        if "trigger" in raw and "pending" in raw:
            return "PENDING"
        if raw in ("pending", "open", "put order req received", "validation pending"):
            return "PENDING"
        return raw.upper()

    def _pending_order_lot_size(self, ctx: Dict[str, Any]) -> float:
        label = str(ctx.get("label") or "")
        meta = self.instrument_meta.get(label, {})
        try:
            lot_size = float(ctx.get("lot_size") or meta.get("lot_size") or 1.0)
        except (TypeError, ValueError):
            lot_size = 1.0
        return lot_size if lot_size > 0 else 1.0

    def _pending_order_qty_from_row(
        self,
        *,
        ctx: Dict[str, Any],
        row: Dict[str, Any],
    ) -> int:
        qty = int(ctx.get("qty") or 0)
        filled_quantity = self._coerce_int(
            row.get("filledshares")
            or row.get("filled_qty")
            or row.get("filled_quantity")
            or row.get("filledQuantity")
        )
        if filled_quantity <= 0:
            return qty
        lot_size = self._pending_order_lot_size(ctx)
        normalized_qty = int(round(float(filled_quantity) / lot_size))
        return normalized_qty if normalized_qty > 0 else qty

    def _persist_execution_record_from_ctx(
        self,
        *,
        ctx: Dict[str, Any],
        qty: int,
        price: float,
        broker_order_id: str,
    ) -> None:
        if not self.trade_persister:
            return
        try:
            exec_ts = datetime.now(timezone.utc).isoformat()
            exec_record = {
                "timestamp": exec_ts,
                "label": ctx.get("label"),
                "underlying": ctx.get("underlying"),
                "strategy_name": ctx.get("strategy_name"),
                "template_name": ctx.get("template_name"),
                "reason": ctx.get("reason") or "pending_resolved",
                "side": ctx.get("side"),
                "qty": qty,
                "price": price,
                "exchange": ctx.get("exchange"),
                "symboltoken": ctx.get("symboltoken"),
                "tradingsymbol": ctx.get("tradingsymbol"),
                "trade_mode": self.current_trade_mode,
                "product_type": ctx.get("product_type"),
                "tag": ctx.get("tag"),
                "broker_order_id": broker_order_id,
            }
            exec_record["trade_id"] = "|".join(
                [
                    str(
                        ctx.get("symboltoken")
                        or ctx.get("tradingsymbol")
                        or ctx.get("label")
                    ),
                    str(ctx.get("side") or ""),
                    str(qty),
                    exec_ts,
                ]
            )
            self.trade_persister.record_execution(exec_record)
        except Exception as exc:
            logger.warning("Pending order execution persist failed: %s", exc)

    def _load_pending_order_row(
        self,
        order_id: str,
        *,
        order_index: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        details_row: Optional[Dict[str, Any]] = None
        try:
            details = self.order_client.get_order_details(str(order_id))
            if isinstance(details, dict):
                payload = details.get("data")
                if isinstance(payload, dict):
                    details_row = payload
        except Exception as exc:
            logger.debug(
                "Pending order reconcile: get_order_details failed order_id=%s err=%s",
                order_id,
                exc,
            )
        if details_row:
            return details_row
        if order_index is not None:
            return order_index.get(str(order_id))
        try:
            book = self.order_client.get_order_book()
        except Exception as exc:
            logger.debug(
                "Pending order reconcile: get_order_book failed order_id=%s err=%s",
                order_id,
                exc,
            )
            return None
        rows = book.get("data") if isinstance(book, dict) else None
        if not isinstance(rows, list):
            return None
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_order_id = row.get("orderid") or row.get("order_id") or row.get("orderId")
            if str(row_order_id or "") == str(order_id):
                return row
        return None

    def _build_closing_pending_order_ctx(
        self,
        *,
        label: str,
        side: str,
        qty: int,
        price: Optional[float],
        trigger_price: Optional[float],
        exchange: str,
        symboltoken: Any,
        tradingsymbol: str,
        tag: Optional[str],
        product_type: str,
    ) -> Dict[str, Any]:
        with self._state_lock:
            entry = dict(self.open_positions.get(label, {}))
        meta = self.instrument_meta.get(label, {})
        lot_size = float(entry.get("lot_size") or meta.get("lot_size") or 1.0)
        return {
            "label": label,
            "side": side,
            "qty": int(qty),
            "price": float(price or 0.0),
            "trigger_price": float(trigger_price)
            if trigger_price not in (None, 0, 0.0)
            else None,
            "template_name": entry.get("template_name"),
            "role": entry.get("role"),
            "underlying": entry.get("underlying") or self._infer_underlying(label),
            "strategy_name": entry.get("strategy_name"),
            "reason": entry.get("reason") or tag,
            "ml_info": entry.get("ml_info"),
            "count_for_spreads": False,
            "is_closing": True,
            "exchange": exchange,
            "symboltoken": symboltoken,
            "tradingsymbol": tradingsymbol,
            "tag": tag,
            "product_type": product_type,
            "lot_size": lot_size if lot_size > 0 else 1.0,
        }

    def _handle_confirmed_closing_order(
        self,
        *,
        response: Dict[str, Any],
        label: str,
        side: str,
        qty: int,
        price: Optional[float],
        trigger_price: Optional[float],
        exchange: str,
        symboltoken: Any,
        tradingsymbol: str,
        tag: Optional[str],
        product_type: str,
    ) -> Dict[str, Any]:
        terminal = str(response.get("terminal_status") or "UNKNOWN")
        order_id = str(response.get("order_id") or "")
        order_row = response.get("order_row") if isinstance(response, dict) else None
        ctx = self._build_closing_pending_order_ctx(
            label=label,
            side=side,
            qty=qty,
            price=price,
            trigger_price=trigger_price,
            exchange=exchange,
            symboltoken=symboltoken,
            tradingsymbol=tradingsymbol,
            tag=tag,
            product_type=product_type,
        )
        fill_price = float(
            self._pick_nonzero_price(
                order_row.get("averageprice") if isinstance(order_row, dict) else None,
                order_row.get("average_price") if isinstance(order_row, dict) else None,
                order_row.get("avgprice") if isinstance(order_row, dict) else None,
                order_row.get("averagePrice") if isinstance(order_row, dict) else None,
                order_row.get("tradedprice") if isinstance(order_row, dict) else None,
                order_row.get("traded_price") if isinstance(order_row, dict) else None,
                order_row.get("fillprice") if isinstance(order_row, dict) else None,
                order_row.get("filledprice") if isinstance(order_row, dict) else None,
                order_row.get("price") if isinstance(order_row, dict) else None,
                price,
                trigger_price,
                0.0,
            )
            or 0.0
        )
        if terminal == "COMPLETE":
            self._persist_execution_record_from_ctx(
                ctx=ctx,
                qty=int(qty),
                price=fill_price,
                broker_order_id=order_id,
            )
            self._register_exit(label, fill_price, int(qty))
            return {"terminal": terminal, "closed": True, "order_id": order_id}
        if terminal in {"PENDING", "UNKNOWN"} and order_id:
            self._record_pending_order(order_id=order_id, ctx=ctx)
        return {"terminal": terminal, "closed": False, "order_id": order_id}

    # Attach a maintenance manager for heartbeat and kill-switch checks.
    def attach_maintenance(self, maintenance: MaintenanceManager) -> None:
        self.maintenance = maintenance

    # Register an optional hub-routed submitter for forced/safety exits.
    def set_hub_exit_submitter(
        self, submitter: Optional[Callable[..., bool]]
    ) -> None:
        self._hub_exit_submitter = submitter

    def set_operating_mode(self, mode: str) -> None:
        """Set the resolved operating mode string for authority-path enforcement."""
        self._operating_mode = str(mode or "")

    def _assert_direct_submit_allowed(
        self, *, trade_mode: str, exit_source: str, label: str
    ) -> None:
        """Block direct broker submit fallback in hub-authoritative LIVE.

        Raises P0RuleViolation if the operating mode is HUB_AUTHORITATIVE
        and trade_mode is LIVE, since only the hub should place orders.
        """
        if str(trade_mode or "").upper() != "LIVE":
            return
        if not self._operating_mode:
            return
        assert_legacy_exit_allowed(
            operating_mode=self._operating_mode,
            is_break_glass=False,
            exit_source=exit_source,
            scope_key=label,
        )

    # Attempt to submit an EXIT order through the optional hub exit hook.
    def _submit_exit_via_hub(
        self,
        *,
        label: str,
        strategy_name: Optional[str],
        exchange: str,
        symboltoken: str,
        tradingsymbol: str,
        side: str,
        quantity: int,
        trade_mode: str,
        tag: Optional[str],
        reason: Optional[str],
    ) -> bool:
        submitter = self._hub_exit_submitter
        if submitter is None:
            return False
        if str(trade_mode or "").upper() != "LIVE":
            return False
        if quantity <= 0:
            return False
        try:
            return bool(
                submitter(
                    label=label,
                    strategy_name=strategy_name,
                    exchange=exchange,
                    symboltoken=str(symboltoken or ""),
                    tradingsymbol=tradingsymbol,
                    side=str(side or "").upper(),
                    quantity=int(quantity),
                    trade_mode=str(trade_mode or ""),
                    tag=tag,
                    reason=reason,
                )
            )
        except Exception as exc:
            logger.warning(
                "Hub exit submitter failed for %s (%s): %s",
                label,
                strategy_name or "unknown",
                exc,
            )
            return False

    def _maybe_migrate_legacy_state(self) -> None:
        """One-time migration: move legacy app/config/risk_positions.json to canonical path."""
        if self.state_path == _LEGACY_STATE_PATH:
            return
        if self.state_path.exists():
            return
        if not _LEGACY_STATE_PATH.exists():
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            _LEGACY_STATE_PATH.rename(self.state_path)
            logger.warning(
                "startup.risk_state_migrated: moved legacy risk state from %s to %s",
                _LEGACY_STATE_PATH,
                self.state_path,
            )
        except Exception as exc:
            logger.warning(
                "startup.risk_state_migration_failed: could not move %s to %s: %s",
                _LEGACY_STATE_PATH,
                self.state_path,
                exc,
            )

    # Load persisted risk state from disk if available.
    def _load_state(self) -> None:
        candidate_path = self.state_path
        if not candidate_path.exists():
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            return
        if not candidate_path.exists():
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            return
        try:
            data = json.loads(candidate_path.read_text(encoding="utf-8"))
        except Exception:
            data = None
        if data is None:
            bak_path = candidate_path.with_suffix(candidate_path.suffix + ".bak")
            try:
                if bak_path.exists():
                    data = json.loads(bak_path.read_text(encoding="utf-8"))
                    logger.warning("Loaded risk state from backup: %s", bak_path)
            except Exception:
                return
        if data is None:
            return
        with self._state_lock:
            self.realized_pnl = float(data.get("realized_pnl", 0.0))
            self.max_equity = float(data.get("max_equity", self.realized_pnl))
            self.daily_realized_pnl = float(data.get("daily_realized_pnl", 0.0))
            self.daily_peak_equity = float(
                data.get("daily_peak_equity", self.daily_realized_pnl)
            )
            self.daily_peak_total_pnl = float(
                data.get(
                    "daily_peak_total_pnl",
                    data.get("daily_peak_equity", self.daily_realized_pnl),
                )
            )
            self.last_unrealized_pnl = float(data.get("last_unrealized_pnl", 0.0))
            self.last_total_pnl = float(
                data.get(
                    "last_total_pnl",
                    self.daily_realized_pnl + self.last_unrealized_pnl,
                )
            )
            ks_date = data.get("kill_switch_date")
            try:
                self.kill_switch_date = (
                    datetime.fromisoformat(ks_date).date() if ks_date else None
                )
            except Exception:
                self.kill_switch_date = None
            self.kill_switch_activated = bool(data.get("kill_switch_activated", False))
            for underlying, spreads in data.get("open_spreads", {}).items():
                for template, labels in spreads.items():
                    self.open_spreads_by_underlying.setdefault(underlying, {})[
                        template
                    ] = set(labels)
            self.open_positions = data.get("open_positions", {})

    # Persist current risk state to disk.
    def _persist_state(self, *, force: bool = True) -> None:
        now_mono = time.monotonic()
        if (
            not force
            and (now_mono - self._last_state_persist_mono)
            < self._state_persist_min_interval_seconds
        ):
            return
        with self._state_lock:
            payload = {
                "realized_pnl": self.realized_pnl,
                "open_spreads": {
                    underlying: {tpl: list(labels) for tpl, labels in templates.items()}
                    for underlying, templates in self.open_spreads_by_underlying.items()
                },
                "open_positions": dict(self.open_positions),
                "max_equity": self.max_equity,
                "daily_realized_pnl": self.daily_realized_pnl,
                "daily_peak_equity": self.daily_peak_equity,
                "daily_peak_total_pnl": self.daily_peak_total_pnl,
                "last_unrealized_pnl": self.last_unrealized_pnl,
                "last_total_pnl": self.last_total_pnl,
                "kill_switch_activated": self.kill_switch_activated,
                "kill_switch_date": (
                    self.kill_switch_date.isoformat() if self.kill_switch_date else None
                ),
            }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            bak_path = self.state_path.with_suffix(self.state_path.suffix + ".bak")
            serialized = json.dumps(payload, indent=2)
            if self.state_path.exists():
                try:
                    bak_path.write_text(
                        self.state_path.read_text(encoding="utf-8"),
                        encoding="utf-8",
                    )
                except Exception:
                    pass
            self.state_path.write_text(serialized, encoding="utf-8")
            self._last_state_persist_mono = now_mono
        except Exception as exc:  # pragma: no cover
            logger.error("Failed to persist risk state: %s", exc)

    # Infer underlying name from instrument metadata.
    def _infer_underlying(self, label: str) -> Optional[str]:
        meta = self.instrument_meta.get(label)
        if not meta:
            return None
        return meta.get("underlying")

    # Resolve an underlying label for the given instrument label.
    def _underlying_label_for_label(self, label: str) -> str:
        meta = self.instrument_meta.get(label, {})
        kind = meta.get("kind")
        if kind == "UNDERLYING":
            return label
        underlying_name = str(meta.get("underlying") or "")
        if underlying_name and underlying_name in self._underlying_name_to_label:
            return self._underlying_name_to_label[underlying_name]
        # Build mapping lazily
        for m_label, m_meta in self.instrument_meta.items():
            if m_meta.get("kind") == "UNDERLYING":
                name = str(m_meta.get("underlying") or "")
                if name:
                    self._underlying_name_to_label[name] = m_label
        return self._underlying_name_to_label.get(underlying_name, label)

    # Reset daily counters when a new trading day starts.
    def _reset_daily_if_new_day(self, now: datetime) -> bool:
        now_date = now.astimezone(IST).date()
        if self.kill_switch_date == now_date:
            return False
        # New day: reset daily counters and kill switch
        with self._state_lock:
            self._forced_exit_suppression_until.clear()
        self.kill_switch_date = now_date
        self.realized_pnl = 0.0
        self.max_equity = 0.0
        self.daily_realized_pnl = 0.0
        self.daily_peak_equity = 0.0
        self.daily_peak_total_pnl = 0.0
        self.last_unrealized_pnl = 0.0
        self.last_total_pnl = 0.0
        self.kill_switch_activated = False
        # Issue #218 (PR #231 review): a fresh trading day is a new
        # legacy-auto-trip episode. Reset the bridge-success tracker so a
        # new day's first auto-trip is propagated to the durable manager
        # rather than skipped on the stale "already succeeded yesterday"
        # signal.
        self._durable_kill_switch_bridge_succeeded = False
        # PR #258 round-3 P2 (Codex line 1266): a stale pending-reemit
        # marker from yesterday's session has no semantic meaning today
        # (durable record is rolled by operator action, not by us). Reset
        # alongside the success flag so a new day starts clean.
        self._pending_audit_reemit = False
        self._pending_audit_reemit_reason = None
        self._publish_account_loss_guard(source="new_day_reset")
        return True

    def _compute_open_unrealized_pnl(self) -> float:
        with self._state_lock:
            positions_snapshot = {
                str(label): dict(entry)
                for label, entry in self.open_positions.items()
                if isinstance(entry, dict)
            }
        unrealized_total = 0.0
        for label, entry in positions_snapshot.items():
            qty = float(entry.get("qty") or 0.0)
            if qty <= 0:
                continue
            side = str(entry.get("side") or "SELL").strip().upper()
            entry_price = float(entry.get("entry_price") or 0.0)
            meta = self.instrument_meta.get(label, {})
            lot_size = float(entry.get("lot_size") or meta.get("lot_size") or 1.0)
            last_price = dashboard_bus.get_last_price(label)
            if last_price is None:
                # Try instrument symbol/token lookup as fallback
                tradingsymbol = entry.get("tradingsymbol") or entry.get("symbol")
                symboltoken = entry.get("symboltoken") or entry.get("symbol_token")
                if tradingsymbol or symboltoken:
                    _fn = getattr(dashboard_bus, "get_last_price_for_instrument", None)
                    if callable(_fn):
                        last_price = _fn(symbol=tradingsymbol, token=symboltoken)
            if last_price is None:
                last_price = float(entry.get("avg_price", 0.0) or 0.0)
                if last_price > 0:
                    logger.warning(
                        "[RISK] kill_switch_mark_stale: label=%s using broker avg_price=%s "
                        "— stream mark unavailable. Drawdown may be inaccurate.",
                        label, last_price,
                    )
            if last_price is None:
                last_price = entry_price
            side_mult = 1.0 if side == "BUY" else -1.0
            unrealized_total += (
                (float(last_price) - entry_price) * side_mult * qty * lot_size
            )
        return float(unrealized_total)

    @staticmethod
    def _position_net_qty_for_flat_check(position: Any) -> Optional[int]:
        for key in ("netqty", "net", "quantity", "qty", "net_qty"):
            if isinstance(position, dict):
                value = position.get(key)
            else:
                value = getattr(position, key, None)
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return None
        return None

    def _load_hub_authoritative_flat_account_pnl(
        self,
    ) -> Optional[_AuthoritativeAccountPnL]:
        """Use hub PnL only when LIVE hub mode and broker positions are flat."""
        if str(self.current_trade_mode or "").strip().upper() != "LIVE":
            return None
        if str(self._operating_mode or "").strip().upper() not in {
            "HUB",
            "HUB_AUTHORITATIVE",
        }:
            return None
        if not self.tenant_id or not self.broker_account_id:
            return None
        with self._state_lock:
            if self.open_positions:
                return None
        try:
            from app.hub.runtime import get_hub_runtime

            cache_info = getattr(get_hub_runtime, "cache_info", None)
            if not callable(cache_info) or cache_info().currsize == 0:
                return None
            runtime = get_hub_runtime()
        except Exception:
            return None

        state_store = getattr(runtime, "state_store", None)
        get_positions = getattr(state_store, "get_positions", None)
        if not callable(get_positions):
            return None
        try:
            broker_positions = get_positions(self.broker_account_id) or []
        except Exception as exc:
            logger.warning(
                "risk_manager.authoritative_pnl_unavailable: account=%s "
                "position_store_read_failed=%s",
                self.broker_account_id,
                exc,
            )
            return None
        for position in broker_positions:
            net_qty = self._position_net_qty_for_flat_check(position)
            if net_qty is None or net_qty != 0:
                return None

        pnl_engine = getattr(runtime, "pnl_engine", None)
        pnl_state_store = getattr(pnl_engine, "_state_store", None)
        list_snapshots = getattr(pnl_state_store, "list_account_snapshots", None)
        if not callable(list_snapshots):
            return None
        try:
            snapshots = list_snapshots(
                tenant_id=TenantId(self.tenant_id),
                broker_account_id=BrokerAccountId(self.broker_account_id),
            )
        except Exception as exc:
            logger.warning(
                "risk_manager.authoritative_pnl_unavailable: account=%s "
                "pnl_snapshot_read_failed=%s",
                self.broker_account_id,
                exc,
            )
            return None
        if not snapshots:
            return None
        for snapshot in snapshots:
            try:
                net_open_qty = int(float(getattr(snapshot, "net_open_qty", 0) or 0))
                control_open_qty = int(
                    float(getattr(snapshot, "control_open_qty", 0) or 0)
                )
            except (TypeError, ValueError):
                return None
            if net_open_qty != 0 or control_open_qty != 0:
                return None

        get_realized = getattr(pnl_engine, "get_display_realized_pnl_account", None)
        if not callable(get_realized):
            return None
        try:
            tenant_id = TenantId(self.tenant_id)
            broker_account_id = BrokerAccountId(self.broker_account_id)
            realized_pnl = float(get_realized(tenant_id, broker_account_id))
        except Exception as exc:
            logger.warning(
                "risk_manager.authoritative_pnl_unavailable: account=%s "
                "pnl_read_failed=%s",
                self.broker_account_id,
                exc,
            )
            return None
        return _AuthoritativeAccountPnL(
            realized_pnl=realized_pnl,
            unrealized_pnl=0.0,
            total_pnl=realized_pnl,
            source="hub_pnl_engine_flat_account",
        )

    def evaluate_account_loss(
        self,
        *,
        now: Optional[datetime] = None,
        source: str = "runtime",
        force: bool = False,
    ) -> Dict[str, Any]:
        now_ts = now or datetime.now(timezone.utc)
        # Issue #222 (PR #234 review P3): _reset_daily_if_new_day can
        # flip kill_switch_activated from True to False. Publish the
        # post-reset state to the hub BEFORE the throttle-skip return
        # below — otherwise the hub keeps reporting the stale (yesterday's)
        # legacy_active=True until a forced or unthrottled evaluation
        # later in the day, surfacing a phantom divergence in /readyz.
        self._reset_daily_if_new_day(now_ts)
        self._publish_legacy_kill_switch_state_to_hub()
        now_mono = time.monotonic()
        if (
            not force
            and (now_mono - self._last_account_loss_eval_mono)
            < self._account_loss_eval_interval_seconds
        ):
            with self._state_lock:
                return {
                    "kill_switch_activated": bool(self.kill_switch_activated),
                    "daily_realized_pnl": float(self.daily_realized_pnl),
                    "unrealized_pnl": float(self.last_unrealized_pnl),
                    "daily_total_pnl": float(self.last_total_pnl),
                    "daily_peak_equity": float(self.daily_peak_equity),
                    "daily_peak_total_pnl": float(self.daily_peak_total_pnl),
                }
        self._last_account_loss_eval_mono = now_mono
        legacy_unrealized_pnl = self._compute_open_unrealized_pnl()
        authoritative_pnl = self._load_hub_authoritative_flat_account_pnl()
        authoritative_source: Optional[str] = None
        with self._state_lock:
            prior_realized_pnl = float(self.daily_realized_pnl)
            prior_realized_peak = float(self.daily_peak_equity)
            prior_total_peak = float(self.daily_peak_total_pnl)
            prior_unrealized = float(self.last_unrealized_pnl)
            prior_total = float(self.last_total_pnl)

            if authoritative_pnl is not None:
                realized_pnl = float(authoritative_pnl.realized_pnl)
                unrealized_pnl = float(authoritative_pnl.unrealized_pnl)
                total_pnl = float(authoritative_pnl.total_pnl)
                authoritative_source = authoritative_pnl.source
                self.realized_pnl = realized_pnl
                self.daily_realized_pnl = realized_pnl
            else:
                realized_pnl = float(self.daily_realized_pnl)
                unrealized_pnl = float(legacy_unrealized_pnl)
                total_pnl = float(realized_pnl + unrealized_pnl)
            self.last_unrealized_pnl = float(unrealized_pnl)
            self.last_total_pnl = float(total_pnl)
            self.daily_peak_equity = max(float(self.daily_peak_equity), realized_pnl)
            self.daily_peak_total_pnl = max(
                float(self.daily_peak_total_pnl),
                realized_pnl,
                total_pnl,
            )
            realized_peak = float(self.daily_peak_equity)
            total_peak = float(self.daily_peak_total_pnl)

            realized_drawdown = max(0.0, realized_peak - realized_pnl)
            total_drawdown = max(0.0, total_peak - total_pnl)

            loss_limit = abs(float(self.max_daily_loss or 0.0))
            drawdown_limit = abs(float(self.max_intraday_drawdown or 0.0))
            realized_loss_hit = loss_limit > 0.0 and realized_pnl <= -loss_limit
            floating_loss_hit = (
                self._include_unrealized_in_kill_switch
                and loss_limit > 0.0
                and total_pnl <= -loss_limit
            )
            realized_drawdown_hit = (
                drawdown_limit > 0.0 and realized_drawdown >= drawdown_limit
            )
            floating_drawdown_hit = (
                self._include_unrealized_in_kill_switch
                and drawdown_limit > 0.0
                and total_drawdown >= drawdown_limit
            )
            should_activate = not self.kill_switch_activated and (
                realized_loss_hit
                or floating_loss_hit
                or realized_drawdown_hit
                or floating_drawdown_hit
            )
            if should_activate:
                self.kill_switch_activated = True
                self.kill_switch_date = now_ts.astimezone(IST).date()
            has_open_positions = bool(self.open_positions)
            open_labels = list(self.open_positions.keys()) if has_open_positions else []

        state_changed = (
            abs(prior_realized_pnl - realized_pnl) > 0.0001
            or abs(prior_realized_peak - realized_peak) > 0.0001
            or abs(prior_total_peak - total_peak) > 0.0001
            or abs(prior_unrealized - unrealized_pnl) > 0.0001
            or abs(prior_total - total_pnl) > 0.0001
        )
        if (
            authoritative_source is not None
            and abs(prior_realized_pnl - realized_pnl) > 0.0001
        ):
            logger.warning(
                "risk_manager.authoritative_pnl_override: account=%s source=%s "
                "legacy_daily_realized=%.2f authoritative_realized=%.2f "
                "authoritative_total=%.2f",
                self.broker_account_id,
                authoritative_source,
                prior_realized_pnl,
                realized_pnl,
                total_pnl,
            )
        self._publish_account_loss_guard(
            source=source,
            unrealized_pnl=unrealized_pnl,
            total_pnl=total_pnl,
        )
        # Issue #222: publish current legacy kill-switch state to the hub
        # runtime so /readyz and admin endpoints can detect divergence
        # with the durable KillSwitchManager. Called every evaluate cycle
        # (including when False) so the timestamp stays fresh and the
        # publisher is observable as live.
        self._publish_legacy_kill_switch_state_to_hub()
        if should_activate:
            ltp_lookup = {
                label: float(price)
                for label in open_labels
                for price in [dashboard_bus.get_last_price(label)]
                if price is not None
            }
            hit_reasons = []
            if realized_loss_hit:
                hit_reasons.append("realized_loss")
            if floating_loss_hit:
                hit_reasons.append("floating_loss")
            if realized_drawdown_hit:
                hit_reasons.append("realized_drawdown")
            if floating_drawdown_hit:
                hit_reasons.append("floating_drawdown")
            logger.error(
                "[RISK] Kill-switch activated: realized=%.2f unrealized=%.2f total=%.2f realized_dd=%.2f total_dd=%.2f (loss_lim=%.2f, dd_lim=%.2f) evaluation_source=%s open_positions=%s reasons=%s",
                realized_pnl,
                unrealized_pnl,
                total_pnl,
                realized_drawdown,
                total_drawdown,
                self.max_daily_loss,
                self.max_intraday_drawdown,
                source,
                ",".join(open_labels) or "none",
                ",".join(hit_reasons) or "unknown",
            )
            # Issue #218: bridge legacy auto-trip to the durable hub-authoritative
            # KillSwitchManager. Without this, the hub OrderRouter keeps allowing
            # entries until an operator manually trips. Failure-safe and idempotent.
            self._propagate_kill_switch_to_durable_manager(
                reasons=hit_reasons, source=source,
            )
            # Issue #222 (PR #234 round-3 review P2): the bridge call
            # above can construct the hub runtime singleton on first use
            # in early startup (before the regular tick path has built
            # it). Republish legacy state HERE so the freshly-built hub
            # observes ``legacy_active=True`` immediately — otherwise
            # /readyz reports ``publisher_seen=false`` / ``legacy_active=
            # false`` for the exact startup bridge-failure case the
            # divergence detector is meant to surface.
            self._publish_legacy_kill_switch_state_to_hub()
            if self.kill_switch_square_off_open_positions and has_open_positions:
                try:
                    self.square_off_all(
                        trade_mode=self.current_trade_mode,
                        ltp_lookup=ltp_lookup or None,
                        reason="KILL_SWITCH_ACCOUNT_LOSS",
                        tag="KILL_SWITCH_EXIT",
                    )
                except Exception as exc:
                    logger.error("Kill-switch square-off failed: %s", exc)
            self._persist_state()
        elif state_changed:
            self._persist_state(force=False)
        # Issue #218 (PR #231 review): retry the durable-bridge propagation
        # on SUBSEQUENT account-loss evaluations after the initial attempt
        # failed. The first attempt happens inside the ``if should_activate:``
        # block above (one-shot per legacy auto-trip episode). If that
        # initial attempt did not fully succeed (transient hub-runtime or
        # Postgres outage), the legacy flag is set but the durable manager
        # is INACTIVE — the very gap this PR closes. The retry below fires
        # ONLY when ``should_activate`` was False this cycle (i.e. the
        # legacy flag was already True from a prior cycle), so the same
        # cycle does not run the bridge twice.
        if (
            not should_activate
            and self.kill_switch_activated
            and not self._durable_kill_switch_bridge_succeeded
        ):
            self._propagate_kill_switch_to_durable_manager(
                reasons=["legacy_already_tripped_retry"], source=source,
            )
            # PR #234 round-3 review P2: republish after the retry path
            # too — same rationale as above (the retry can lazily build
            # the hub runtime; republish so it observes the active
            # legacy halt immediately).
            self._publish_legacy_kill_switch_state_to_hub()
        return {
            "kill_switch_activated": bool(self.kill_switch_activated),
            "daily_realized_pnl": float(realized_pnl),
            "unrealized_pnl": float(unrealized_pnl),
            "daily_total_pnl": float(total_pnl),
            "daily_peak_equity": float(realized_peak),
            "daily_peak_total_pnl": float(total_peak),
            "realized_drawdown": float(realized_drawdown),
            "total_drawdown": float(total_drawdown),
        }

    # Get risk limit settings for a specific underlying.
    def _get_limits_for_underlying(
        self, underlying: Optional[str]
    ) -> Dict[str, Optional[float]]:
        limits: Dict[str, Optional[float]] = {
            "max_lots_per_trade": None,
            "max_open_lots": None,
            "max_notional_per_underlying": None,
        }
        cfg = self.risk_limits or {}
        default_cfg = cfg.get("default", {}) if isinstance(cfg, dict) else {}
        per_cfg = (cfg.get("per_underlying", {}) or {}) if isinstance(cfg, dict) else {}
        if isinstance(default_cfg, dict):
            limits.update({k: default_cfg.get(k) for k in limits})
        key = (underlying or "").upper()
        if (
            key
            and isinstance(per_cfg, dict)
            and key in per_cfg
            and isinstance(per_cfg[key], dict)
        ):
            for k in limits:
                if per_cfg[key].get(k) is not None:
                    limits[k] = per_cfg[key][k]
        return limits

    # Compute total open lots for a given underlying.
    def _get_current_open_lots(self, underlying: Optional[str]) -> float:
        if not underlying:
            return 0.0
        total_lots = 0.0
        with self._state_lock:
            for label, entry in self.open_positions.items():
                entry_under = (entry.get("underlying") or "").upper()
                if not entry_under:
                    meta = self.instrument_meta.get(label, {})
                    entry_under = str(meta.get("underlying") or "").upper()
                if not entry_under and label.upper().startswith((underlying or "").upper()):
                    entry_under = (underlying or "").upper()
                if entry_under != underlying.upper():
                    continue
                qty = abs(float(entry.get("qty", 0)))
                total_lots += qty
        return total_lots

    # Build and log a risk decision.
    def _make_decision(
        self,
        *,
        allowed: bool,
        reason: str,
        label: str,
        side: str,
        qty: int,
        strategy_name: Optional[str],
        template_name: Optional[str],
    ) -> RiskDecision:
        status = "ACCEPTED" if allowed else "REJECTED"
        log_event(
            logger,
            event_type="LEGACY_RISK_DECISION",
            message=reason,
            level=logging.INFO if allowed else logging.WARNING,
            label=label,
            side=side,
            qty=qty,
            strategy_name=strategy_name,
            template_name=template_name,
            allowed=allowed,
            status=status,
        )
        return RiskDecision(allowed=allowed, status=status, reason=reason)

    # Check whether a new spread can be opened for an underlying.
    def _can_enter(
        self, underlying: Optional[str], template_name: Optional[str]
    ) -> bool:
        if not underlying:
            return True
        # Select per-underlying override if configured.
        max_allowed = self.per_underlying_max_open_spreads.get(
            (underlying or "").upper(), self.max_open_spreads
        )
        if max_allowed and max_allowed > 0:
            with self._state_lock:
                open_spreads = dict(self.open_spreads_by_underlying.get(underlying, {}))
            # Allow adding legs to an existing spread
            if template_name in open_spreads:
                return True
            current = len(open_spreads)
            allowed = current < max_allowed
            if not allowed:
                logger.warning(
                    "RiskManager blocking spread for %s (max %d reached)",
                    underlying,
                    max_allowed,
                )
            return allowed
        return True

    # Record a new position entry and update trackers.
    def _register_entry(
        self,
        label: str,
        side: str,
        qty: int,
        price: float,
        template_name: str,
        role: Optional[str],
        underlying: Optional[str],
        strategy_name: Optional[str] = None,
        reason: Optional[str] = None,
        ml_info: Optional[Dict[str, Any]] = None,
        count_for_spreads: bool = True,
        strategy_context: Optional[Dict[str, Any]] = None,
    ) -> None:
        template_key = template_name or "__UNKNOWN__"
        underlying = underlying or self._infer_underlying(label)
        meta = self.instrument_meta.get(label, {})
        exchange = meta.get("exchange", "NSE")
        symboltoken = meta.get("token")
        tradingsymbol = meta.get("symbol", label)
        lot_size = float(meta.get("lot_size") or 1)
        with self._state_lock:
            if count_for_spreads:
                self.open_spreads_by_underlying.setdefault(
                    underlying or "__UNKNOWN__", {}
                ).setdefault(template_key, set()).add(label)
            self.open_positions[label] = {
                "side": side,
                "qty": qty,
                "entry_price": price,
                "template_name": template_key,
                "role": role,
                "underlying": underlying,
                "strategy_name": strategy_name,
                "reason": reason,
                "entry_ts": datetime.now(timezone.utc).isoformat(),
                "exchange": exchange,
                "symboltoken": symboltoken,
                "tradingsymbol": tradingsymbol,
                "lot_size": lot_size,
            }
            if ml_info:
                self.open_positions[label]["ml_info"] = ml_info
            if strategy_context:
                self.open_positions[label]["strategy_context"] = dict(strategy_context)
            entry_ts = self.open_positions[label]["entry_ts"]
        # Ensure the executed token stays subscribed for live LTP updates
        try:
            executed_tokens_tracker.register_position(label, meta)
        except Exception as exc:  # safety net, do not break trading on tracker errors
            logger.warning(
                "ExecutedTokensTracker.register_position failed for %s: %s",
                label,
                exc,
            )
        dashboard_bus.add_position(
            label,
            side=side,
            qty=qty,
            entry_price=price,
            lot_size=lot_size,
            template_name=template_name,
            role=role,
            underlying=underlying,
            strategy_name=strategy_name,
            reason=reason,
            entry_ts=entry_ts,
        )
        logger.info(
            "[PNL_ENTRY] position_added | label=%s side=%s qty=%d entry_price=%.2f template=%s underlying=%s",
            label,
            side,
            qty,
            price,
            template_name,
            underlying,
        )

        if self._is_recovery_owned_entry(
            template_name=template_name,
            strategy_name=strategy_name,
            reason=reason,
        ):
            logger.info(
                "[BRACKET] Skipping broker bracket orders for %s due to recovery-owned position [%s]",
                label,
                strategy_name or template_name or "UNKNOWN",
            )
            return

        if not self._broker_brackets_enabled(strategy_context):
            logger.info(
                "[BRACKET] Skipping broker bracket orders for %s due to strategy-managed exit ownership [%s]",
                label,
                strategy_name or "UNKNOWN",
            )
            return

        # Automatically place target and stoploss orders after entry fills
        # Use strategy-specific exit parameters if available
        try:
            self._place_bracket_orders_async(
                label=label,
                side=side,
                qty=qty,
                entry_price=price,
                strategy_name=strategy_name,
                strategy_context=strategy_context,
            )
        except Exception as exc:
            logger.warning(
                "Failed to place bracket orders for %s: %s",
                label,
                exc,
            )

    # Apply broker position updates with lock protection.
    def update_position_from_broker(
        self,
        *,
        label: str,
        side: str,
        qty: int,
        entry_price: float,
        exchange: Optional[str] = None,
        symboltoken: Optional[str] = None,
        tradingsymbol: Optional[str] = None,
        template_name: Optional[str] = None,
        strategy_name: Optional[str] = None,
        strategy_context: Optional[Dict[str, Any]] = None,
    ) -> None:
        meta = self.instrument_meta.get(label, {})
        with self._state_lock:
            existing = dict(self.open_positions.get(label, {}))
            existing["side"] = side
            existing["qty"] = qty
            existing["entry_price"] = entry_price
            if template_name is not None and str(template_name).strip():
                existing["template_name"] = str(template_name).strip()
            if exchange is not None:
                existing["exchange"] = exchange
            if symboltoken is not None:
                existing["symboltoken"] = symboltoken
            if tradingsymbol is not None:
                existing["tradingsymbol"] = tradingsymbol
            if strategy_name is not None and str(strategy_name).strip():
                existing["strategy_name"] = str(strategy_name).strip()
            if strategy_context:
                existing["strategy_context"] = dict(strategy_context)
            if existing.get("lot_size") in (None, 0, 0.0):
                existing["lot_size"] = float(meta.get("lot_size") or 1)
            existing.setdefault("entry_ts", datetime.now(timezone.utc).isoformat())
            self.open_positions[label] = existing

            template_name = existing.get("template_name")
            role = existing.get("role")
            underlying = existing.get("underlying")
            strategy_name = existing.get("strategy_name")
            reason = existing.get("reason")
            entry_ts = existing.get("entry_ts")

        try:
            dashboard_bus.add_position(
                label,
                side=side,
                qty=qty,
                entry_price=entry_price,
                lot_size=existing.get("lot_size"),
                template_name=template_name,
                role=role,
                underlying=underlying,
                strategy_name=strategy_name,
                reason=reason,
                entry_ts=entry_ts,
            )
        except Exception:
            pass
        self._persist_state()

    # Place target/stop orders in a background thread after entry fills.
    def _place_bracket_orders_async(
        self,
        label: str,
        side: str,
        qty: int,
        entry_price: float,
        strategy_name: Optional[str] = None,
        strategy_context: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Place target profit and stoploss orders as bracket orders after entry fills.
        
        Uses strategy-specific exit parameters if available, otherwise falls back to
        environment variables. This ensures each strategy uses its own configured
        risk management parameters rather than global settings.
        
        Args:
            label: Instrument label
            side: BUY or SELL
            qty: Position quantity
            entry_price: Entry order price
            strategy_name: Strategy name for logging
            strategy_context: Strategy configuration dict with target_pct/tp_pct and sl_pct
        """
        # Run in background thread to avoid blocking the main reconciliation loop
        # Place target and stoploss orders with strategy-specific parameters.
        def _place_exits():
            try:
                with self._state_lock:
                    position_was_tracked = label in self.open_positions
                    kill_switch_active = bool(self.kill_switch_activated)
                if kill_switch_active or self._has_forced_exit_suppression(label):
                    if not position_was_tracked or kill_switch_active:
                        logger.debug(
                            "[BRACKET] Skipping bracket orders for %s because a forced exit or kill switch is active",
                            label,
                        )
                        return
                meta = self.instrument_meta.get(label, {})
                exchange = meta.get("exchange", "NSE")
                symboltoken = meta.get("token")
                tradingsymbol = meta.get("symbol", label)
                lot_size = float(meta.get("lot_size") or 1)
                tick_size = self._resolve_tick_size(meta)
                if lot_size <= 0:
                    lot_size = 1.0
                order_qty_units = int(round(float(qty) * lot_size)) if qty else 0
                if order_qty_units <= 0:
                    logger.debug(
                        "[BRACKET] Skipping orders for %s due to invalid qty: lots=%s lot_size=%s",
                        label,
                        qty,
                        lot_size,
                    )
                    return
                
                # Determine target profit and stoploss settings.
                target_ratio = None
                stoploss_ratio = None
                if strategy_context:
                    for key in ("target_pct", "tp_pct"):
                        if strategy_context.get(key) is not None:
                            target_ratio = self._normalize_strategy_ratio(
                                strategy_context.get(key)
                            )
                            break
                    for key in ("sl_pct", "stoploss_pct", "stop_loss_pct"):
                        if strategy_context.get(key) is not None:
                            stoploss_ratio = self._normalize_strategy_ratio(
                                strategy_context.get(key)
                            )
                            break

                if target_ratio is None:
                    target_ratio = self._normalize_env_percent(
                        os.getenv("DEFAULT_PROFIT_TARGET_PCT", "0.50")
                    )
                if stoploss_ratio is None:
                    stoploss_ratio = self._normalize_env_percent(
                        os.getenv("DEFAULT_STOPLOSS_PCT", "0.30")
                    )

                if target_ratio is None or stoploss_ratio is None:
                    logger.debug(
                        "Bracket orders skipped for %s due to invalid target/stop config",
                        label,
                    )
                    return

                if target_ratio <= 0 or stoploss_ratio <= 0:
                    logger.debug(
                        "Bracket orders skipped for %s (target=%f%% sl=%f%% source=%s)",
                        label,
                        target_ratio * 100.0,
                        stoploss_ratio * 100.0,
                        "strategy" if strategy_context else "environment",
                    )
                    return

                # Exit side is opposite of entry side.
                exit_side = OrderSide.SELL if side.upper() == "BUY" else OrderSide.BUY
                if side.upper() == "BUY":
                    raw_target_price = entry_price * (1.0 + target_ratio)
                    raw_stoploss_price = entry_price * (1.0 - stoploss_ratio)
                else:
                    raw_target_price = entry_price * (1.0 - target_ratio)
                    raw_stoploss_price = entry_price * (1.0 + stoploss_ratio)
                target_price = self._snap_order_price(
                    raw_target_price,
                    tick_size=tick_size,
                    side=exit_side.value,
                )
                stoploss_price = self._snap_order_price(
                    raw_stoploss_price,
                    tick_size=tick_size,
                    side=exit_side.value,
                )

                # Compatibility: fall back to legacy place_order when confirmed helper
                # isn't available (e.g., in tests or non-Angel brokers).
                has_confirmed = callable(
                    getattr(self.order_client, "place_order_confirmed", None)
                )
                
                # Log bracket order configuration
                logger.debug(
                    "[BRACKET] Configuration for %s | source=%s target=%.2f%% sl=%.2f%% target_price=%.2f sl_price=%.2f",
                    label,
                    "strategy" if strategy_context else "environment",
                    target_ratio * 100.0,
                    stoploss_ratio * 100.0,
                    target_price,
                    stoploss_price,
                )
                
                # Place target profit order
                if target_ratio > 0:
                    if has_confirmed:
                        try:
                            response = self.order_client.place_order_confirmed(
                                exchange=exchange,
                                tradingsymbol=tradingsymbol,
                                symboltoken=str(symboltoken),
                                transaction_type=exit_side.value,
                                quantity=order_qty_units,
                                producttype=ProductType.INTRADAY.value,
                                ordertype=OrderType.LIMIT.value,
                                price=target_price,
                                tag=f"TARGET_{strategy_name or 'UNKNOWN'}",
                                timeout_seconds=self.order_confirm_timeout_seconds,
                                poll_seconds=self.order_confirm_poll_seconds,
                                tick_size=tick_size,
                            )
                            terminal = str(response.get("terminal_status") or "")
                            outcome = self._handle_confirmed_closing_order(
                                response=response,
                                label=label,
                                side=exit_side.value,
                                qty=int(qty),
                                price=target_price,
                                trigger_price=None,
                                exchange=exchange,
                                symboltoken=symboltoken,
                                tradingsymbol=tradingsymbol,
                                tag=f"TARGET_{strategy_name or 'UNKNOWN'}",
                                product_type=ProductType.INTRADAY.value,
                            )
                            if terminal == "COMPLETE":
                                logger.info(
                                    "[BRACKET] Target profit order placed for %s at %.2f qty=%d [%s]",
                                    label,
                                    target_price,
                                    order_qty_units,
                                    strategy_name or "UNKNOWN",
                                )
                                log_event(
                                    logger,
                                    event_type="BRACKET_TARGET_ORDER_PLACED",
                                    message="Target profit order placed after entry fill",
                                    label=label,
                                    target_price=target_price,
                                    qty=order_qty_units,
                                    strategy_name=strategy_name,
                                    source="strategy"
                                    if strategy_context
                                    else "environment",
                                )
                                if bool(outcome.get("closed")):
                                    return
                            elif terminal in {"PENDING", "UNKNOWN"}:
                                logger.info(
                                    "[BRACKET] Target profit order pending for %s: %s",
                                    label,
                                    response,
                                )
                            else:
                                logger.warning(
                                    "[BRACKET] Target profit order placement failed for %s: %s",
                                    label,
                                    response,
                                )
                        except Exception as exc:
                            logger.warning(
                                "[BRACKET] Target profit order placement error for %s: %s",
                                label,
                                exc,
                            )
                    else:
                        try:
                            request = OrderRequest(
                                symbol=tradingsymbol,
                                quantity=order_qty_units,
                                side=exit_side,
                                order_type=OrderType.LIMIT,
                                product_type=ProductType.INTRADAY,
                                time_in_force=TimeInForce.DAY,
                                limit_price=target_price,
                                stop_price=None,
                                tag=f"TARGET_{strategy_name or 'UNKNOWN'}",
                                purpose=OrderPurpose.EXIT,
                                exchange=exchange,
                                symbol_token=str(symboltoken)
                                if symboltoken is not None
                                else None,
                                tick_size=tick_size,
                            )
                            self.order_client.place_order(request)
                        except Exception as exc:
                            logger.warning(
                                "[BRACKET] Target profit order placement error for %s: %s",
                                label,
                                exc,
                            )
                
                # Place stoploss order
                if stoploss_ratio > 0:
                    stoploss_skip_reason = self._stoploss_skip_reason(
                        label=label,
                        require_position=position_was_tracked,
                    )
                    if stoploss_skip_reason is not None:
                        logger.warning(
                            "[BRACKET] Skipping stoploss order for %s reason=%s",
                            label,
                            stoploss_skip_reason,
                        )
                        log_event(
                            logger,
                            event_type="BRACKET_STOPLOSS_SKIPPED",
                            message="Skipped stoploss order placement.",
                            level=logging.WARNING,
                            label=label,
                            strategy_name=strategy_name,
                            reason=stoploss_skip_reason,
                        )
                        return
                    if has_confirmed:
                        try:
                            response = self.order_client.place_order_confirmed(
                                exchange=exchange,
                                tradingsymbol=tradingsymbol,
                                symboltoken=str(symboltoken),
                                transaction_type=exit_side.value,
                                quantity=order_qty_units,
                                producttype=ProductType.INTRADAY.value,
                                ordertype=OrderType.SLM.value,
                                price=None,
                                trigger_price=stoploss_price,
                                tag=f"STOPLOSS_{strategy_name or 'UNKNOWN'}",
                                timeout_seconds=self.order_confirm_timeout_seconds,
                                poll_seconds=self.order_confirm_poll_seconds,
                                tick_size=tick_size,
                            )
                            if self._is_invalid_slm_ordertype_error(response):
                                fallback_limit_price = self._fallback_stoplimit_price(
                                    trigger_price=stoploss_price,
                                    exit_side=exit_side,
                                    tick_size=tick_size,
                                )
                                logger.info(
                                    "[BRACKET] Retrying stoploss order for %s as SL | trigger=%.2f limit=%.2f side=%s",
                                    label,
                                    stoploss_price,
                                    fallback_limit_price,
                                    exit_side.value,
                                )
                                response = self.order_client.place_order_confirmed(
                                    exchange=exchange,
                                    tradingsymbol=tradingsymbol,
                                    symboltoken=str(symboltoken),
                                    transaction_type=exit_side.value,
                                    quantity=order_qty_units,
                                    producttype=ProductType.INTRADAY.value,
                                    ordertype=OrderType.SL.value,
                                    price=fallback_limit_price,
                                    trigger_price=stoploss_price,
                                    tag=f"STOPLOSS_{strategy_name or 'UNKNOWN'}",
                                    timeout_seconds=self.order_confirm_timeout_seconds,
                                    poll_seconds=self.order_confirm_poll_seconds,
                                    tick_size=tick_size,
                                )
                            terminal = str(response.get("terminal_status") or "")
                            outcome = self._handle_confirmed_closing_order(
                                response=response,
                                label=label,
                                side=exit_side.value,
                                qty=int(qty),
                                price=response.get("order_row", {}).get("price")
                                if isinstance(response.get("order_row"), dict)
                                else fallback_limit_price if "fallback_limit_price" in locals() else None,
                                trigger_price=stoploss_price,
                                exchange=exchange,
                                symboltoken=symboltoken,
                                tradingsymbol=tradingsymbol,
                                tag=f"STOPLOSS_{strategy_name or 'UNKNOWN'}",
                                product_type=ProductType.INTRADAY.value,
                            )
                            if terminal == "COMPLETE":
                                logger.info(
                                    "[BRACKET] Stoploss order placed for %s at %.2f qty=%d [%s]",
                                    label,
                                    stoploss_price,
                                    order_qty_units,
                                    strategy_name or "UNKNOWN",
                                )
                                log_event(
                                    logger,
                                    event_type="BRACKET_STOPLOSS_ORDER_PLACED",
                                    message="Stoploss order placed after entry fill",
                                    label=label,
                                    stoploss_price=stoploss_price,
                                    qty=order_qty_units,
                                    strategy_name=strategy_name,
                                    source="strategy"
                                    if strategy_context
                                    else "environment",
                                )
                                if bool(outcome.get("closed")):
                                    return
                            elif terminal in {"PENDING", "UNKNOWN"}:
                                logger.info(
                                    "[BRACKET] Stoploss order pending for %s: %s",
                                    label,
                                    response,
                                )
                            else:
                                logger.warning(
                                    "[BRACKET] Stoploss order placement failed for %s: %s",
                                    label,
                                    response,
                                )
                                self._log_stoploss_order_failure(
                                    label=label,
                                    strategy_name=strategy_name,
                                    stoploss_price=stoploss_price,
                                    detail=response,
                                    fallback_mode=(
                                        "sl"
                                        if "fallback_limit_price" in locals()
                                        else "slm"
                                    ),
                                )
                        except Exception as exc:
                            if self._is_invalid_slm_ordertype_error(exc):
                                try:
                                    fallback_limit_price = self._fallback_stoplimit_price(
                                        trigger_price=stoploss_price,
                                        exit_side=exit_side,
                                        tick_size=tick_size,
                                    )
                                    logger.info(
                                        "[BRACKET] Retrying stoploss order for %s as SL | trigger=%.2f limit=%.2f side=%s",
                                        label,
                                        stoploss_price,
                                        fallback_limit_price,
                                        exit_side.value,
                                    )
                                    response = self.order_client.place_order_confirmed(
                                        exchange=exchange,
                                        tradingsymbol=tradingsymbol,
                                        symboltoken=str(symboltoken),
                                        transaction_type=exit_side.value,
                                        quantity=order_qty_units,
                                        producttype=ProductType.INTRADAY.value,
                                        ordertype=OrderType.SL.value,
                                        price=fallback_limit_price,
                                        trigger_price=stoploss_price,
                                        tag=f"STOPLOSS_{strategy_name or 'UNKNOWN'}",
                                        timeout_seconds=self.order_confirm_timeout_seconds,
                                        poll_seconds=self.order_confirm_poll_seconds,
                                        tick_size=tick_size,
                                    )
                                    terminal = str(response.get("terminal_status") or "")
                                    outcome = self._handle_confirmed_closing_order(
                                        response=response,
                                        label=label,
                                        side=exit_side.value,
                                        qty=int(qty),
                                        price=fallback_limit_price,
                                        trigger_price=stoploss_price,
                                        exchange=exchange,
                                        symboltoken=symboltoken,
                                        tradingsymbol=tradingsymbol,
                                        tag=f"STOPLOSS_{strategy_name or 'UNKNOWN'}",
                                        product_type=ProductType.INTRADAY.value,
                                    )
                                    if terminal == "COMPLETE":
                                        logger.info(
                                            "[BRACKET] Stoploss order placed for %s at %.2f qty=%d [%s]",
                                            label,
                                            stoploss_price,
                                            order_qty_units,
                                            strategy_name or "UNKNOWN",
                                        )
                                        log_event(
                                            logger,
                                            event_type="BRACKET_STOPLOSS_ORDER_PLACED",
                                            message="Stoploss order placed after entry fill",
                                            label=label,
                                            stoploss_price=stoploss_price,
                                            qty=order_qty_units,
                                            strategy_name=strategy_name,
                                            source="strategy"
                                            if strategy_context
                                            else "environment",
                                        )
                                        if bool(outcome.get("closed")):
                                            return
                                    elif terminal in {"PENDING", "UNKNOWN"}:
                                        logger.info(
                                            "[BRACKET] Stoploss order pending for %s: %s",
                                            label,
                                            response,
                                        )
                                    else:
                                        logger.warning(
                                            "[BRACKET] Stoploss order placement failed for %s: %s",
                                            label,
                                            response,
                                        )
                                        self._log_stoploss_order_failure(
                                            label=label,
                                            strategy_name=strategy_name,
                                            stoploss_price=stoploss_price,
                                            detail=response,
                                            fallback_mode="sl",
                                        )
                                    return
                                except Exception as fallback_exc:
                                    logger.warning(
                                        "[BRACKET] Stoploss order placement error for %s: %s",
                                        label,
                                        fallback_exc,
                                    )
                                    self._log_stoploss_order_failure(
                                        label=label,
                                        strategy_name=strategy_name,
                                        stoploss_price=stoploss_price,
                                        detail=fallback_exc,
                                        fallback_mode="sl",
                                    )
                                    return
                            logger.warning(
                                "[BRACKET] Stoploss order placement error for %s: %s",
                                label,
                                exc,
                            )
                            self._log_stoploss_order_failure(
                                label=label,
                                strategy_name=strategy_name,
                                stoploss_price=stoploss_price,
                                detail=exc,
                                fallback_mode="slm",
                            )
                    else:
                        try:
                            request = OrderRequest(
                                symbol=tradingsymbol,
                                quantity=order_qty_units,
                                side=exit_side,
                                order_type=OrderType.SLM,
                                product_type=ProductType.INTRADAY,
                                time_in_force=TimeInForce.DAY,
                                limit_price=None,
                                stop_price=stoploss_price,
                                tag=f"STOPLOSS_{strategy_name or 'UNKNOWN'}",
                                purpose=OrderPurpose.EXIT,
                                exchange=exchange,
                                symbol_token=str(symboltoken)
                                if symboltoken is not None
                                else None,
                                tick_size=tick_size,
                            )
                            self.order_client.place_order(request)
                        except Exception as exc:
                            logger.warning(
                                "[BRACKET] Stoploss order placement error for %s: %s",
                                label,
                                exc,
                            )
                            self._log_stoploss_order_failure(
                                label=label,
                                strategy_name=strategy_name,
                                stoploss_price=stoploss_price,
                                detail=exc,
                                fallback_mode="legacy",
                            )
                        
            except Exception as exc:
                logger.error(
                    "[BRACKET] Unexpected error placing bracket orders for %s: %s",
                    label,
                    exc,
                )
        
        # Start in background thread (non-blocking)
        thread = threading.Thread(target=_place_exits, daemon=True, name=f"bracket-{label}")
        thread.start()

    # Close a position, compute PnL, and persist trade records.
    def _register_exit(self, label: str, exit_price: float, qty: int) -> None:
        # Drop from executed-token watch list when we close the position
        try:
            executed_tokens_tracker.unregister_position(label)
        except Exception as exc:
            logger.warning(
                "ExecutedTokensTracker.unregister_position failed for %s: %s",
                label,
                exc,
            )
        now_ts = datetime.now(timezone.utc)
        with self._state_lock:
            entry = self.open_positions.pop(label, None)
            if not entry:
                self._persist_state()
                return
            self._reset_daily_if_new_day(now_ts)

            entry_underlying = entry.get("underlying")
            template = entry.get("template_name")
            template_key = str(template or "__UNKNOWN__")
            self.open_spreads_by_underlying.get(
                entry_underlying or "__UNKNOWN__", {}
            ).get(template_key, set()).discard(label)
            if not self.open_spreads_by_underlying.get(
                entry_underlying or "__UNKNOWN__", {}
            ).get(template_key):
                self.open_spreads_by_underlying.get(
                    entry_underlying or "__UNKNOWN__", {}
                ).pop(template_key, None)
            if not self.open_spreads_by_underlying.get(
                entry_underlying or "__UNKNOWN__", {}
            ):
                self.open_spreads_by_underlying.pop(entry_underlying or "__UNKNOWN__", None)

            entry_side = entry.get("side", "SELL").upper()
            sign = 1.0 if entry_side == "BUY" else -1.0
            entry_price_val = float(entry.get("entry_price", 0.0))
            meta = self.instrument_meta.get(label, {})
            lot_size = float(entry.get("lot_size") or meta.get("lot_size") or 1)
            pnl = sign * (exit_price - entry_price_val) * qty * lot_size
            self.realized_pnl += pnl
            self.daily_realized_pnl += pnl
            self.max_equity = max(self.max_equity, self.realized_pnl)
            self.daily_peak_equity = max(self.daily_peak_equity, self.daily_realized_pnl)
            realized_total = self.realized_pnl
            drawdown = max(0.0, self.max_equity - self.realized_pnl)

        logger.info(
            "[PNL_EXIT] position_closed | label=%s side=%s qty=%d entry=%.2f exit=%.2f pnl=%.2f total_realized=%.2f",
            label,
            entry_side,
            qty,
            entry_price_val,
            exit_price,
            pnl,
            realized_total,
        )
        self._check_kill_switch(now_ts)
        trade_record = {
            "entry_ts": entry.get("entry_ts"),
            "exit_ts": datetime.now(timezone.utc).isoformat(),
            "label": label,
            "underlying": entry_underlying,
            "strategy_name": entry.get("strategy_name"),
            "template_name": entry.get("template_name"),
            "side": entry_side,
            "qty": qty,
            "entry_price": entry.get("entry_price"),
            "exit_price": exit_price,
            "pnl": pnl,
            "realized_pnl": realized_total,
            "drawdown": drawdown,
            "reason": entry.get("reason"),
            "fees": 0.0,
        }
        if self.trade_persister:
            self.trade_persister.record_trade(trade_record)
        dashboard_bus.close_position(
            label,
            exit_price=exit_price,
            realized_pnl=realized_total,
            trade_record=trade_record,
        )
        logger.info(
            "RiskManager realized PnL for %s | label=%s qty=%d entry=%.3f exit=%.3f -> pnl=%.3f | total=%.3f",
            entry_side,
            label,
            qty,
            entry.get("entry_price"),
            exit_price,
            pnl,
            realized_total,
        )
        self._publish_account_loss_guard(source="register_exit")
        self._persist_state()

    # Activate the kill switch when loss/drawdown limits are hit.
    def _check_kill_switch(self, now: datetime) -> None:
        self.evaluate_account_loss(now=now, source="kill_switch_check", force=True)

    # ----------------------
    # Pending order reconciliation
    # ----------------------

    # Find a pending order context for the given label.
    def _pending_order_for_label(self, label: str) -> Optional[Dict[str, Any]]:
        with self._state_lock:
            for _oid, ctx in self._pending_orders.items():
                if ctx.get("label") == label:
                    return dict(ctx)
        return None

    # Store pending order context and log it for tracking.
    def _record_pending_order(self, *, order_id: str, ctx: Dict[str, Any]) -> None:
        if not order_id:
            return
        ctx = dict(ctx)
        ctx.setdefault("order_id", order_id)
        ctx.setdefault("created_ts", datetime.now(timezone.utc).isoformat())
        ctx.setdefault("last_status", "PENDING")
        with self._state_lock:
            self._pending_orders[str(order_id)] = ctx
        logger.warning(
            "[RISK][%s] Order pending | order_id=%s label=%s side=%s qty=%s",
            self.current_trade_mode,
            order_id,
            ctx.get("label"),
            ctx.get("side"),
            ctx.get("qty"),
        )

    def _pending_closing_order_items(
        self, label: str
    ) -> list[tuple[str, Dict[str, Any]]]:
        target_label = str(label or "")
        if not target_label:
            return []
        with self._state_lock:
            return [
                (str(oid), dict(ctx))
                for oid, ctx in self._pending_orders.items()
                if str(ctx.get("label") or "") == target_label
                and bool(ctx.get("is_closing"))
            ]

    def _cancel_pending_closing_orders_for_label(self, label: str) -> list[str]:
        pending_items = self._pending_closing_order_items(label)
        if not pending_items:
            return []
        cancel_order = getattr(self.order_client, "cancel_order", None)
        if not callable(cancel_order):
            logger.warning(
                "[RISK] Pending closing orders remain for %s because cancel_order is unavailable",
                label,
            )
            return []

        cancelled: list[str] = []
        terminal_statuses = {"CANCELLED", "REJECTED", "EXPIRED"}
        for oid, ctx in pending_items:
            tag = str(ctx.get("tag") or "").strip().upper()
            variety = "STOPLOSS" if tag.startswith("STOPLOSS") else "NORMAL"
            symbol = str(ctx.get("tradingsymbol") or ctx.get("label") or "").strip()
            try:
                try:
                    response = cancel_order(str(oid), symbol=symbol or None, variety=variety)
                except TypeError:
                    response = cancel_order(
                        order_id=str(oid),
                        symbol=symbol or None,
                        variety=variety,
                    )
            except Exception as exc:
                logger.warning(
                    "[RISK] Failed to cancel pending closing order before forced exit | order_id=%s label=%s error=%s",
                    oid,
                    label,
                    exc,
                )
                continue

            success = bool(response) and not isinstance(response, dict)
            terminal = "UNKNOWN"
            if isinstance(response, dict):
                response_data = response.get("data")
                raw_status = (
                    response.get("orderstatus")
                    or response.get("status")
                    if isinstance(response.get("status"), str)
                    else None
                )
                if raw_status is None and isinstance(response_data, dict):
                    raw_status = (
                        response_data.get("orderstatus")
                        or response_data.get("status")
                    )
                terminal = self._normalize_broker_order_status(raw_status)
                success = bool(response.get("status") is True or terminal in terminal_statuses)
            if not success:
                logger.warning(
                    "[RISK] Pending closing order cancel not confirmed | order_id=%s label=%s response=%s",
                    oid,
                    label,
                    response,
                )
                continue

            resolved_status = terminal if terminal in terminal_statuses else "CANCELLED"
            with self._state_lock:
                existing_ctx = self._pending_orders.pop(str(oid), None)
                if existing_ctx is not None:
                    existing_ctx["last_status"] = resolved_status
                    existing_ctx["cancelled_ts"] = datetime.now(timezone.utc).isoformat()
            cancelled.append(str(oid))
            logger.info(
                "[RISK] Cancelled pending closing order before forced exit | order_id=%s label=%s status=%s",
                oid,
                label,
                resolved_status,
            )
        remaining_pending = self._pending_closing_order_items(label)
        if remaining_pending:
            log_event(
                logger,
                event_type="FORCED_EXIT_PENDING_ORDERS_REMAIN",
                message="Pending closing orders remain after forced-exit cancellation attempt.",
                level=logging.WARNING,
                label=label,
                pending_count=len(remaining_pending),
                pending_order_ids=",".join(str(oid) for oid, _ctx in remaining_pending),
            )
        return cancelled

    def _reconcile_pending_order(
        self,
        order_id: str,
        *,
        ctx: Optional[Dict[str, Any]] = None,
        order_index: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        oid = str(order_id or "")
        if not oid:
            return {"matched": False, "closed": False, "pending": False, "status": "UNKNOWN"}
        if ctx is None:
            with self._state_lock:
                existing_ctx = self._pending_orders.get(oid)
            if existing_ctx is None:
                return {"matched": False, "closed": False, "pending": False, "status": "UNKNOWN"}
            ctx = dict(existing_ctx)
        else:
            ctx = dict(ctx)

        row = self._load_pending_order_row(oid, order_index=order_index)
        if not row:
            return {
                "matched": True,
                "closed": False,
                "pending": True,
                "status": str(ctx.get("last_status") or "PENDING"),
            }

        raw_status = (
            row.get("orderstatus")
            or row.get("orderStatus")
            or row.get("status")
            or row.get("order_state")
        )
        status = self._normalize_broker_order_status(raw_status)
        with self._state_lock:
            existing_ctx = self._pending_orders.get(oid)
            if existing_ctx is not None:
                existing_ctx["last_status"] = status
                existing_ctx["last_checked_ts"] = datetime.now(timezone.utc).isoformat()

        logger.info(
            "[ORDER_DETAILS] order_id=%s status=%s averageprice=%s filledshares=%s",
            oid,
            status,
            row.get("averageprice")
            or row.get("average_price")
            or row.get("avgprice")
            or row.get("averagePrice"),
            row.get("filledshares")
            or row.get("filled_qty")
            or row.get("filled_quantity")
            or row.get("filledQuantity"),
        )

        if status == "COMPLETE":
            label = str(ctx.get("label") or "")
            side = str(ctx.get("side") or "")
            qty = self._pending_order_qty_from_row(ctx=ctx, row=row)
            price = float(
                self._pick_nonzero_price(
                    row.get("averageprice"),
                    row.get("average_price"),
                    row.get("avgprice"),
                    row.get("averagePrice"),
                    row.get("tradedprice"),
                    row.get("traded_price"),
                    row.get("fillprice"),
                    row.get("filledprice"),
                    row.get("price"),
                    ctx.get("price"),
                    ctx.get("trigger_price"),
                    0.0,
                )
                or 0.0
            )
            if not label or qty <= 0:
                with self._state_lock:
                    self._pending_orders.pop(oid, None)
                return {
                    "matched": True,
                    "closed": False,
                    "pending": False,
                    "status": status,
                }

            self._persist_execution_record_from_ctx(
                ctx=ctx,
                qty=qty,
                price=price,
                broker_order_id=oid,
            )

            if bool(ctx.get("is_closing")):
                self._register_exit(label, price, qty)
            else:
                self._register_entry(
                    label,
                    side,
                    qty,
                    price,
                    str(ctx.get("template_name") or "__UNKNOWN__"),
                    ctx.get("role"),
                    ctx.get("underlying"),
                    ctx.get("strategy_name"),
                    ctx.get("reason"),
                    ml_info=ctx.get("ml_info"),
                    count_for_spreads=bool(ctx.get("count_for_spreads")),
                )

            with self._state_lock:
                self._pending_orders.pop(oid, None)
            return {
                "matched": True,
                "closed": bool(ctx.get("is_closing")),
                "pending": False,
                "status": status,
                "price": price,
                "qty": qty,
            }

        if status in ("REJECTED", "CANCELLED", "EXPIRED"):
            logger.warning(
                "[RISK][%s] Pending order terminal=%s | order_id=%s label=%s",
                self.current_trade_mode,
                status,
                oid,
                ctx.get("label"),
            )
            with self._state_lock:
                self._pending_orders.pop(oid, None)
            return {
                "matched": True,
                "closed": False,
                "pending": False,
                "status": status,
            }

        return {"matched": True, "closed": False, "pending": True, "status": status}

    def reconcile_pending_closing_orders_for_label(self, label: str) -> Dict[str, Any]:
        target_label = str(label or "")
        if not target_label:
            return {"matched": False, "closed": False, "pending": False}
        with self._state_lock:
            pending_items = [
                (str(oid), dict(ctx))
                for oid, ctx in self._pending_orders.items()
                if str(ctx.get("label") or "") == target_label and bool(ctx.get("is_closing"))
            ]
        if not pending_items:
            return {"matched": False, "closed": False, "pending": False}

        matched = False
        pending = False
        statuses: list[str] = []
        for oid, ctx in pending_items:
            outcome = self._reconcile_pending_order(oid, ctx=ctx)
            matched = matched or bool(outcome.get("matched"))
            status = str(outcome.get("status") or "")
            if status:
                statuses.append(status)
            if bool(outcome.get("closed")):
                return {
                    "matched": True,
                    "closed": True,
                    "pending": False,
                    "status": status or "COMPLETE",
                    "statuses": statuses,
                }
            pending = pending or bool(outcome.get("pending"))
        return {
            "matched": matched,
            "closed": False,
            "pending": pending,
            "status": statuses[-1] if statuses else "UNKNOWN",
            "statuses": statuses,
        }

    # Resolve pending orders by inspecting the broker order book.
    def _reconcile_pending_orders(self) -> None:
        """Check pending broker orders and finalize state only when COMPLETE."""
        if self.current_trade_mode.upper() != "LIVE":
            return
        with self._state_lock:
            pending_items = list(self._pending_orders.items())
        if not pending_items:
            return

        # Fetch once to limit API pressure.
        try:
            book = self.order_client.get_order_book()
        except Exception as exc:
            logger.warning("Pending order reconcile: get_order_book failed: %s", exc)
            return

        rows = book.get("data") if isinstance(book, dict) else None
        if not isinstance(rows, list):
            return
        index: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            oid = row.get("orderid") or row.get("order_id") or row.get("orderId")
            if oid:
                index[str(oid)] = row

        for oid, ctx in pending_items:
            self._reconcile_pending_order(oid, ctx=ctx, order_index=index)

    # Submit an order to the broker or simulate in paper mode.
    def _submit_order(
        self,
        *,
        exchange: str,
        symboltoken: str,
        tradingsymbol: str,
        side: str,
        quantity: int,
        producttype: str,
        trade_mode: str,
        tag: Optional[str] = None,
    ) -> Tuple[bool, str, Optional[str], Optional[Dict[str, Any]]]:
        mode = trade_mode.upper()
        self._reset_daily_if_new_day(datetime.now(timezone.utc))
        if mode != "LIVE":
            logger.info(
                "[PAPER][RiskManager] %s %s (%s:%s) qty=%d tag=%s",
                side,
                tradingsymbol,
                exchange,
                symboltoken,
                quantity,
                tag,
            )
            return True, "PAPER", None, None

        try:
            confirm = self.order_client.place_order_confirmed(
                exchange=exchange,
                tradingsymbol=tradingsymbol,
                symboltoken=str(symboltoken),
                transaction_type=side,
                quantity=quantity,
                producttype=producttype,
                ordertype="MARKET",
                variety="NORMAL",
                tag=tag,
                timeout_seconds=self.order_confirm_timeout_seconds,
                poll_seconds=self.order_confirm_poll_seconds,
            )
            order_id = str(confirm.get("order_id") or "")
            terminal = str(confirm.get("terminal_status") or "UNKNOWN")
            order_row = confirm.get("order_row") if isinstance(confirm, dict) else None
            logger.info(
                "[LIVE][RiskManager] %s %s (%s:%s) qty=%d tag=%s -> order_id=%s terminal=%s",
                side,
                tradingsymbol,
                exchange,
                symboltoken,
                quantity,
                tag,
                order_id or "-",
                terminal,
            )
            return (terminal == "COMPLETE"), terminal, order_id, order_row
        except Exception as exc:
            logger.error("[LIVE][RiskManager] order failed: %s", exc)
            return False, "ERROR", None, None

    # Main entry point to place an order with full risk checks.
    def place_order(
        self,
        *,
        label: str,
        side: str,
        qty: int,
        price: float,
        template_name: Optional[str] = None,
        role: Optional[str] = None,
        trade_mode: str = "PAPER",
        product_type: str = "INTRADAY",
        strategy_name: Optional[str] = None,
        reason: Optional[str] = None,
        tag: Optional[str] = None,
        ml_info: Optional[Dict[str, Any]] = None,
    ) -> RiskDecision:
        side = side.upper()
        meta = self.instrument_meta.get(label, {})
        exchange = meta.get("exchange", "NSE")
        symboltoken = meta.get("token")
        tradingsymbol = meta.get("symbol", label)
        underlying = meta.get("underlying")
        heartbeat_label = underlying or label
        self.current_trade_mode = trade_mode.upper()
        mode_tag = f"[RISK][{self.current_trade_mode}]"
        dashboard_bus.set_trade_mode(self.current_trade_mode)
        now_ts = datetime.now(timezone.utc)
        self._reset_daily_if_new_day(now_ts)
        # Finalize any previously submitted broker orders before deciding on new actions.
        self._reconcile_pending_orders()

        if self.current_trade_mode.upper() == "LIVE":
            # §102: In LIVE hub-authoritative mode, direct broker order placement
            # through RiskManager is forbidden for automated entries.  All entry
            # and exit signals must pass through the hub/router/lifecycle path.
            # If the hub exit submitter is set, this indicates hub mode is active;
            # block legacy direct entries.  Closing (exit) orders are still
            # allowed through this path as break-glass or reconciliation exits.
            with self._state_lock:
                _existing_for_guard = (
                    dict(self.open_positions.get(label, {}))
                    if label in self.open_positions
                    else None
                )
            _is_closing_entry = bool(
                _existing_for_guard
                and _existing_for_guard.get("side", "").upper() != side
            )
            if (
                self._hub_exit_submitter is not None
                and not _is_closing_entry
            ):
                logger.error(
                    "risk_manager.legacy_entry_blocked: LIVE hub-authoritative mode active "
                    "but RiskManager.place_order() called for automated entry "
                    "label=%s side=%s qty=%d strategy=%s. "
                    "Use place_order_via_bridge / hub router instead.",
                    label, side, qty, strategy_name,
                )
                return self._make_decision(
                    allowed=False,
                    reason="legacy_direct_entry_blocked_in_live_hub_mode",
                    label=label,
                    side=side,
                    qty=qty,
                    strategy_name=strategy_name,
                    template_name=template_name,
                )

            pending = self._pending_order_for_label(label)
            if pending:
                return self._make_decision(
                    allowed=False,
                    reason=f"order_pending:{pending.get('last_status') or 'PENDING'}",
                    label=label,
                    side=side,
                    qty=qty,
                    strategy_name=strategy_name,
                    template_name=template_name,
                )
        with self._state_lock:
            existing = dict(self.open_positions.get(label, {})) if label in self.open_positions else None
        is_closing = existing and existing.get("side", "").upper() != side

        if side not in {"BUY", "SELL"} or qty <= 0:
            logger.warning("%s Skipping invalid order %s qty=%d", mode_tag, label, qty)
            return self._make_decision(
                allowed=False,
                reason="invalid_order_params",
                label=label,
                side=side,
                qty=qty,
                strategy_name=strategy_name,
                template_name=template_name,
            )

        if not is_closing:
            self.evaluate_account_loss(
                now=now_ts,
                source="place_order",
                force=True,
            )
        if self.kill_switch_activated:
            if not is_closing:
                logger.warning("%s Kill switch active, blocking new entries", mode_tag)
                return self._make_decision(
                    allowed=False,
                    reason="kill_switch_active_blocking_entries",
                    label=label,
                    side=side,
                    qty=qty,
                    strategy_name=strategy_name,
                    template_name=template_name,
                )

        # Always enforce day-loss and maintenance before spread limits
        if (
            not is_closing
            and self.day_loss_limit > 0
            and self.realized_pnl <= -abs(self.day_loss_limit)
        ):
            logger.warning(
                "%s Day loss limit reached (limit=%.2f, realized=%.2f); blocking new entries",
                mode_tag,
                self.day_loss_limit,
                self.realized_pnl,
            )
            return self._make_decision(
                allowed=False,
                reason="day_loss_limit_reached",
                label=label,
                side=side,
                qty=qty,
                strategy_name=strategy_name,
                template_name=template_name,
            )

        if (
            self.maintenance
            and not is_closing
            and not self.maintenance.allow_new_entries(heartbeat_label)
        ):
            logger.warning(
                "%s [%s] Maintenance blocks new entries", mode_tag, heartbeat_label
            )
            return self._make_decision(
                allowed=False,
                reason="maintenance_block",
                label=label,
                side=side,
                qty=qty,
                strategy_name=strategy_name,
                template_name=template_name,
            )

        if side == "SELL" and not is_closing and not self._can_enter(
            underlying, template_name
        ):
            logger.warning(
                "%s Spread cap hit for %s (template=%s)",
                mode_tag,
                underlying,
                template_name,
            )
            return self._make_decision(
                allowed=False,
                reason="max_spreads_reached",
                label=label,
                side=side,
                qty=qty,
                strategy_name=strategy_name,
                template_name=template_name,
            )

        underlying_label = self._underlying_label_for_label(label)
        if (
            not is_closing
            and self.instrument_controller
            and not self.instrument_controller.is_enabled(underlying_label)
        ):
            logger.warning(
                "[INSTRUMENT_CONTROL] Blocking order: strategy=%s instrument=%s reason=DISABLED",
                strategy_name,
                underlying_label,
            )
            return self._make_decision(
                allowed=False,
                reason="instrument_disabled",
                label=label,
                side=side,
                qty=qty,
                strategy_name=strategy_name,
                template_name=template_name,
            )
        if (
            not is_closing
            and self.instrument_controller
            and strategy_name
            and not self.instrument_controller.is_strategy_allowed(
                underlying_label, strategy_name
            )
        ):
            logger.warning(
                "[INSTRUMENT_CONTROL] Blocking order: strategy=%s instrument=%s reason=NOT_ALLOWED",
                strategy_name,
                underlying_label,
            )
            return self._make_decision(
                allowed=False,
                reason="strategy_not_allowed_for_instrument",
                label=label,
                side=side,
                qty=qty,
                strategy_name=strategy_name,
                template_name=template_name,
            )

        # Per-underlying lot/notional limits (only for opening trades)
        if not is_closing:
            limits = self._get_limits_for_underlying(underlying)
            meta = self.instrument_meta.get(label, {})
            lot_size = float(meta.get("lot_size") or 1)
            order_lots = float(qty)
            open_lots_before = self._get_current_open_lots(underlying)
            open_lots_after = open_lots_before + order_lots

            max_lots_per_trade = limits.get("max_lots_per_trade")
            if max_lots_per_trade is not None and order_lots > float(
                max_lots_per_trade
            ):
                logger.warning(
                    "%s Blocked order for %s: lots=%.3f open_before=%.3f limit=%.3f reason=RISK_MAX_LOTS_PER_TRADE",
                    mode_tag,
                    underlying,
                    order_lots,
                    open_lots_before,
                    max_lots_per_trade,
                )
                return self._make_decision(
                    allowed=False,
                    reason="max_lots_per_trade_exceeded",
                    label=label,
                    side=side,
                    qty=qty,
                    strategy_name=strategy_name,
                    template_name=template_name,
                )

            max_open_lots = limits.get("max_open_lots")
            if max_open_lots is not None and open_lots_after > float(max_open_lots):
                logger.warning(
                    "%s Blocked order for %s: lots=%.3f open_before=%.3f open_after=%.3f limit=%.3f reason=RISK_MAX_OPEN_LOTS",
                    mode_tag,
                    underlying,
                    order_lots,
                    open_lots_before,
                    open_lots_after,
                    max_open_lots,
                )
                return self._make_decision(
                    allowed=False,
                    reason="max_open_lots_exceeded",
                    label=label,
                    side=side,
                    qty=qty,
                    strategy_name=strategy_name,
                    template_name=template_name,
                )

            max_notional = limits.get("max_notional_per_underlying")
            if max_notional:
                try:
                    price_f = float(price)
                    notional_after = open_lots_after * lot_size * price_f
                    if notional_after > float(max_notional):
                        logger.warning(
                            "%s Blocked order for %s: notional_after=%.2f limit=%.2f reason=RISK_MAX_NOTIONAL_EXCEEDED",
                            mode_tag,
                            underlying,
                            notional_after,
                            max_notional,
                        )
                        return self._make_decision(
                            allowed=False,
                            reason="max_notional_exceeded",
                            label=label,
                            side=side,
                            qty=qty,
                            strategy_name=strategy_name,
                            template_name=template_name,
                        )
                except Exception:
                    logger.warning(
                        "%s Unable to compute notional for %s, skipping notional check",
                        mode_tag,
                        underlying,
                    )

        symboltoken_str = str(symboltoken or "")
        ok, terminal, broker_order_id, _order_row = self._submit_order(
            exchange=exchange,
            symboltoken=symboltoken_str,
            tradingsymbol=tradingsymbol,
            side=side,
            quantity=qty,
            producttype=product_type,
            trade_mode=trade_mode,
            tag=tag,
        )
        if not ok:
            # If the broker has accepted the order but it hasn't reached a terminal
            # status yet, treat it as pending and block duplicates.
            if (
                self.current_trade_mode.upper() == "LIVE"
                and broker_order_id
                and terminal not in ("REJECTED", "CANCELLED", "EXPIRED", "ERROR")
            ):
                self._record_pending_order(
                    order_id=str(broker_order_id),
                    ctx={
                        "label": label,
                        "side": side,
                        "qty": qty,
                        "price": price,
                        "template_name": template_name,
                        "role": role,
                        "underlying": underlying,
                        "strategy_name": strategy_name,
                        "reason": reason,
                        "ml_info": ml_info,
                        "count_for_spreads": (side == "SELL"),
                        "is_closing": bool(is_closing),
                        "exchange": exchange,
                        "symboltoken": symboltoken,
                        "tradingsymbol": tradingsymbol,
                        "tag": tag,
                        "product_type": product_type,
                    },
                )
                return self._make_decision(
                    allowed=False,
                    reason=f"order_pending:{terminal}",
                    label=label,
                    side=side,
                    qty=qty,
                    strategy_name=strategy_name,
                    template_name=template_name,
                )

            return self._make_decision(
                allowed=False,
                reason=f"order_{(terminal or 'submit_failed').lower()}",
                label=label,
                side=side,
                qty=qty,
                strategy_name=strategy_name,
                template_name=template_name,
            )

        def _coerce_float(value: Any) -> Optional[float]:
            try:
                if value is None:
                    return None
                return float(value)
            except (TypeError, ValueError):
                return None

        def _pick_price(*values: Any) -> Optional[float]:
            for v in values:
                f = _coerce_float(v)
                if f is None:
                    continue
                if f == 0.0:
                    continue
                return f
            return None

        fill_price = None
        if isinstance(_order_row, dict):
            fill_price = _pick_price(
                _order_row.get("averageprice"),
                _order_row.get("average_price"),
                _order_row.get("avgprice"),
                _order_row.get("averagePrice"),
                _order_row.get("tradedprice"),
                _order_row.get("traded_price"),
                _order_row.get("fillprice"),
                _order_row.get("filledprice"),
                _order_row.get("price"),
            )

        exec_price = float(fill_price if fill_price is not None else price)
        exec_ts = datetime.now(timezone.utc).isoformat()
        if self.trade_persister:
            exec_record = {
                "timestamp": exec_ts,
                "label": label,
                "underlying": underlying,
                "strategy_name": strategy_name,
                "template_name": template_name,
                "reason": reason,
                "side": side,
                "qty": qty,
                "price": exec_price,
                "exchange": exchange,
                "symboltoken": symboltoken,
                "tradingsymbol": tradingsymbol,
                "trade_mode": trade_mode,
                "product_type": product_type,
                "tag": tag,
                "broker_order_id": broker_order_id,
            }
            exec_record["trade_id"] = "|".join(
                [
                    str(symboltoken or tradingsymbol or label),
                    side,
                    str(qty),
                    exec_ts,
                ]
            )
            try:
                self.trade_persister.record_execution(exec_record)
            except Exception as exc:
                logger.error("Failed to persist execution for %s: %s", label, exc)

        strategy_tag = strategy_name or "__UNKNOWN_STRAT__"
        if is_closing:
            self._register_exit(label, exec_price, qty)
        else:
            self._register_entry(
                label,
                side,
                qty,
                exec_price,
                template_name or "__UNKNOWN__",
                role,
                underlying,
                strategy_name,
                reason,
                ml_info=ml_info,
                count_for_spreads=(side == "SELL"),
            )
            logger.info(
                "[%s] ENTER %s %s qty=%d price=%.2f tmpl=%s role=%s reason=%s",
                strategy_tag,
                side,
                tradingsymbol,
                qty,
                price,
                template_name,
                role,
                reason,
            )
        if is_closing:
            logger.info(
                "[%s] EXIT %s %s qty=%d price=%.2f tmpl=%s role=%s reason=%s",
                strategy_tag,
                side,
                tradingsymbol,
                qty,
                price,
                template_name,
                role,
                reason,
            )
        self.current_trade_mode = trade_mode.upper()

        return self._make_decision(
            allowed=True,
            reason=reason or "order_submitted",
            label=label,
            side=side,
            qty=qty,
            strategy_name=strategy_name,
            template_name=template_name,
        )

    # Square off all open positions immediately.
    def square_off_all(
        self,
        *,
        trade_mode: str = "LIVE",
        ltp_lookup: Optional[Dict[str, float]] = None,
        reason: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> None:
        with self._state_lock:
            positions_snapshot = list(self.open_positions.items())
        for label, entry in positions_snapshot:
            side = entry.get("side", "SELL").upper()
            qty = entry.get("qty", 0)
            exit_side = "BUY" if side == "SELL" else "SELL"
            price = float(entry.get("entry_price", 0.0))
            if ltp_lookup and label in ltp_lookup:
                price = float(ltp_lookup[label])
            meta = self.instrument_meta.get(label, {})
            entry_exchange = entry.get("exchange")
            entry_token = entry.get("symboltoken")
            entry_symbol = entry.get("tradingsymbol")
            exchange = entry_exchange or meta.get("exchange", "NSE")
            symboltoken = entry_token or meta.get("token")
            tradingsymbol = entry_symbol or meta.get("symbol", label)
            if not symboltoken or qty <= 0:
                continue
            strategy_name = (
                str(entry.get("strategy_name") or "").strip() or None
            )
            exit_reason = str(reason or tag or "SQUARE_OFF")
            self._mark_forced_exit_suppression(label)
            try:
                self._cancel_pending_closing_orders_for_label(label)
                if self._submit_exit_via_hub(
                    label=label,
                    strategy_name=strategy_name,
                    exchange=str(exchange),
                    symboltoken=str(symboltoken),
                    tradingsymbol=str(tradingsymbol),
                    side=exit_side,
                    quantity=int(qty),
                    trade_mode=trade_mode,
                    tag=tag,
                    reason=exit_reason,
                ):
                    self._register_exit(label, price, qty)
                    continue
                self._assert_direct_submit_allowed(
                    trade_mode=trade_mode,
                    exit_source="square_off_all",
                    label=label,
                )
                ok, terminal, broker_order_id, _order_row = self._submit_order(
                    exchange=exchange,
                    symboltoken=symboltoken,
                    tradingsymbol=tradingsymbol,
                    side=exit_side,
                    quantity=qty,
                    producttype="INTRADAY",
                    trade_mode=trade_mode,
                    tag=tag,
                )
                if not ok:
                    if (
                        trade_mode.upper() == "LIVE"
                        and broker_order_id
                        and terminal not in ("REJECTED", "CANCELLED", "EXPIRED", "ERROR")
                    ):
                        self._record_pending_order(
                            order_id=str(broker_order_id),
                            ctx={
                                "label": label,
                                "side": exit_side,
                                "qty": int(qty),
                                "price": float(price),
                                "template_name": entry.get("template_name"),
                                "role": entry.get("role"),
                                "underlying": entry.get("underlying"),
                                "strategy_name": entry.get("strategy_name"),
                                "reason": entry.get("reason") or tag,
                                "ml_info": entry.get("ml_info"),
                                "count_for_spreads": False,
                                "is_closing": True,
                                "exchange": exchange,
                                "symboltoken": symboltoken,
                                "tradingsymbol": tradingsymbol,
                                "tag": tag,
                                "product_type": "INTRADAY",
                            },
                        )
                    continue

                self._register_exit(label, price, qty)
            except P0RuleViolation:
                logger.warning(
                    "Direct broker submit blocked for %s in hub-authoritative LIVE (square_off_all)",
                    label,
                )
            except Exception as exc:
                logger.error("Error while square-off %s: %s", label, exc)

    # Square off positions at end-of-day, respecting exclusions.
    def square_off_eod(
        self,
        *,
        trade_mode: str = "LIVE",
        ltp_lookup: Optional[Dict[str, float]] = None,
        exclude_underlyings: Optional[set[str]] = None,
        processed_underlyings: Optional[set[str]] = None,
        tag: str = "EOD_SQUARE_OFF",
    ) -> set[str]:
        """

        from __future__ import annotations

                Square-off all open positions except excluded underlyings, skipping any already processed.
                Returns the set of underlyings for which square-off orders were placed successfully.
        """
        exclude_set = {u.upper() for u in (exclude_underlyings or set())}
        processed_set = {u.upper() for u in (processed_underlyings or set())}
        positions_by_underlying: Dict[str, list[tuple[str, Dict[str, Any]]]] = {}

        with self._state_lock:
            positions_snapshot = list(self.open_positions.items())
        for label, entry in positions_snapshot:
            underlying = (
                entry.get("underlying") or self._infer_underlying(label) or ""
            ).upper()
            if (
                not underlying
                or underlying in exclude_set
                or underlying in processed_set
            ):
                continue
            positions_by_underlying.setdefault(underlying, []).append((label, entry))

        closed_underlyings: set[str] = set()
        for underlying, entries in positions_by_underlying.items():
            any_success = False
            had_failure = False
            for label, entry in entries:
                side = entry.get("side", "SELL").upper()
                qty = int(entry.get("qty", 0))
                if qty <= 0:
                    continue
                exit_side = "BUY" if side == "SELL" else "SELL"
                price = float(entry.get("entry_price", 0.0))
                if ltp_lookup and label in ltp_lookup:
                    price = float(ltp_lookup[label])

                meta = self.instrument_meta.get(label, {})
                exchange = entry.get("exchange") or meta.get("exchange", "NSE")
                symboltoken = entry.get("symboltoken") or meta.get("token")
                tradingsymbol = entry.get("tradingsymbol") or meta.get("symbol", label)
                if not symboltoken:
                    logger.warning(
                        "[EOD] Missing token for %s (%s), skipping square-off",
                        label,
                        underlying,
                    )
                    continue

                logger.info(
                    "[EOD] Square-off | ts=%s underlying=%s label=%s side=%s qty=%d reason=%s",
                    datetime.now(timezone.utc).isoformat(),
                    underlying,
                    label,
                    exit_side,
                    qty,
                    tag,
                )

                try:
                    strategy_name = (
                        str(entry.get("strategy_name") or "").strip() or None
                    )
                    self._mark_forced_exit_suppression(label)
                    self._cancel_pending_closing_orders_for_label(label)
                    if self._submit_exit_via_hub(
                        label=label,
                        strategy_name=strategy_name,
                        exchange=str(exchange),
                        symboltoken=str(symboltoken),
                        tradingsymbol=str(tradingsymbol),
                        side=exit_side,
                        quantity=int(qty),
                        trade_mode=trade_mode,
                        tag=tag,
                        reason=tag,
                    ):
                        self._register_exit(label, price, qty)
                        any_success = True
                        continue
                    self._assert_direct_submit_allowed(
                        trade_mode=trade_mode,
                        exit_source="square_off_eod",
                        label=label,
                    )
                    ok, terminal, broker_order_id, _order_row = self._submit_order(
                        exchange=exchange,
                        symboltoken=symboltoken,
                        tradingsymbol=tradingsymbol,
                        side=exit_side,
                        quantity=qty,
                        producttype="INTRADAY",
                        trade_mode=trade_mode,
                        tag=tag,
                    )
                    if not ok:
                        had_failure = True
                        if (
                            trade_mode.upper() == "LIVE"
                            and broker_order_id
                            and terminal not in (
                                "REJECTED",
                                "CANCELLED",
                                "EXPIRED",
                                "ERROR",
                            )
                        ):
                            self._record_pending_order(
                                order_id=str(broker_order_id),
                                ctx={
                                    "label": label,
                                    "side": exit_side,
                                    "qty": int(qty),
                                    "price": float(price),
                                    "template_name": entry.get("template_name"),
                                    "role": entry.get("role"),
                                    "underlying": entry.get("underlying"),
                                    "strategy_name": entry.get("strategy_name"),
                                    "reason": entry.get("reason") or tag,
                                    "ml_info": entry.get("ml_info"),
                                    "count_for_spreads": False,
                                    "is_closing": True,
                                    "exchange": exchange,
                                    "symboltoken": symboltoken,
                                    "tradingsymbol": tradingsymbol,
                                    "tag": tag,
                                    "product_type": "INTRADAY",
                                },
                            )
                        continue

                    self._register_exit(label, price, qty)
                    if self.trade_persister:
                        exec_ts = datetime.now(timezone.utc).isoformat()
                        exec_record = {
                            "timestamp": exec_ts,
                            "label": label,
                            "underlying": underlying,
                            "strategy_name": entry.get("strategy_name"),
                            "template_name": entry.get("template_name"),
                            "reason": entry.get("reason") or tag,
                            "side": exit_side,
                            "qty": qty,
                            "price": price,
                            "exchange": exchange,
                            "symboltoken": symboltoken,
                            "tradingsymbol": tradingsymbol,
                            "trade_mode": trade_mode,
                            "product_type": "INTRADAY",
                            "tag": tag,
                            "broker_order_id": broker_order_id,
                        }
                        exec_record["trade_id"] = "|".join(
                            [
                                str(symboltoken or tradingsymbol or label),
                                exit_side,
                                str(qty),
                                exec_ts,
                            ]
                        )
                        try:
                            self.trade_persister.record_execution(exec_record)
                        except Exception as exc:
                            logger.error(
                                "[EOD] Failed to persist execution for %s: %s",
                                label,
                                exc,
                            )
                    any_success = True
                except Exception as exc:
                    logger.error(
                        "[EOD] Square-off failed for %s (%s): %s",
                        label,
                        underlying,
                        exc,
                    )
                    had_failure = True
            if any_success and not had_failure:
                closed_underlyings.add(underlying)
        return closed_underlyings

    # Square off positions belonging to specific strategies.
    def square_off_strategies(
        self,
        *,
        strategy_names: set[str],
        trade_mode: str = "LIVE",
        ltp_lookup: Optional[Dict[str, float]] = None,
        tag: str = "STRATEGY_SQUARE_OFF",
        reason: Optional[str] = None,
    ) -> set[str]:
        target_names = {
            canonicalize_strategy_name(
                str(name),
                source="risk.square_off_strategies.target",
                warn_alias=False,
            )
            for name in (strategy_names or set())
            if str(name or "").strip()
        }
        if not target_names:
            return set()

        with self._state_lock:
            positions_snapshot = list(self.open_positions.items())

        closed_labels: set[str] = set()
        for label, entry in positions_snapshot:
            strategy_name = canonicalize_strategy_name(
                str(entry.get("strategy_name") or "").strip(),
                source="risk.square_off_strategies.entry",
                warn_alias=False,
            )
            if strategy_name not in target_names:
                continue

            side = str(entry.get("side", "SELL")).upper()
            qty = int(entry.get("qty", 0) or 0)
            if qty <= 0:
                continue
            # Avoid duplicate submit while prior exit is pending broker confirmation.
            if self._pending_order_for_label(label):
                continue

            exit_side = "BUY" if side == "SELL" else "SELL"
            price = float(entry.get("entry_price", 0.0) or 0.0)
            if ltp_lookup and label in ltp_lookup:
                price = float(ltp_lookup[label])

            meta = self.instrument_meta.get(label, {})
            exchange = entry.get("exchange") or meta.get("exchange", "NSE")
            symboltoken = entry.get("symboltoken") or meta.get("token")
            tradingsymbol = entry.get("tradingsymbol") or meta.get("symbol", label)
            if not symboltoken:
                logger.warning(
                    "[%s] Missing token for %s (strategy=%s), skipping square-off",
                    tag,
                    label,
                    strategy_name,
                )
                continue

            try:
                exit_reason = str(reason or tag or "STRATEGY_SQUARE_OFF")
                strategy_name_for_route = (
                    str(entry.get("strategy_name") or "").strip() or None
                )
                self._mark_forced_exit_suppression(label)
                self._cancel_pending_closing_orders_for_label(label)
                if self._submit_exit_via_hub(
                    label=label,
                    strategy_name=strategy_name_for_route,
                    exchange=str(exchange),
                    symboltoken=str(symboltoken),
                    tradingsymbol=str(tradingsymbol),
                    side=exit_side,
                    quantity=int(qty),
                    trade_mode=trade_mode,
                    tag=tag,
                    reason=exit_reason,
                ):
                    self._register_exit(label, price, qty)
                    closed_labels.add(label)
                    continue

                self._assert_direct_submit_allowed(
                    trade_mode=trade_mode,
                    exit_source="square_off_strategies",
                    label=label,
                )
                ok, terminal, broker_order_id, _order_row = self._submit_order(
                    exchange=exchange,
                    symboltoken=symboltoken,
                    tradingsymbol=tradingsymbol,
                    side=exit_side,
                    quantity=qty,
                    producttype="INTRADAY",
                    trade_mode=trade_mode,
                    tag=tag,
                )
                if not ok:
                    if (
                        trade_mode.upper() == "LIVE"
                        and broker_order_id
                        and terminal not in ("REJECTED", "CANCELLED", "EXPIRED", "ERROR")
                    ):
                        self._record_pending_order(
                            order_id=str(broker_order_id),
                            ctx={
                                "label": label,
                                "side": exit_side,
                                "qty": int(qty),
                                "price": float(price),
                                "template_name": entry.get("template_name"),
                                "role": entry.get("role"),
                                "underlying": entry.get("underlying"),
                                "strategy_name": entry.get("strategy_name"),
                                "reason": entry.get("reason") or tag,
                                "ml_info": entry.get("ml_info"),
                                "count_for_spreads": False,
                                "is_closing": True,
                                "exchange": exchange,
                                "symboltoken": symboltoken,
                                "tradingsymbol": tradingsymbol,
                                "tag": tag,
                                "product_type": "INTRADAY",
                            },
                        )
                    continue

                self._register_exit(label, price, qty)
                closed_labels.add(label)
            except P0RuleViolation:
                logger.warning(
                    "Direct broker submit blocked for %s in hub-authoritative LIVE (square_off_strategies)",
                    label,
                )
            except Exception as exc:
                logger.error(
                    "[%s] Square-off failed for %s (strategy=%s): %s",
                    tag,
                    label,
                    strategy_name,
                    exc,
                )
        return closed_labels
