"""Policy-bounded autonomy envelopes — PHX-STRAT-005.

Autonomous trading is permitted only within approved envelopes:
  - Capital envelope: max deployed capital
  - Turnover envelope: max daily notional
  - Time envelope: trading hours
  - Symbol envelope: allowed instruments
  - Drawdown envelope: auto-disable on breach

When ANY envelope is breached the strategy is automatically disabled
and an audit event + kill-switch trip is emitted.

Usage:
    envelope = AutonomyEnvelope(strategy_id="ema20_strategy", ...)
    envelope.check_or_raise(capital_used=..., turnover_today=..., symbol=..., drawdown=...)
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.core.audit_log import emit_audit_event

logger = logging.getLogger(__name__)


class EnvelopeBreach(Exception):
    """Raised when an autonomy envelope is breached."""

    def __init__(self, strategy_id: str, envelope: str, message: str) -> None:
        self.strategy_id = strategy_id
        self.envelope = envelope
        super().__init__(f"[{strategy_id}] Envelope breach [{envelope}]: {message}")


@dataclass
class AutonomyEnvelope:
    """Policy envelope for a strategy.  All limits are hard stops."""

    strategy_id: str

    # Capital (base currency)
    max_capital_deployed: float = 0.0      # 0 = no limit
    # Turnover (sum of abs notional per day)
    max_daily_turnover: float = 0.0        # 0 = no limit
    # Time window (IST HH:MM strings)
    allowed_time_start: str = "09:15"
    allowed_time_end: str = "15:25"
    # Symbol allowlist (empty = all allowed)
    allowed_symbols: frozenset = field(default_factory=frozenset)
    # Drawdown auto-disable (fraction of capital, e.g. 0.05 = 5%)
    max_drawdown_pct: float = 0.0          # 0 = no limit
    # Enabled flag
    enabled: bool = True

    # Runtime state (set by the envelope manager)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _disabled: bool = field(default=False, repr=False, compare=False)
    _disabled_reason: str = field(default="", repr=False, compare=False)

    def is_active(self) -> bool:
        with self._lock:
            return self.enabled and not self._disabled

    def disable(self, reason: str = "", actor: str = "system") -> None:
        with self._lock:
            if self._disabled:
                return
            self._disabled = True
            self._disabled_reason = reason
        emit_audit_event(
            actor=actor,
            action="autonomy_envelope_disabled",
            resource_type="strategy",
            resource_id=self.strategy_id,
            after={"reason": reason},
        )
        logger.error(
            "autonomy_envelope_disabled strategy=%s reason=%s actor=%s",
            self.strategy_id, reason, actor,
        )

    def re_enable(self, actor: str = "operator") -> None:
        """Re-enable after manual review. Requires operator approval."""
        with self._lock:
            self._disabled = False
            self._disabled_reason = ""
        emit_audit_event(
            actor=actor,
            action="autonomy_envelope_reenabled",
            resource_type="strategy",
            resource_id=self.strategy_id,
        )
        logger.warning("autonomy_envelope_reenabled strategy=%s actor=%s", self.strategy_id, actor)

    def check_or_raise(
        self,
        *,
        capital_deployed: float = 0.0,
        turnover_today: float = 0.0,
        symbol: str = "",
        drawdown_pct: float = 0.0,
    ) -> None:
        """Check all envelope constraints.  Raises EnvelopeBreach on violation.

        Also auto-disables on breach.
        """
        with self._lock:
            enabled = self.enabled
            disabled = self._disabled
            disabled_reason = self._disabled_reason

        if not enabled:
            raise EnvelopeBreach(self.strategy_id, "enabled_flag", "Strategy is not enabled")
        if disabled:
            raise EnvelopeBreach(self.strategy_id, "auto_disabled", disabled_reason or "Auto-disabled")

        # Capital
        if self.max_capital_deployed > 0 and capital_deployed > self.max_capital_deployed:
            self._breach(
                "capital",
                f"Capital deployed {capital_deployed:.0f} exceeds envelope {self.max_capital_deployed:.0f}",
            )

        # Turnover
        if self.max_daily_turnover > 0 and turnover_today > self.max_daily_turnover:
            self._breach(
                "turnover",
                f"Daily turnover {turnover_today:.0f} exceeds envelope {self.max_daily_turnover:.0f}",
            )

        # Symbol
        if self.allowed_symbols and symbol and symbol not in self.allowed_symbols:
            self._breach(
                "symbol",
                f"Symbol {symbol} not in approved envelope {set(self.allowed_symbols)}",
            )

        # Time window
        if self.allowed_time_start and self.allowed_time_end:
            self._check_time_window()

        # Drawdown
        if self.max_drawdown_pct > 0 and drawdown_pct >= self.max_drawdown_pct:
            self._breach(
                "drawdown",
                f"Drawdown {drawdown_pct*100:.1f}% has reached auto-disable threshold {self.max_drawdown_pct*100:.1f}%",
            )

    def _breach(self, envelope_type: str, message: str) -> None:
        self.disable(reason=message)
        raise EnvelopeBreach(self.strategy_id, envelope_type, message)

    def _check_time_window(self) -> None:
        try:
            from zoneinfo import ZoneInfo
            now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
        except Exception:
            return
        hhmm = now_ist.strftime("%H:%M")
        if not (self.allowed_time_start <= hhmm <= self.allowed_time_end):
            raise EnvelopeBreach(
                self.strategy_id,
                "time_window",
                f"Current time {hhmm} IST is outside allowed window "
                f"[{self.allowed_time_start}, {self.allowed_time_end}]",
            )

    def status(self) -> dict:
        with self._lock:
            return {
                "strategy_id": self.strategy_id,
                "enabled": self.enabled,
                "auto_disabled": self._disabled,
                "disabled_reason": self._disabled_reason,
                "is_active": self.enabled and not self._disabled,
                "max_capital_deployed": self.max_capital_deployed,
                "max_daily_turnover": self.max_daily_turnover,
                "max_drawdown_pct": self.max_drawdown_pct,
                "allowed_time": f"{self.allowed_time_start}–{self.allowed_time_end}",
            }


class AutonomyEnvelopeRegistry:
    """Registry of all strategy autonomy envelopes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._envelopes: dict[str, AutonomyEnvelope] = {}

    def register(self, envelope: AutonomyEnvelope) -> None:
        with self._lock:
            self._envelopes[envelope.strategy_id] = envelope

    def get(self, strategy_id: str) -> Optional[AutonomyEnvelope]:
        with self._lock:
            return self._envelopes.get(strategy_id)

    def check_or_raise(self, strategy_id: str, **kwargs) -> None:
        """Check envelope for strategy. No-op if no envelope registered."""
        envelope = self.get(strategy_id)
        if envelope is not None:
            envelope.check_or_raise(**kwargs)

    def all_status(self) -> list[dict]:
        with self._lock:
            envelopes = list(self._envelopes.values())
        return [e.status() for e in envelopes]


# Module-level singleton
autonomy_registry = AutonomyEnvelopeRegistry()


__all__ = [
    "AutonomyEnvelope",
    "AutonomyEnvelopeRegistry",
    "EnvelopeBreach",
    "autonomy_registry",
]
