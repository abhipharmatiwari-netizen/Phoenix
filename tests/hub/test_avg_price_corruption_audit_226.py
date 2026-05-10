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


# ---------------------------------------------------------------------------
# Codex round-2 review on PR #237: alias + decimal-coercion + read-failure.
# ---------------------------------------------------------------------------


def test_audit_normalizes_avgprice_lowercase_alias():
    """avgprice (Angel-style lowercase no-camelCase) must be recognized."""
    rt = _make_runtime_with_positions(
        {"A1": [{"symbol": "X", "qty": 100, "avgprice": 0.0}]}
    )
    assert rt.audit_position_avg_price_corruption()["corrupt_count"] == 1


def test_audit_normalizes_averageprice_lowercase_alias():
    """averageprice (lowercase variant) must be recognized."""
    rt = _make_runtime_with_positions(
        {"A1": [{"symbol": "X", "qty": 100, "averageprice": 0.0}]}
    )
    assert rt.audit_position_avg_price_corruption()["corrupt_count"] == 1


def test_audit_normalizes_netqty_alias():
    """netqty (Angel) must be recognized as quantity."""
    rt = _make_runtime_with_positions(
        {"A1": [{"symbol": "X", "netqty": 100, "avgPrice": 0.0}]}
    )
    assert rt.audit_position_avg_price_corruption()["corrupt_count"] == 1


def test_audit_normalizes_tradingsymbol_alias():
    """tradingsymbol (Angel) must be normalized as symbol — otherwise
    rate-limit alerts collide on empty symbol."""
    rt = _make_runtime_with_positions(
        {
            "A1": [
                {"tradingsymbol": "ANGEL_FORMAT_SYM", "qty": 100, "avgPrice": 0.0},
            ]
        }
    )
    result = rt.audit_position_avg_price_corruption()
    assert result["corrupt_count"] == 1
    assert result["samples"][0]["symbol"] == "ANGEL_FORMAT_SYM"


def test_audit_coerces_decimal_string_quantity_to_int():
    """Codex P2: qty='65.0' must be coerced via int(float()) instead
    of failing to parse and being skipped as zero."""
    rt = _make_runtime_with_positions(
        {"A1": [{"symbol": "X", "quantity": "65.0", "avg_price": 0.0}]}
    )
    assert rt.audit_position_avg_price_corruption()["corrupt_count"] == 1


def test_audit_does_not_flag_healthy_dict_with_avgprice_alias():
    """Sanity: a healthy position with the Angel ``avgprice`` alias and
    a positive value must NOT be flagged. (Earlier rounds missed this
    alias and incorrectly flagged positive avgprice as zero.)"""
    rt = _make_runtime_with_positions(
        {"A1": [{"symbol": "X", "qty": 100, "avgprice": 250.5}]}
    )
    assert rt.audit_position_avg_price_corruption()["corrupt_count"] == 0


def test_audit_surfaces_per_account_read_failures():
    """Codex P2: when get_positions raises for an account, the audit
    must report it via ``read_failures`` rather than silently
    continuing with corrupt_count=0."""
    failing_state_store = SimpleNamespace(
        get_positions=lambda acct: (_ for _ in ()).throw(
            RuntimeError("postgres unreachable")
        ),
    )
    from app.hub.runtime import HubRuntime
    rt = SimpleNamespace(
        hub=SimpleNamespace(list_runner_ids=lambda: ["A1"]),
        state_store=failing_state_store,
    )
    rt.audit_position_avg_price_corruption = (
        HubRuntime.audit_position_avg_price_corruption.__get__(rt, HubRuntime)
    )
    rt._maybe_emit_avg_price_corruption_alert = (
        HubRuntime._maybe_emit_avg_price_corruption_alert.__get__(rt, HubRuntime)
    )
    result = rt.audit_position_avg_price_corruption()
    assert result["corrupt_count"] == 0
    assert len(result["read_failures"]) == 1
    assert result["read_failures"][0]["account_id"] == "A1"
    assert "postgres unreachable" in result["read_failures"][0]["error"]


# ---------------------------------------------------------------------------
# Codex round-2 review on PR #237: net + netavgprice fallbacks.
# ---------------------------------------------------------------------------


def test_audit_normalizes_net_quantity_alias():
    """Codex P2 round 2: ``net`` alias for quantity (Angel falls back
    from ``netqty`` to ``net``)."""
    rt = _make_runtime_with_positions(
        {"A1": [{"tradingsymbol": "X", "net": 65, "avgprice": 0}]}
    )
    assert rt.audit_position_avg_price_corruption()["corrupt_count"] == 1


