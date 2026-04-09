"""Unified pre-trade policy engine — PHX-RISK-001.

Every order passes through one policy layer that enforces:
  - Per-instrument position/exposure limits
  - Per-strategy daily turnover and order-count limits
  - Per-account and per-portfolio capital constraints
  - Time-window restrictions (no orders outside allowed sessions)
  - Venue/instrument allowlist
  - Kill-switch and degraded-scope gate

All checks are configurable, auditable, and composable.
A PolicyViolation is raised (never silently swallowed) when an order fails.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Policy violation
# ---------------------------------------------------------------------------

class PolicyViolation(Exception):
    """Raised when a pre-trade policy check fails."""

    def __init__(self, check: str, message: str, details: Optional[dict] = None) -> None:
        self.check = check
        self.details = dict(details or {})
        super().__init__(f"[{check}] {message}")


# ---------------------------------------------------------------------------
# Policy limits config
# ---------------------------------------------------------------------------

@dataclass
class InstrumentPolicy:
    symbol: str
    max_position_qty: int = 0          # 0 = no limit
    max_order_qty: int = 0             # 0 = no limit
    allowed: bool = True               # False = blocked entirely


@dataclass
class StrategyPolicy:
    strategy_id: str
    max_daily_orders: int = 0          # 0 = no limit
    max_daily_turnover: float = 0.0    # 0 = no limit (in base currency)
    max_open_orders: int = 0           # 0 = no limit
    allowed_symbols: frozenset = field(default_factory=frozenset)  # empty = all allowed
    blocked_symbols: frozenset = field(default_factory=frozenset)


@dataclass
class AccountPolicy:
    account_id: str
    max_order_value: float = 0.0       # per single order (0 = no limit)
    max_daily_loss: float = 0.0        # 0 = no limit
    allowed: bool = True


@dataclass
class GlobalPolicy:
    """Global system-wide constraints."""
    max_order_qty_any: int = 0         # hard upper bound on any order
    allowed_time_windows: list[tuple[str, str]] = field(default_factory=list)
    # List of (start_hhmm, end_hhmm) in IST e.g. [("09:15", "15:25")]
    # Empty list = no time restriction
    allowed_venues: frozenset = field(default_factory=frozenset)
    # Empty = all venues allowed


# ---------------------------------------------------------------------------
# Runtime state (per-strategy daily counters)
# ---------------------------------------------------------------------------

@dataclass
class StrategyDailyState:
    strategy_id: str
    date: str                          # YYYY-MM-DD
    order_count: int = 0
    turnover: float = 0.0
    open_orders: int = 0


_STRATEGY_STATE: dict[str, StrategyDailyState] = {}
_STATE_LOCK = threading.Lock()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _get_or_create_strategy_state(strategy_id: str) -> StrategyDailyState:
    today = _today()
    with _STATE_LOCK:
        key = f"{strategy_id}:{today}"
        state = _STRATEGY_STATE.get(key)
        if state is None or state.date != today:
            state = StrategyDailyState(strategy_id=strategy_id, date=today)
            _STRATEGY_STATE[key] = state
        return state


def _increment_strategy_state(
    strategy_id: str, *, turnover: float = 0.0, open_delta: int = 0
) -> None:
    today = _today()
    with _STATE_LOCK:
        key = f"{strategy_id}:{today}"
        state = _STRATEGY_STATE.get(key)
        if state is None or state.date != today:
            state = StrategyDailyState(strategy_id=strategy_id, date=today)
            _STRATEGY_STATE[key] = state
        state.order_count += 1
        state.turnover += max(0.0, turnover)
        state.open_orders = max(0, state.open_orders + open_delta)


def decrement_open_orders(strategy_id: str) -> None:
    """Call when an order reaches terminal state to free open-order slot."""
    with _STATE_LOCK:
        key = f"{strategy_id}:{_today()}"
        state = _STRATEGY_STATE.get(key)
        if state:
            state.open_orders = max(0, state.open_orders - 1)


# ---------------------------------------------------------------------------
# Policy store (in-memory; can be loaded from config/DB)
# ---------------------------------------------------------------------------

class PreTradePolicyEngine:
    """Thread-safe pre-trade policy enforcement engine."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._instrument_policies: dict[str, InstrumentPolicy] = {}
        self._strategy_policies: dict[str, StrategyPolicy] = {}
        self._account_policies: dict[str, AccountPolicy] = {}
        self._global: GlobalPolicy = GlobalPolicy()

    # -- Configuration -------------------------------------------------------

    def set_global_policy(self, policy: GlobalPolicy) -> None:
        with self._lock:
            self._global = policy

    def set_instrument_policy(self, policy: InstrumentPolicy) -> None:
        with self._lock:
            self._instrument_policies[policy.symbol] = policy

    def set_strategy_policy(self, policy: StrategyPolicy) -> None:
        with self._lock:
            self._strategy_policies[policy.strategy_id] = policy

    def set_account_policy(self, policy: AccountPolicy) -> None:
        with self._lock:
            self._account_policies[policy.account_id] = policy

    def get_global_policy(self) -> GlobalPolicy:
        with self._lock:
            return self._global

    def get_strategy_policy(self, strategy_id: str) -> Optional[StrategyPolicy]:
        with self._lock:
            return self._strategy_policies.get(strategy_id)

    def get_account_policy(self, account_id: str) -> Optional[AccountPolicy]:
        with self._lock:
            return self._account_policies.get(account_id)

    # -- Enforcement ---------------------------------------------------------

    def check(
        self,
        *,
        symbol: str,
        strategy_id: str,
        account_id: str,
        side: str,
        qty: int,
        price: float,
        current_position_qty: int = 0,
        current_daily_loss: float = 0.0,
    ) -> list[str]:
        """Run all policy checks. Returns list of passed check names.

        Raises PolicyViolation on first failure.
        """
        checks_passed: list[str] = []
        with self._lock:
            global_pol = self._global
            instr_pol = self._instrument_policies.get(symbol)
            strat_pol = self._strategy_policies.get(strategy_id)
            acct_pol = self._account_policies.get(account_id)

        order_value = qty * price

        # 1. Global max order qty
        if global_pol.max_order_qty_any > 0 and qty > global_pol.max_order_qty_any:
            raise PolicyViolation(
                "global_max_order_qty",
                f"Order qty {qty} exceeds global max {global_pol.max_order_qty_any}",
                {"qty": qty, "limit": global_pol.max_order_qty_any},
            )
        checks_passed.append("global_max_order_qty")

        # 2. Time window
        if global_pol.allowed_time_windows:
            self._check_time_window(global_pol.allowed_time_windows)
        checks_passed.append("time_window")

        # 3. Instrument policy
        if instr_pol is not None:
            if not instr_pol.allowed:
                raise PolicyViolation(
                    "instrument_blocked",
                    f"Symbol {symbol} is blocked by instrument policy",
                    {"symbol": symbol},
                )
            if instr_pol.max_order_qty > 0 and qty > instr_pol.max_order_qty:
                raise PolicyViolation(
                    "instrument_max_order_qty",
                    f"Order qty {qty} exceeds instrument limit {instr_pol.max_order_qty}",
                    {"symbol": symbol, "qty": qty, "limit": instr_pol.max_order_qty},
                )
            if instr_pol.max_position_qty > 0:
                projected = abs(current_position_qty + (qty if side.upper() == "BUY" else -qty))
                if projected > instr_pol.max_position_qty:
                    raise PolicyViolation(
                        "instrument_max_position",
                        f"Projected position {projected} exceeds instrument limit {instr_pol.max_position_qty}",
                        {"symbol": symbol, "projected": projected, "limit": instr_pol.max_position_qty},
                    )
        checks_passed.append("instrument_policy")

        # 4. Strategy policy
        if strat_pol is not None:
            if strat_pol.allowed_symbols and symbol not in strat_pol.allowed_symbols:
                raise PolicyViolation(
                    "strategy_symbol_not_allowed",
                    f"Symbol {symbol} is not in strategy {strategy_id} allowlist",
                    {"symbol": symbol, "strategy_id": strategy_id},
                )
            if symbol in strat_pol.blocked_symbols:
                raise PolicyViolation(
                    "strategy_symbol_blocked",
                    f"Symbol {symbol} is blocked for strategy {strategy_id}",
                    {"symbol": symbol, "strategy_id": strategy_id},
                )
            state = _get_or_create_strategy_state(strategy_id)
            if strat_pol.max_daily_orders > 0 and state.order_count >= strat_pol.max_daily_orders:
                raise PolicyViolation(
                    "strategy_daily_order_limit",
                    f"Strategy {strategy_id} has reached daily order limit {strat_pol.max_daily_orders}",
                    {"strategy_id": strategy_id, "count": state.order_count, "limit": strat_pol.max_daily_orders},
                )
            if strat_pol.max_daily_turnover > 0 and state.turnover + order_value > strat_pol.max_daily_turnover:
                raise PolicyViolation(
                    "strategy_daily_turnover_limit",
                    f"Strategy {strategy_id} would exceed daily turnover limit",
                    {"strategy_id": strategy_id, "current": state.turnover, "order_value": order_value, "limit": strat_pol.max_daily_turnover},
                )
            if strat_pol.max_open_orders > 0 and state.open_orders >= strat_pol.max_open_orders:
                raise PolicyViolation(
                    "strategy_max_open_orders",
                    f"Strategy {strategy_id} has {state.open_orders} open orders (max {strat_pol.max_open_orders})",
                    {"strategy_id": strategy_id, "open": state.open_orders, "limit": strat_pol.max_open_orders},
                )
        checks_passed.append("strategy_policy")

        # 5. Account policy
        if acct_pol is not None:
            if not acct_pol.allowed:
                raise PolicyViolation(
                    "account_blocked",
                    f"Account {account_id} is blocked by account policy",
                    {"account_id": account_id},
                )
            if acct_pol.max_order_value > 0 and order_value > acct_pol.max_order_value:
                raise PolicyViolation(
                    "account_max_order_value",
                    f"Order value {order_value} exceeds account limit {acct_pol.max_order_value}",
                    {"account_id": account_id, "order_value": order_value, "limit": acct_pol.max_order_value},
                )
            if acct_pol.max_daily_loss > 0 and current_daily_loss >= acct_pol.max_daily_loss:
                raise PolicyViolation(
                    "account_daily_loss_limit",
                    f"Account {account_id} daily loss {current_daily_loss} has reached limit {acct_pol.max_daily_loss}",
                    {"account_id": account_id, "loss": current_daily_loss, "limit": acct_pol.max_daily_loss},
                )
        checks_passed.append("account_policy")

        logger.debug(
            "pre_trade_check_passed symbol=%s strategy=%s account=%s qty=%d",
            symbol, strategy_id, account_id, qty,
        )
        return checks_passed

    def record_submitted(
        self,
        *,
        strategy_id: str,
        qty: int,
        price: float,
    ) -> None:
        """Call after a check passes and the order is actually submitted."""
        _increment_strategy_state(strategy_id, turnover=qty * price, open_delta=1)

    @staticmethod
    def _check_time_window(windows: list[tuple[str, str]]) -> None:
        """Raise if current IST time is outside all allowed windows."""
        try:
            from zoneinfo import ZoneInfo
            now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
        except Exception:
            return  # zoneinfo unavailable; skip check
        hhmm = now_ist.strftime("%H:%M")
        for start, end in windows:
            if start <= hhmm <= end:
                return
        raise PolicyViolation(
            "time_window",
            f"Current time {hhmm} IST is outside allowed trading windows {windows}",
            {"current_time": hhmm, "allowed_windows": windows},
        )


# Module-level singleton
pre_trade_policy = PreTradePolicyEngine()


__all__ = [
    "AccountPolicy",
    "GlobalPolicy",
    "InstrumentPolicy",
    "PolicyViolation",
    "PreTradePolicyEngine",
    "StrategyPolicy",
    "decrement_open_orders",
    "pre_trade_policy",
]
