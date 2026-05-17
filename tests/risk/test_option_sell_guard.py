from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.data.option_chain_provider import OptionQuote
from app.risk.kill_switch import KillSwitchManager, KillSwitchScope
from app.risk.option_sell_guard import (
    OptionSellGuardConfig,
    OptionSellGuardDecision,
    OptionSellGuardContext,
    OptionSellStructure,
    evaluate_option_sell_guard,
)
from app.strategies.identifiers import OI_ML_CE_SELLER_ID


IST = timezone(timedelta(hours=5, minutes=30))


def _now(hour: int = 10, minute: int = 0) -> datetime:
    return datetime(2026, 5, 19, hour, minute, tzinfo=IST)


def _quote(**overrides) -> OptionQuote:
    base = {
        "snapshot_ts": _now(),
        "source_ts": _now(9, 59),
        "underlying": "NIFTY",
        "expiry": date(2026, 5, 21),
        "strike": 25200,
        "option_type": "CE",
        "trading_symbol": "NIFTY21MAY2625200CE",
        "exchange": "NFO",
        "provider": "angel",
        "symbol_token": "12345",
        "oi": 120000,
        "volume": 1500,
        "iv": "11.25",
        "bid": "42.5",
        "ask": "43.0",
        "ltp": "42.8",
    }
    base.update(overrides)
    return OptionQuote(**base)


def _context(**overrides) -> OptionSellGuardContext:
    base = {
        "now": _now(),
        "structure": OptionSellStructure.BEAR_CALL_SPREAD,
        "quote": _quote(),
        "ml_score": 0.64,
        "predicted_mae_premium": 45.0,
        "premium_received": 50.0,
        "max_loss_rupees": 4200.0,
        "vix": 16.5,
        "tenant_id": "tenant-a",
        "account_id": "acct-a",
    }
    base.update(overrides)
    return OptionSellGuardContext(**base)


def test_non_target_strategy_is_not_guarded():
    result = evaluate_option_sell_guard(_context(strategy_id="other_strategy"))

    assert result.allowed is True
    assert result.reasons == ("not_guarded_strategy",)


def test_exit_order_bypasses_entry_guard():
    result = evaluate_option_sell_guard(
        _context(
            is_exit=True,
            now=_now(15, 25),
            quote=None,
            ml_score=None,
            vix=None,
        )
    )

    assert result.allowed is True
    assert result.reasons == ("exit_order_bypass",)


def test_valid_spread_entry_is_allowed():
    result = evaluate_option_sell_guard(_context())

    assert result.allowed is True
    assert result.decision == OptionSellGuardDecision.ALLOW


def test_outside_entry_window_rejects_new_entries():
    result = evaluate_option_sell_guard(_context(now=_now(14, 30)))

    assert result.allowed is False
    assert "outside_entry_window:after_end" in result.reasons


def test_missing_quote_rejects_fail_closed():
    result = evaluate_option_sell_guard(_context(quote=None))

    assert result.allowed is False
    assert "missing_option_quote" in result.reasons


def test_quote_quality_flags_reject_live_entries():
    result = evaluate_option_sell_guard(_context(quote=_quote(symbol_token=None)))

    assert result.allowed is False
    assert "quote_quality:missing_symbol_token" in result.reasons


def test_stale_snapshot_rejects_even_when_source_timestamp_is_clean():
    stale_quote = _quote(
        snapshot_ts=_now(9, 57),
        source_ts=_now(9, 56),
    )

    result = evaluate_option_sell_guard(_context(quote=stale_quote))

    assert result.allowed is False
    assert "stale_option_quote" in result.reasons
    assert result.metadata["quote_age_seconds"] == 180


def test_vix_above_entry_max_rejects():
    result = evaluate_option_sell_guard(_context(vix=23.0))

    assert result.allowed is False
    assert "vix_above_entry_max" in result.reasons


def test_predicted_mae_above_premium_multiple_rejects():
    result = evaluate_option_sell_guard(
        _context(predicted_mae_premium=61.0, premium_received=50.0)
    )

    assert result.allowed is False
    assert "predicted_mae_above_limit" in result.reasons


def test_spread_loss_above_limit_rejects():
    result = evaluate_option_sell_guard(_context(max_loss_rupees=6000.0))

    assert result.allowed is False
    assert "spread_loss_above_limit" in result.reasons


def test_kill_switch_tripped_rejects_entries():
    manager = KillSwitchManager(audit_fn=lambda **_: None)
    manager.trip(
        KillSwitchScope.STRATEGY,
        OI_ML_CE_SELLER_ID,
        reason="daily_loss",
        actor="test",
    )

    result = evaluate_option_sell_guard(_context(kill_switch_manager=manager))

    assert result.allowed is False
    assert "kill_switch_tripped" in result.reasons


def test_required_kill_switch_manager_rejects_when_missing():
    result = evaluate_option_sell_guard(
        _context(kill_switch_manager=None),
        OptionSellGuardConfig(require_kill_switch_manager=True),
    )

    assert result.allowed is False
    assert "missing_kill_switch_manager" in result.reasons


def test_naked_entry_requires_spread_when_naked_is_disabled():
    result = evaluate_option_sell_guard(
        _context(structure=OptionSellStructure.NAKED_SHORT_CE),
        OptionSellGuardConfig(allow_naked=False),
    )

    assert result.allowed is False
    assert result.decision == OptionSellGuardDecision.REQUIRE_SPREAD
    assert result.required_structure == OptionSellStructure.BEAR_CALL_SPREAD
    assert "naked_disabled" in result.reasons


def test_naked_entry_requires_spread_when_vix_or_score_are_not_tight_enough():
    result = evaluate_option_sell_guard(
        _context(
            structure=OptionSellStructure.NAKED_SHORT_CE,
            ml_score=0.62,
            vix=19.0,
        ),
        OptionSellGuardConfig(allow_naked=True),
    )

    assert result.allowed is False
    assert result.decision == OptionSellGuardDecision.REQUIRE_SPREAD
    assert "naked_ml_score_below_min" in result.reasons
    assert "naked_vix_above_max" in result.reasons


def test_naked_entry_is_allowed_only_under_tight_gates():
    result = evaluate_option_sell_guard(
        _context(
            structure=OptionSellStructure.NAKED_SHORT_CE,
            ml_score=0.72,
            vix=17.0,
            max_loss_rupees=5000.0,
        ),
        OptionSellGuardConfig(allow_naked=True),
    )

    assert result.allowed is True


def test_naked_entry_with_hard_failure_does_not_get_spread_overlay_decision():
    result = evaluate_option_sell_guard(
        _context(
            structure=OptionSellStructure.NAKED_SHORT_CE,
            quote=None,
        ),
        OptionSellGuardConfig(allow_naked=False),
    )

    assert result.allowed is False
    assert result.decision == OptionSellGuardDecision.REJECT
    assert result.required_structure is None
    assert "missing_option_quote" in result.reasons
    assert "naked_disabled" in result.reasons
