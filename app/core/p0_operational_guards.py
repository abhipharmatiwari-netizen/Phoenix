"""Explicit guards for P0 operational rules 9, 12, 21 (Architecture S20).

Rule 9:  Dashboard data is derived and non-authoritative.
Rule 12: Hub-authoritative LIVE mode forbids legacy automated exits
         except audited break-glass.
Rule 21: Non-authoritative components may propose but never commit
         state directly.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class P0RuleViolation(RuntimeError):
    """Raised when a P0 operational rule is violated."""

    def __init__(self, rule_number: int, message: str) -> None:
        self.rule_number = rule_number
        super().__init__(f"P0 Rule {rule_number} violation: {message}")


# ---------------------------------------------------------------------------
# Rule 9: Dashboard data is non-authoritative
# ---------------------------------------------------------------------------

def assert_dashboard_not_authoritative(
    *,
    caller: str,
    action: str,
) -> None:
    """Assert that dashboard state is never used for position/order decisions.

    Rule 9: Dashboard data is derived from authoritative stores and must never
    be used as input for trading decisions, position mutations, or order routing.
    """
    dashboard_callers = (
        "dashboard", "dashboard_bus", "websocket", "ui", "frontend",
        "bff", "display", "view",
    )
    caller_lower = caller.lower()
    if any(dc in caller_lower for dc in dashboard_callers):
        raise P0RuleViolation(
            9,
            f"Dashboard-sourced caller '{caller}' attempted authoritative action "
            f"'{action}'. Dashboard data is derived and non-authoritative (Rule 9).",
        )


# ---------------------------------------------------------------------------
# Rule 12: Hub-authoritative forbids legacy automated exits
# ---------------------------------------------------------------------------

def assert_legacy_exit_allowed(
    *,
    operating_mode: str,
    is_break_glass: bool = False,
    exit_source: str,
    scope_key: str,
) -> None:
    """Assert that legacy automated exits are allowed in the current mode.

    Rule 12: In hub-authoritative LIVE mode, legacy stream-side automated exits
    are read-only except for an audited break-glass emergency path.
    """
    if operating_mode.upper() in ("HUB_AUTHORITATIVE", "HUB"):
        if not is_break_glass:
            raise P0RuleViolation(
                12,
                f"Legacy automated exit from '{exit_source}' blocked for scope "
                f"'{scope_key}' in hub-authoritative mode. Only audited break-glass "
                f"exits are permitted (Rule 12).",
            )
        logger.info(
            "p0_rule_12: break-glass legacy exit allowed for scope=%s source=%s",
            scope_key, exit_source,
        )


# ---------------------------------------------------------------------------
# Rule 21: Non-authoritative components must not commit state
# ---------------------------------------------------------------------------

_AUTHORITATIVE_MUTATORS = frozenset({
    "order_lifecycle_service",
    "position_ownership_store",
    "state_store",
    "risk_manager",
    "scope_serializer",
    "reconciliation_engine",
    "break_glass",
    "admin",
    "operator",
})


def assert_caller_is_authoritative(
    *,
    caller: str,
    action: str,
    scope_key: str,
) -> None:
    """Assert that the caller is an authoritative mutator.

    Rule 21: Non-authoritative components (dashboard, indicators, strategies)
    may propose actions but never commit state mutations directly.
    """
    caller_lower = caller.lower().strip()
    if caller_lower not in _AUTHORITATIVE_MUTATORS:
        # Check if the caller contains any authoritative prefix
        if not any(am in caller_lower for am in _AUTHORITATIVE_MUTATORS):
            raise P0RuleViolation(
                21,
                f"Non-authoritative caller '{caller}' attempted state mutation "
                f"'{action}' on scope '{scope_key}'. Only authoritative components "
                f"may commit state directly (Rule 21).",
            )


__all__ = [
    "P0RuleViolation",
    "assert_caller_is_authoritative",
    "assert_dashboard_not_authoritative",
    "assert_legacy_exit_allowed",
]
