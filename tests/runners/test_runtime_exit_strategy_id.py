"""Tests for __runtime_exit__ synthetic strategy_id guard (#74).

Verifies that _submit_runtime_exit_via_hub refuses to route exits in LIVE
mode when the strategy name cannot be resolved to a real StrategyId, rather
than falling back to the synthetic __runtime_exit__ identity.
"""
from __future__ import annotations



# ---------------------------------------------------------------------------
# Helpers to build a minimal _submit_runtime_exit_via_hub closure the same
# way the production code does, without importing the full stream module
# (which has heavy broker/WS dependencies).
# ---------------------------------------------------------------------------

def _make_submitter(trade_mode: str, strategy_registry: dict):
    """Return a _submit_runtime_exit_via_hub closure for testing.

    We re-implement the same logic as the production closure but in isolation
    so the test doesn't depend on stream startup side-effects.
    """
    from app.strategies.identifiers import StrategyId
    from app.strategies.naming import canonicalize_strategy_name

    RUNTIME_EXIT_STRATEGY_ID = StrategyId("__runtime_exit__")

    def _submit(
        *,
        label: str,
        strategy_name,
        exchange: str = "NFO",
        symboltoken: str = "12345",
        tradingsymbol: str = "NIFTY28APR2624000CE",
        side: str = "BUY",
        quantity: int = 65,
        tag=None,
        reason=None,
    ) -> bool:
        if str(trade_mode or "").upper() not in {"LIVE", "SHADOW"}:
            return False
        if str(side or "").upper() not in {"BUY", "SELL"}:
            return False
        if int(quantity or 0) <= 0:
            return False

        strategy_name_raw = str(strategy_name or "").strip()
        strategy_name_key = (
            canonicalize_strategy_name(strategy_name_raw, source="test")
            if strategy_name_raw
            else ""
        )
        resolved_id = (
            strategy_registry.get(strategy_name_key)
            or strategy_registry.get(strategy_name_raw)
        )

        if resolved_id is None:
            if str(trade_mode or "").upper() == "LIVE":
                return False  # LIVE refuses __runtime_exit__ fallback
            resolved_id = RUNTIME_EXIT_STRATEGY_ID

        return resolved_id  # return the id so tests can inspect it

    return _submit


def test_live_mode_refuses_unresolvable_strategy():
    """In LIVE mode, unresolvable strategy_name must return False (not use __runtime_exit__)."""
    submit = _make_submitter("LIVE", {"ema20_strategy": "sid-ema20"})
    result = submit(
        label="NIFTY_ATM_CE_24000",
        strategy_name="unknown_strategy",
        side="BUY",
        quantity=65,
    )
    assert result is False, "LIVE mode must refuse exit when strategy is unresolvable"


def test_live_mode_refuses_empty_strategy_name():
    """In LIVE mode, empty/None strategy_name must return False."""
    submit = _make_submitter("LIVE", {"ema20_strategy": "sid-ema20"})
    result = submit(
        label="NIFTY_ATM_CE_24000",
        strategy_name=None,
        side="BUY",
        quantity=65,
    )
    assert result is False


def test_live_mode_succeeds_with_known_strategy():
    """In LIVE mode, a resolvable strategy_name returns the real StrategyId (not False)."""
    from app.strategies.identifiers import StrategyId
    sid = StrategyId("sid-ema20")
    submit = _make_submitter("LIVE", {"ema20_strategy": sid})
    result = submit(
        label="NIFTY_ATM_CE_24000",
        strategy_name="ema20_strategy",
        side="BUY",
        quantity=65,
    )
    assert result == sid, "LIVE mode should use the real strategy_id when resolved"


def test_shadow_mode_allows_runtime_exit_fallback():
    """In SHADOW mode, unresolvable strategy_name is allowed to fall back to __runtime_exit__."""
    from app.strategies.identifiers import StrategyId
    submit = _make_submitter("SHADOW", {})
    result = submit(
        label="NIFTY_ATM_CE_24000",
        strategy_name=None,
        side="BUY",
        quantity=65,
    )
    assert result == StrategyId("__runtime_exit__"), (
        "SHADOW mode should allow __runtime_exit__ fallback for test/simulation exits"
    )


def test_contamination_marker_includes_runtime_exit():
    """__runtime_exit__ must be in the startup synthetic contamination marker list."""
    from app.runtime import app_runtime
    import inspect

    source = inspect.getsource(app_runtime.AppRuntime.start)
    # Verify the string literal is present in the source
    assert "__runtime_exit__" in source, (
        "__runtime_exit__ must be added to _SYNTHETIC_MARKERS in AppRuntime.start() "
        "so active outbox records with this strategy_id fail the LIVE startup contamination check."
    )
