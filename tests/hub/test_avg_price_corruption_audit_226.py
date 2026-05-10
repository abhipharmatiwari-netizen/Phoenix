"""Tests for issue #226: HubRuntime.audit_position_avg_price_corruption.

The dashboard already detects ``qty != 0 and avg_price <= 0`` per request
(``POSITION_VIEW_AVG_PRICE_INVALID``) but that is reactive. This audit is
proactive — runs on every readiness probe — so corruption cannot hide
between dashboard views. The 2026-05-08 incident logged
``POSITION_VIEW_AVG_PRICE_INVALID qty=7500 avg_price=0.0`` for
NATURALGAS22MAY26265CE while ``BROKER_SYNC`` was suppressed by the
legacy kill switch.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace


def _make_runtime_with_positions(positions_by_account: dict):
    """Build a minimal HubRuntime-shaped stub bound to the real audit
    method so we test the exact production path."""
    from app.hub.runtime import HubRuntime

    rt = SimpleNamespace()
    # Hub stub returns the account ids the audit should iterate.
    rt.hub = SimpleNamespace(
        list_runner_ids=lambda: list(positions_by_account.keys()),
    )
    # State store stub returns the per-account positions.
    rt.state_store = SimpleNamespace(
        get_positions=lambda acct: positions_by_account.get(acct, []),
    )
    # Bind the real audit + alert helpers.
    rt.audit_position_avg_price_corruption = (
        HubRuntime.audit_position_avg_price_corruption.__get__(rt, HubRuntime)
    )
    rt._maybe_emit_avg_price_corruption_alert = (
        HubRuntime._maybe_emit_avg_price_corruption_alert.__get__(rt, HubRuntime)
    )
    return rt


def _pos(symbol: str, qty: int, avg_price):
    return SimpleNamespace(
        symbol=symbol, quantity=qty, avg_price=avg_price, tenant_id="t-1",
    )


def test_audit_zero_corrupt_when_all_positions_healthy():
    rt = _make_runtime_with_positions(
        {
            "A1": [
                _pos("NIFTY24DEC25000CE", 50, 100.0),
                _pos("NG22MAY26265CE", 1250, 14.30),
            ]
        }
    )
    result = rt.audit_position_avg_price_corruption()
    assert result["corrupt_count"] == 0
    assert result["samples"] == []


def test_audit_skips_zero_qty_positions_even_if_avg_price_is_zero():
    """qty == 0 means the position is closed; avg_price=0 there is
    expected and not corruption."""
    rt = _make_runtime_with_positions(
        {"A1": [_pos("CLOSED_SYM", 0, 0.0)]}
    )
    result = rt.audit_position_avg_price_corruption()
    assert result["corrupt_count"] == 0


def test_audit_detects_qty_nonzero_with_avg_price_zero():
    """The 2026-05-08 scenario: qty=7500 avg_price=0.0."""
    rt = _make_runtime_with_positions(
        {
            "A1": [
                _pos("NATURALGAS22MAY26265CE", 7500, 0.0),
            ]
        }
    )
    result = rt.audit_position_avg_price_corruption()
    assert result["corrupt_count"] == 1
    sample = result["samples"][0]
    assert sample["account_id"] == "A1"
    assert sample["symbol"] == "NATURALGAS22MAY26265CE"
    assert sample["quantity"] == 7500
    assert sample["avg_price"] == 0.0


def test_audit_detects_qty_nonzero_with_avg_price_negative():
    """avg_price < 0 is also corruption."""
    rt = _make_runtime_with_positions(
        {"A1": [_pos("BAD_SYM", 1000, -5.0)]}
    )
    result = rt.audit_position_avg_price_corruption()
    assert result["corrupt_count"] == 1


def test_audit_caps_samples_at_5(caplog):
    """Many corrupt records — samples capped to keep payload small."""
    positions = [_pos(f"SYM_{i}", 100, 0.0) for i in range(10)]
    rt = _make_runtime_with_positions({"A1": positions})
    result = rt.audit_position_avg_price_corruption()
    assert result["corrupt_count"] == 10
    assert len(result["samples"]) == 5


def test_audit_emits_error_event_for_each_unique_corruption(caplog):
    """Issue #226: each unique (account, symbol) corruption emits an
    ERROR-level POSITION_AVG_PRICE_CORRUPTION event."""
    caplog.set_level(logging.ERROR)
    rt = _make_runtime_with_positions(
        {
            "A1": [
                _pos("NG22MAY26265CE", 1250, 0.0),
                _pos("NIFTY24DEC25000CE", 50, 0.0),
            ]
        }
    )
    rt.audit_position_avg_price_corruption()
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    corruption_events = [m for m in msgs if "POSITION_AVG_PRICE_CORRUPTION" in m]
    assert len(corruption_events) == 2, (
        f"expected 2 ERROR events (one per scope), got {len(corruption_events)}"
    )


def test_audit_rate_limits_alert_per_scope_within_60s(caplog):
    """Repeated audit cycles within 60s for the same (account, symbol)
    must emit only one ERROR — log volume control."""
    caplog.set_level(logging.ERROR)
    rt = _make_runtime_with_positions(
        {"A1": [_pos("NG_SYM", 1000, 0.0)]}
    )
    rt.audit_position_avg_price_corruption()
    rt.audit_position_avg_price_corruption()
    rt.audit_position_avg_price_corruption()
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    corruption_events = [m for m in msgs if "POSITION_AVG_PRICE_CORRUPTION" in m]
    assert len(corruption_events) == 1, (
        f"expected exactly 1 rate-limited ERROR within 60s, got "
        f"{len(corruption_events)}"
    )


def test_audit_handles_missing_hub_or_state_store_gracefully():
    """If hub or state_store is not wired, audit returns 0 without
    raising."""
    rt = SimpleNamespace(hub=None, state_store=None)
    from app.hub.runtime import HubRuntime
    rt.audit_position_avg_price_corruption = (
        HubRuntime.audit_position_avg_price_corruption.__get__(rt, HubRuntime)
    )
    result = rt.audit_position_avg_price_corruption()
    assert result["corrupt_count"] == 0
    assert result["samples"] == []


def test_audit_iterates_multiple_accounts():
    """Corruption found on multiple accounts is all reported."""
    rt = _make_runtime_with_positions(
        {
            "A1": [_pos("SYM_A", 100, 0.0)],
            "A2": [_pos("SYM_B", 200, 0.0)],
        }
    )
    result = rt.audit_position_avg_price_corruption()
    assert result["corrupt_count"] == 2
    accounts_in_samples = {s["account_id"] for s in result["samples"]}
    assert accounts_in_samples == {"A1", "A2"}


# ---------------------------------------------------------------------------
# Codex round-1 review on PR #237: dict-style position with field aliases.
# ---------------------------------------------------------------------------


def test_audit_detects_corruption_in_dict_position_with_qty_alias():
    """Codex P2 round 1: dict-style positions with ``qty`` alias instead
    of ``quantity`` (and ``avgPrice``/``entry_price`` aliases) must be
    audited. Some adapters and test harnesses return dicts — the same
    shapes the dashboard normalizer accepts."""
    rt = _make_runtime_with_positions(
        {
            "A1": [
                {"symbol": "DICT_SYM_1", "qty": 1000, "avgPrice": 0.0},
                {"symbol": "DICT_SYM_2", "net_qty": 500, "entry_price": 0.0},
            ]
        }
    )
    result = rt.audit_position_avg_price_corruption()
    assert result["corrupt_count"] == 2, (
        "dict/alias positions must be audited (#237 review P2)"
    )
    symbols = {s["symbol"] for s in result["samples"]}
    assert symbols == {"DICT_SYM_1", "DICT_SYM_2"}


def test_audit_detects_corruption_in_dict_position_with_average_price_alias():
    """``average_price`` (with underscore) is an Angel-API alias that
    must also be normalized."""
    rt = _make_runtime_with_positions(
        {
            "A1": [
                {"symbol": "DICT_SYM", "quantity": 250, "average_price": 0.0},
            ]
        }
    )
    result = rt.audit_position_avg_price_corruption()
    assert result["corrupt_count"] == 1


def test_audit_dict_position_with_healthy_avg_price_is_not_flagged():
    """Sanity: dict positions with positive avg_price are NOT corrupt."""
    rt = _make_runtime_with_positions(
        {
            "A1": [
                {"symbol": "DICT_OK", "qty": 1000, "avgPrice": 100.5},
            ]
        }
    )
    result = rt.audit_position_avg_price_corruption()
    assert result["corrupt_count"] == 0