def test_audit_uses_netavgprice_when_avgprice_is_zero():
    """Codex P2 round 2: a healthy open row with ``avgprice=0`` but
    ``netavgprice=100`` must NOT be flagged — the audit must fall
    back to ``netavgprice`` like angel_client and position_sync do."""
    rt = _make_runtime_with_positions(
        {
            "A1": [
                {
                    "tradingsymbol": "X",
                    "netqty": 65,
                    "avgprice": 0,
                    "netavgprice": 100.5,
                }
            ]
        }
    )
    assert rt.audit_position_avg_price_corruption()["corrupt_count"] == 0


def test_audit_uses_netprice_when_other_fields_zero():
    """``netprice`` is also a valid fallback."""
    rt = _make_runtime_with_positions(
        {
            "A1": [
                {"qty": 100, "avgprice": 0, "netprice": 50.0, "symbol": "X"}
            ]
        }
    )
    assert rt.audit_position_avg_price_corruption()["corrupt_count"] == 0


def test_audit_uses_buyavgprice_for_long_positions():
    """Side-specific ``buyavgprice`` for long positions."""
    rt = _make_runtime_with_positions(
        {
            "A1": [
                {"qty": 100, "avgprice": 0, "buyavgprice": 75.0, "symbol": "X"}
            ]
        }
    )
    assert rt.audit_position_avg_price_corruption()["corrupt_count"] == 0


def test_audit_flags_negative_primary_avg_price_even_with_positive_fallback():
    """Round-3 P2: a row with ``avg_price=-5`` must be flagged as
    corrupt regardless of any positive fallback price — broker would
    never legitimately send a negative average price for an open
    position, so fallback-substitution must NOT hide it."""
    rt = _make_runtime_with_positions(
        {
            "A1": [
                {
                    "qty": 65,
                    "avg_price": -5.0,
                    "netavgprice": 100.0,
                    "symbol": "X",
                }
            ]
        }
    )
    result = rt.audit_position_avg_price_corruption()
    assert result["corrupt_count"] == 1
    assert result["samples"][0]["avg_price"] == -5.0


def test_audit_keeps_scanning_primary_aliases_after_zero():
    """Round-3 P2: a mixed row like ``{'avg_price': 0, 'avgPrice': 100}``
    must stay healthy — the audit must not stop at the first present
    zero alias and treat the row as corrupt; positive primary alias
    proves the row is healthy even when an earlier alias is zero."""
    rt = _make_runtime_with_positions(
        {
            "A1": [
                {
                    "qty": 65,
                    "avg_price": 0,
                    "avgPrice": 100.0,
                    "symbol": "X",
                }
            ]
        }
    )
    assert rt.audit_position_avg_price_corruption()["corrupt_count"] == 0


def test_audit_uses_camelcase_broker_fallbacks():
    """Round-3 P2: camelCase broker fallback fields ``netAvgPrice``,
    ``netPrice``, ``buyAvgPrice``, ``sellAvgPrice`` must be honoured
    (the angel_client and position_sync paths accept these casings)."""
    rt = _make_runtime_with_positions(
        {
            "A1": [
                {
                    "qty": 65,
                    "avgprice": 0,
                    "netAvgPrice": 100.0,
                    "symbol": "X",
                }
            ]
        }
    )
    assert rt.audit_position_avg_price_corruption()["corrupt_count"] == 0


def test_audit_does_not_use_buyavgprice_for_short_positions():
    """Round-3 P2: side-specific fallbacks. A short row (qty<0) with
    ``avg_price=0`` and stale ``buyavgprice=80`` (from prior intraday
    long activity) must NOT be treated as healthy — only sell-side
    fallbacks apply for shorts."""
    rt = _make_runtime_with_positions(
        {
            "A1": [
                {
                    "qty": -50,
                    "avgprice": 0,
                    "buyavgprice": 80.0,
                    "sellavgprice": 0,
                    "symbol": "X",
                }
            ]
        }
    )
    result = rt.audit_position_avg_price_corruption()
    assert result["corrupt_count"] == 1


def test_audit_does_not_use_sellavgprice_for_long_positions():
    """Round-3 P2: side-specific fallbacks. A long row (qty>0) with
    ``avg_price=0``, stale ``sellavgprice=80`` (from prior closed
    sell activity), and zero buy-side averages must be flagged as
    corrupt — only buy-side fallbacks apply for longs."""
    rt = _make_runtime_with_positions(
        {
            "A1": [
                {
                    "qty": 50,
                    "avgprice": 0,
                    "buyavgprice": 0,
                    "sellavgprice": 80.0,
                    "symbol": "X",
                }
            ]
        }
    )
    result = rt.audit_position_avg_price_corruption()
    assert result["corrupt_count"] == 1
