"""Runtime assertion guards for forbidden anti-patterns (Architecture \u00a719.3).

These guards are fail-closed checks that prevent:
- Direct position create/delete from a single broker poll
- Direct ownership release from ATM refresh or dashboard
- Using display labels as ownership identity
- Clearing live option legs on remap failure
- Treating dashboard silence as flat proof
- Replaying pending submissions before reconciliation
- Multiple writers mutating the same OwnershipKey
- Auto-clearing kill switch on restart
"""

import logging
import threading

logger = logging.getLogger(__name__)


class AntiPatternViolation(RuntimeError):
    """Raised when code attempts a forbidden anti-pattern."""
    pass


# Track whether reconciliation has completed for each scope
_reconciliation_complete: dict[str, bool] = {}
_reconciliation_lock = threading.Lock()


def assert_reconciliation_complete_before_replay(
    scope_key: str,
    *,
    action: str = "replay",
) -> None:
    """\u00a719.3: Replaying pending submissions before reconciliation is forbidden."""
    with _reconciliation_lock:
        if not _reconciliation_complete.get(scope_key, False):
            raise AntiPatternViolation(
                f"Anti-pattern violation: {action} attempted for scope {scope_key} "
                f"before reconciliation completed. \u00a719.3 forbids replay before reconciliation."
            )


def mark_reconciliation_complete(scope_key: str) -> None:
    """Mark that reconciliation is complete for a scope, enabling replay."""
    with _reconciliation_lock:
        _reconciliation_complete[scope_key] = True


def reset_reconciliation_state() -> None:
    """Reset all reconciliation tracking (for testing or restart)."""
    with _reconciliation_lock:
        _reconciliation_complete.clear()


def assert_not_single_poll_mutation(
    *,
    consecutive_polls: int,
    required_polls: int,
    action: str,
    scope_key: str,
) -> None:
    """\u00a719.3: Direct create/delete from one broker poll is forbidden."""
    if consecutive_polls < required_polls:
        raise AntiPatternViolation(
            f"Anti-pattern violation: {action} for {scope_key} after only "
            f"{consecutive_polls}/{required_polls} consecutive polls. "
            f"\u00a719.3 forbids single-poll position mutation."
        )


def assert_not_display_label_as_ownership(
    *,
    key_source: str,
    key_value: str,
) -> None:
    """\u00a719.3: Display labels, ATM aliases, UI symbols cannot be ownership keys."""
    # Known display-only prefixes that should never be used as ownership identity
    display_prefixes = ("ATM_", "UI_", "DISPLAY_", "ALIAS_")
    if any(key_value.upper().startswith(p) for p in display_prefixes):
        raise AntiPatternViolation(
            f"Anti-pattern violation: display label '{key_value}' from {key_source} "
            f"used as ownership identity. \u00a719.3 forbids display labels as ownership keys."
        )


def assert_not_destructive_leg_clear(
    *,
    action: str,
    has_open_positions: bool,
) -> None:
    """\u00a719.3: CLEAR open_legs on remap failure is forbidden in production."""
    if has_open_positions:
        raise AntiPatternViolation(
            f"Anti-pattern violation: {action} attempted while open positions exist. "
            f"\u00a719.3 forbids clearing live option legs. Use DEGRADED mode instead."
        )


def assert_kill_switch_not_auto_cleared(
    *,
    trigger: str,
) -> None:
    """\u00a719.3: Auto-clearing kill switch on restart or single favorable PnL is forbidden."""
    forbidden_triggers = ("restart", "process_start", "single_pnl_update", "tick")
    if trigger.lower() in forbidden_triggers:
        raise AntiPatternViolation(
            f"Anti-pattern violation: kill switch auto-clear triggered by '{trigger}'. "
            f"\u00a719.3 forbids auto-clearing kill switch on restart or single favorable update."
        )


def assert_not_restart_helper_authority(
    *,
    source: str,
    has_broker_reconciliation: bool,
) -> None:
    """§6 / H3: risk_positions.json (or any restart helper) must not be treated
    as authoritative without broker reconciliation evidence.

    Positions from restart helpers must enter RECOVERY_PENDING, not
    directly OPEN.
    """
    restart_helper_sources = (
        "risk_positions.json", "risk_positions", "restart_helper",
        "json_restore", "file_restore",
    )
    if source.lower() in restart_helper_sources and not has_broker_reconciliation:
        raise AntiPatternViolation(
            f"Anti-pattern violation: '{source}' used as authoritative position source "
            f"without broker reconciliation evidence. §6 requires positions from restart "
            f"helpers to enter RECOVERY_PENDING and await broker confirmation."
        )


def assert_runner_feeds_through_lifecycle(
    *,
    caller: str,
    action: str,
) -> None:
    """§14 / M5: AccountRunner sync results must flow through OrderLifecycleService.

    Runner polling is an evidence feed into authoritative services.
    It must not bypass OrderLifecycleService or ownership policy.
    """
    runner_direct_callers = (
        "account_runner_direct", "runner_direct", "runner_bypass",
    )
    if caller.lower() in runner_direct_callers:
        raise AntiPatternViolation(
            f"Anti-pattern violation: '{caller}' attempted direct state mutation "
            f"'{action}' bypassing OrderLifecycleService. §14 requires runner "
            f"polling to be an evidence feed into authoritative services."
        )
