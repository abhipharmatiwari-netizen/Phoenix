from __future__ import annotations

from datetime import datetime, timezone

from app.strategies.adaptive.dynamic_policy import (
    DynamicPolicyEngine,
    PositionPolicyState,
    parse_dynamic_policy_config,
)
from app.strategies.adaptive.market_context import MarketContext
from app.strategies.adaptive.regime import Regime


def _ctx() -> MarketContext:
    return MarketContext(
        ts=datetime(2025, 1, 1, 9, 30, tzinfo=timezone.utc),
        last_price=100.0,
        atr14=1.0,
        atr_norm=1.0,
        adx14=25.0,
        di_spread=10.0,
        ema_slope=0.01,
        gap_ratio=0.0,
    )


def test_policy_overlay_applies_profile_overrides():
    base = {"sl_pct": 0.3, "tp_pct": 0.3, "use_adx_filter": False}
    cfg = parse_dynamic_policy_config(
        {
            "enabled": True,
            "policy_id": "ema20_ng_v1",
            "profiles": {
                "TRENDING": {
                    "sl_pct": 0.45,
                    "tp_pct": 0.60,
                    "use_adx_filter": True,
                }
            },
        }
    )
    engine = DynamicPolicyEngine(config=cfg)
    effective = engine.apply(
        base_params=base,
        regime=Regime.TRENDING,
        context=_ctx(),
    )
    assert effective["sl_pct"] == 0.45
    assert effective["tp_pct"] == 0.60
    assert effective["use_adx_filter"] is True
    assert base == {"sl_pct": 0.3, "tp_pct": 0.3, "use_adx_filter": False}


def test_directional_trending_profiles_fallback_to_legacy_trending_profile():
    base = {"sl_pct": 0.3, "tp_pct": 0.3}
    cfg = parse_dynamic_policy_config(
        {
            "enabled": True,
            "policy_id": "ema20_ng_v1",
            "profiles": {
                "TRENDING": {
                    "sl_pct": 0.45,
                    "tp_pct": 0.60,
                }
            },
        }
    )
    engine = DynamicPolicyEngine(config=cfg)

    effective = engine.apply(
        base_params=base,
        regime=Regime.TRENDING_DOWN,
        context=_ctx(),
    )

    assert effective["sl_pct"] == 0.45
    assert effective["tp_pct"] == 0.60


def test_directional_trending_profile_overrides_legacy_profile_when_present():
    base = {"sl_pct": 0.3, "tp_pct": 0.3}
    cfg = parse_dynamic_policy_config(
        {
            "enabled": True,
            "policy_id": "ema20_ng_v1",
            "profiles": {
                "TRENDING": {"sl_pct": 0.45},
                "TRENDING_UP": {"sl_pct": 0.25, "tp_pct": 0.50},
            },
        }
    )
    engine = DynamicPolicyEngine(config=cfg)

    effective = engine.apply(
        base_params=base,
        regime=Regime.TRENDING_UP,
        context=_ctx(),
    )

    assert effective["sl_pct"] == 0.25
    assert effective["tp_pct"] == 0.50


def test_no_trade_profile_disables_entries():
    base = {"disable_entries": False}
    cfg = parse_dynamic_policy_config(
        {
            "enabled": True,
            "policy_id": "ema20_ng_v1",
        }
    )
    engine = DynamicPolicyEngine(config=cfg)
    effective = engine.apply(
        base_params=base,
        regime=Regime.NO_TRADE,
        context=_ctx(),
    )
    assert effective["disable_entries"] is True


def test_freeze_entry_critical_params_when_position_open():
    base = {
        "sl_pct": 0.3,
        "tp_pct": 0.3,
        "trail_buffer_pct": 0.10,
        "trail_trigger_pct": 0.20,
        "use_adx_filter": False,
    }
    cfg = parse_dynamic_policy_config(
        {
            "enabled": True,
            "policy_id": "ema20_ng_v1",
            "freeze_on_entry": True,
            "profiles": {
                "TRENDING": {
                    "sl_pct": 0.5,
                    "tp_pct": 0.8,
                    "trail_buffer_pct": 0.2,
                    "trail_trigger_pct": 0.4,
                }
            },
        }
    )
    engine = DynamicPolicyEngine(config=cfg)
    effective = engine.apply(
        base_params=base,
        regime=Regime.TRENDING,
        context=_ctx(),
        state=PositionPolicyState(
            position_open=True,
            entry_frozen_params={
                "sl_pct": 0.3,
                "tp_pct": 0.3,
            },
        ),
    )
    assert effective["sl_pct"] == 0.3
    assert effective["tp_pct"] == 0.3
    # trailing values remain runtime updatable by default
    assert effective["trail_buffer_pct"] == 0.2
    assert effective["trail_trigger_pct"] == 0.4


def test_unknown_override_keys_are_ignored_and_counted():
    base = {"sl_pct": 0.3}
    cfg = parse_dynamic_policy_config(
        {
            "enabled": True,
            "policy_id": "ema20_ng_v1",
            "profiles": {
                "TRENDING": {
                    "made_up_key": 123,
                    "sl_pct": 0.4,
                }
            },
        }
    )
    engine = DynamicPolicyEngine(config=cfg, warning_interval_seconds=0.0)
    effective = engine.apply(
        base_params=base,
        regime=Regime.TRENDING,
        context=_ctx(),
    )
    assert "made_up_key" not in effective
    assert engine.unknown_key_count("made_up_key") == 1
    assert effective["sl_pct"] == 0.4


def test_out_of_range_values_are_clamped_and_counted():
    base = {
        "sl_pct": 0.2,
        "tp_pct": 0.2,
        "trail_buffer_pct": 0.1,
        "ema_period": 20,
        "min_adx": 20.0,
        "qty_mult": 1.0,
    }
    cfg = parse_dynamic_policy_config(
        {
            "enabled": True,
            "policy_id": "ema20_ng_v1",
            "profiles": {
                "TRENDING": {
                    "sl_pct": 5.0,
                    "tp_pct": -1.0,
                    "trail_buffer_pct": 5.0,
                    "ema_period": 500,
                    "min_adx": 500.0,
                    "qty_mult": 10.0,
                }
            },
        }
    )
    engine = DynamicPolicyEngine(config=cfg)
    effective = engine.apply(
        base_params=base,
        regime=Regime.TRENDING,
        context=_ctx(),
    )
    assert effective["sl_pct"] == 0.9
    assert effective["tp_pct"] == 0.05
    assert effective["trail_buffer_pct"] == 1.0
    assert effective["ema_period"] == 200
    assert effective["min_adx"] == 100.0
    assert 0.25 <= effective["qty_mult"] <= 1.5
    assert engine.clamp_count("sl_pct") >= 1
    assert engine.clamp_count("tp_pct") >= 1
    assert engine.clamp_count("trail_buffer_pct") >= 1
    assert engine.clamp_count("ema_period") >= 1
    assert engine.clamp_count("min_adx") >= 1
