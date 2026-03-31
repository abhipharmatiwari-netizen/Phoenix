"""
OBS-5.6: Standardized reason codes for policy decisions.

Every interceptor decision (allow/block/resize) is recorded with a structured
reason code and context fields, surfaced via dashboard API and structured logs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


class PolicyAction(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"
    RESIZE = "resize"
    SKIP = "skip"  # Interceptor not applicable (e.g., exit order)


class PolicySource(str, Enum):
    CAPITAL_RESIZE = "capital_resize"
    CAPITAL_GUARD = "capital_guard"
    RISK_GUARD = "risk_guard"
    PROFIT_GUARD = "profit_guard"
    KILL_SWITCH = "kill_switch"
    POSITION_OWNERSHIP = "position_ownership"
    EXPOSURE_LIMIT = "exposure_limit"
    CIRCUIT_BREAKER = "circuit_breaker"
    IDEMPOTENCY = "idempotency"


@dataclass
class PolicyDecision:
    """Structured record of a single policy check result."""

    source: PolicySource
    action: PolicyAction
    reason_code: str  # e.g. "CAPITAL_INSUFFICIENT", "RISK_DAILY_LOSS_EXCEEDED"
    message: str  # Human-readable explanation
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.value,
            "action": self.action.value,
            "reason_code": self.reason_code,
            "message": self.message,
            "details": self.details,
        }


@dataclass
class PolicyAuditTrail:
    """Collects all policy decisions for a single order submission."""

    hub_order_id: Optional[str] = None
    decisions: list[PolicyDecision] = field(default_factory=list)

    def record(self, decision: PolicyDecision) -> None:
        self.decisions.append(decision)
        if decision.action == PolicyAction.BLOCK:
            logger.info(
                "policy_decision hub_order_id=%s source=%s action=BLOCK reason=%s message=%s",
                self.hub_order_id or "?",
                decision.source.value,
                decision.reason_code,
                decision.message,
            )

    @property
    def final_action(self) -> PolicyAction:
        """Return the most restrictive action across all decisions."""
        if any(d.action == PolicyAction.BLOCK for d in self.decisions):
            return PolicyAction.BLOCK
        if any(d.action == PolicyAction.RESIZE for d in self.decisions):
            return PolicyAction.RESIZE
        return PolicyAction.ALLOW

    @property
    def blocked(self) -> bool:
        return self.final_action == PolicyAction.BLOCK

    @property
    def block_reasons(self) -> list[PolicyDecision]:
        return [d for d in self.decisions if d.action == PolicyAction.BLOCK]

    def to_summary(self) -> dict[str, Any]:
        return {
            "hub_order_id": self.hub_order_id,
            "final_action": self.final_action.value,
            "decision_count": len(self.decisions),
            "decisions": [d.to_dict() for d in self.decisions],
        }


# ------------------------------------------------------------------ #
# Standard reason codes
# ------------------------------------------------------------------ #

# Capital
RC_CAPITAL_INSUFFICIENT = "CAPITAL_INSUFFICIENT"
RC_CAPITAL_RESIZE_APPLIED = "CAPITAL_RESIZE_APPLIED"
RC_CAPITAL_CHECKS_DISABLED = "CAPITAL_CHECKS_DISABLED"

# Risk
RC_RISK_DAILY_LOSS_EXCEEDED = "RISK_DAILY_LOSS_EXCEEDED"
RC_RISK_CHECKS_DISABLED = "RISK_CHECKS_DISABLED"
RC_RISK_PNL_UNAVAILABLE = "RISK_PNL_UNAVAILABLE"

# Profit
RC_PROFIT_TARGET_REACHED = "PROFIT_TARGET_REACHED"
RC_PROFIT_CHECKS_DISABLED = "PROFIT_CHECKS_DISABLED"

# Kill switch
RC_KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"

# Position ownership
RC_OWNERSHIP_CROSS_STRATEGY = "OWNERSHIP_CROSS_STRATEGY"
RC_OWNERSHIP_UNKNOWN_CONTRACT = "OWNERSHIP_UNKNOWN_CONTRACT"

# Exposure
RC_EXPOSURE_MAX_POSITIONS = "EXPOSURE_MAX_POSITIONS"
RC_EXPOSURE_MAX_NOTIONAL = "EXPOSURE_MAX_NOTIONAL"
RC_EXPOSURE_MAX_SHORT_OPTIONS = "EXPOSURE_MAX_SHORT_OPTIONS"

# Circuit breaker
RC_CIRCUIT_BREAKER_TRIPPED = "CIRCUIT_BREAKER_TRIPPED"

# Idempotency
RC_IDEMPOTENCY_DUPLICATE = "IDEMPOTENCY_DUPLICATE"

# Exit bypass
RC_EXIT_ORDER_BYPASS = "EXIT_ORDER_BYPASS"


__all__ = [
    "PolicyAction",
    "PolicySource",
    "PolicyDecision",
    "PolicyAuditTrail",
    "RC_CAPITAL_INSUFFICIENT",
    "RC_CAPITAL_RESIZE_APPLIED",
    "RC_CAPITAL_CHECKS_DISABLED",
    "RC_RISK_DAILY_LOSS_EXCEEDED",
    "RC_RISK_CHECKS_DISABLED",
    "RC_PROFIT_TARGET_REACHED",
    "RC_PROFIT_CHECKS_DISABLED",
    "RC_KILL_SWITCH_ACTIVE",
    "RC_OWNERSHIP_CROSS_STRATEGY",
    "RC_OWNERSHIP_UNKNOWN_CONTRACT",
    "RC_EXPOSURE_MAX_POSITIONS",
    "RC_EXPOSURE_MAX_NOTIONAL",
    "RC_EXPOSURE_MAX_SHORT_OPTIONS",
    "RC_CIRCUIT_BREAKER_TRIPPED",
    "RC_IDEMPOTENCY_DUPLICATE",
    "RC_EXIT_ORDER_BYPASS",
]
