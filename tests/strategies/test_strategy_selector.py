from datetime import datetime, timedelta, timezone

from app.strategies.adaptive.regime import Regime
from app.strategies.adaptive.strategy_selector import (
    StrategySelector,
    StrategySelectorConfig,
)


def _ts(seconds: int) -> datetime:
    base = datetime(2026, 3, 5, 9, 15, tzinfo=timezone.utc)
    return base + timedelta(seconds=seconds)


def test_strategy_selector_uses_mapping_and_max_active_limit():
    cfg = StrategySelectorConfig.from_raw(
        {
            "enabled": True,
            "max_active_per_underlying": 1,
            "min_hold_seconds": 0,
            "mapping": {
                "NIFTY": {
                    "TRENDING": ["ema20", "exclusive_nifty_ce_buy"],
                }
            },
        }
    )
    selector = StrategySelector(cfg)

    decision = selector.select(
        underlying="NIFTY_IDX",
        regime=Regime.TRENDING,
        allowed_strategies=["ema20_strategy", "exclusive_nifty_ce_buy"],
        now=_ts(0),
    )

    assert decision.selected_strategies == ("ema20_strategy",)
    assert decision.reason == "mapping"
    assert selector.is_strategy_active(
        underlying="NIFTY_IDX",
        strategy_name="ema20_strategy",
    )


def test_strategy_selector_honors_hold_window_before_switching():
    cfg = StrategySelectorConfig.from_raw(
        {
            "enabled": True,
            "max_active_per_underlying": 1,
            "min_hold_seconds": 300,
            "mapping": {
                "NIFTY_IDX": {
                    "TRENDING": ["ema20_strategy"],
                    "CHOPPY": ["delta_strangle"],
                }
            },
        }
    )
    selector = StrategySelector(cfg)

    first = selector.select(
        underlying="NIFTY_IDX",
        regime=Regime.TRENDING,
        allowed_strategies=["ema20_strategy", "delta_strangle"],
        now=_ts(0),
    )
    held = selector.select(
        underlying="NIFTY_IDX",
        regime=Regime.CHOPPY,
        allowed_strategies=["ema20_strategy", "delta_strangle"],
        now=_ts(60),
    )
    switched = selector.select(
        underlying="NIFTY_IDX",
        regime=Regime.CHOPPY,
        allowed_strategies=["ema20_strategy", "delta_strangle"],
        now=_ts(360),
    )

    assert first.selected_strategies == ("ema20_strategy",)
    assert held.selected_strategies == ("ema20_strategy",)
    assert held.changed is False
    assert held.reason.startswith("hold_active(")
    assert switched.selected_strategies == ("delta_strangle",)
    assert switched.changed is True


def test_no_trade_explicit_mapping_is_honoured():
    """
    Strategies listed explicitly in a NO_TRADE mapping are selected and receive bar
    dispatch — their own dynamic policy gates entries.  This is the production scenario
    for exclusive_nifty_ce_buy on NIFTY_IDX.
    """
    cfg = StrategySelectorConfig.from_raw(
        {
            "enabled": True,
            "max_active_per_underlying": 2,
            "min_hold_seconds": 0,
            "mapping": {
                "NIFTY_IDX": {
                    "NORMAL": ["ema20_strategy", "exclusive_nifty_ce_buy"],
                    "NO_TRADE": ["exclusive_nifty_ce_buy"],
                }
            },
        }
    )
    selector = StrategySelector(cfg)

    decision = selector.select(
        underlying="NIFTY_IDX",
        regime=Regime.NO_TRADE,
        allowed_strategies=["ema20_strategy", "exclusive_nifty_ce_buy"],
        now=_ts(0),
    )

    assert decision.selected_strategies == ("exclusive_nifty_ce_buy",)
    assert decision.reason == "mapping"
    assert selector.is_strategy_active(
        underlying="NIFTY_IDX", strategy_name="exclusive_nifty_ce_buy"
    )
    assert not selector.is_strategy_active(
        underlying="NIFTY_IDX", strategy_name="ema20_strategy"
    )


def test_no_trade_empty_mapping_still_clears_selection():
    """An explicit but empty NO_TRADE mapping continues to clear the selection."""
    cfg = StrategySelectorConfig.from_raw(
        {
            "enabled": True,
            "max_active_per_underlying": 1,
            "min_hold_seconds": 0,
            "mapping": {
                "NIFTY_IDX": {
                    "NORMAL": ["ema20_strategy"],
                    "NO_TRADE": [],
                }
            },
        }
    )
    selector = StrategySelector(cfg)

    selector.select(
        underlying="NIFTY_IDX",
        regime=Regime.NORMAL,
        allowed_strategies=["ema20_strategy"],
        now=_ts(0),
    )
    no_trade = selector.select(
        underlying="NIFTY_IDX",
        regime=Regime.NO_TRADE,
        allowed_strategies=["ema20_strategy"],
        now=_ts(60),
    )

    assert no_trade.selected_strategies == ()
    assert no_trade.reason == "no_trade_regime"
    assert not selector.is_strategy_active(
        underlying="NIFTY_IDX", strategy_name="ema20_strategy"
    )


def test_strategy_selector_clears_selection_on_no_trade_regime():
    cfg = StrategySelectorConfig.from_raw(
        {
            "enabled": True,
            "max_active_per_underlying": 1,
            "min_hold_seconds": 300,
            "mapping": {
                "BANKNIFTY_IDX": {
                    "TRENDING": ["ema20_strategy"],
                }
            },
        }
    )
    selector = StrategySelector(cfg)

    selector.select(
        underlying="BANKNIFTY_IDX",
        regime=Regime.TRENDING,
        allowed_strategies=["ema20_strategy"],
        now=_ts(0),
    )
    no_trade = selector.select(
        underlying="BANKNIFTY_IDX",
        regime=Regime.NO_TRADE,
        allowed_strategies=["ema20_strategy"],
        now=_ts(60),
    )

    assert no_trade.selected_strategies == ()
    assert no_trade.reason == "no_trade_regime"


# ---------------------------------------------------------------------------
# EMA20-5: ema20_is_authoritative field — unit tests
# Incident date: March 13, 2026 (NIFTY/BANKNIFTY EMA20 suppressed by selector)
# ---------------------------------------------------------------------------


def test_selector_config_ema20_is_authoritative_default_false():
    """ema20_is_authoritative defaults to False for backward compatibility."""
    cfg = StrategySelectorConfig.from_raw({"enabled": True})
    assert cfg.ema20_is_authoritative is False


def test_selector_config_ema20_is_authoritative_parsed_true():
    """ema20_is_authoritative=true is correctly parsed from raw config."""
    cfg = StrategySelectorConfig.from_raw(
        {"enabled": True, "ema20_is_authoritative": True}
    )
    assert cfg.ema20_is_authoritative is True


def test_selector_config_ema20_is_authoritative_parsed_false_explicit():
    """ema20_is_authoritative=false keeps the field False when explicit."""
    cfg = StrategySelectorConfig.from_raw(
        {"enabled": True, "ema20_is_authoritative": False}
    )
    assert cfg.ema20_is_authoritative is False


def test_selector_no_trade_still_suppresses_non_ema20():
    """Other strategies remain suppressed by NO_TRADE when ema20_is_authoritative=True."""
    cfg = StrategySelectorConfig.from_raw(
        {
            "enabled": True,
            "max_active_per_underlying": 1,
            "min_hold_seconds": 0,
            "ema20_is_authoritative": True,
            "mapping": {
                "NIFTY_IDX": {
                    "TRENDING": ["put_momentum_scalper"],
                    "NO_TRADE": [],
                }
            },
        }
    )
    selector = StrategySelector(cfg)
    selector.select(
        underlying="NIFTY_IDX",
        regime=Regime.TRENDING,
        allowed_strategies=["put_momentum_scalper"],
        now=_ts(0),
    )
    no_trade = selector.select(
        underlying="NIFTY_IDX",
        regime=Regime.NO_TRADE,
        allowed_strategies=["put_momentum_scalper"],
        now=_ts(60),
    )
    # Selector correctly returns empty for non-EMA20 strategies in NO_TRADE
    assert no_trade.selected_strategies == ()
    assert not selector.is_strategy_active(
        underlying="NIFTY_IDX", strategy_name="put_momentum_scalper"
    )


def test_selector_no_trade_returns_ema20_inactive_at_selector_level():
    """Selector itself still marks EMA20 inactive in NO_TRADE — bypass happens upstream."""
    cfg = StrategySelectorConfig.from_raw(
        {
            "enabled": True,
            "max_active_per_underlying": 1,
            "min_hold_seconds": 0,
            "ema20_is_authoritative": True,
            "mapping": {
                "NIFTY_IDX": {
                    "TRENDING": ["ema20_strategy"],
                    "NO_TRADE": [],
                }
            },
        }
    )
    selector = StrategySelector(cfg)
    selector.select(
        underlying="NIFTY_IDX",
        regime=Regime.TRENDING,
        allowed_strategies=["ema20_strategy"],
        now=_ts(0),
    )
    selector.select(
        underlying="NIFTY_IDX",
        regime=Regime.NO_TRADE,
        allowed_strategies=["ema20_strategy"],
        now=_ts(60),
    )
    # The selector itself sees EMA20 as inactive; bypass is applied by _is_strategy_selected
    # in multi_instrument_stream, not inside StrategySelector.
    assert not selector.is_strategy_active(
        underlying="NIFTY_IDX", strategy_name="ema20_strategy"
    )


# ---------------------------------------------------------------------------
# EMA20-5: incident replay simulation
# Simulates the March 13, 2026 NO_TRADE regime that suppressed NIFTY EMA20.
# Verifies that _is_strategy_selected bypass logic (tested via config flag) is
# correctly wired: ema20_is_authoritative=True means the caller should allow EMA20.
# ---------------------------------------------------------------------------


def _simulate_is_strategy_selected(
    selector: StrategySelector,
    cfg: StrategySelectorConfig,
    underlying_label: str,
    strategy_name: str,
) -> bool:
    """Mirrors the _is_strategy_selected logic from multi_instrument_stream.py."""
    from app.strategies.naming import canonicalize_strategy_name

    if cfg.ema20_is_authoritative and canonicalize_strategy_name(
        strategy_name, source="test_bypass"
    ) == "ema20_strategy":
        return True
    return selector.is_strategy_active(
        underlying=underlying_label.upper(),
        strategy_name=strategy_name,
    )


def test_incident_replay_nifty_no_trade_ema20_authoritative_bypasses_selector():
    """
    Incident replay: NIFTY_IDX in NO_TRADE regime on March 13, 2026.

    EMA20's NO_TRADE profile has disable_entries=false, meaning EMA20 itself
    permits entries. With ema20_is_authoritative=True, the selector NO_TRADE->[]
    mapping must NOT suppress EMA20.
    """
    cfg = StrategySelectorConfig.from_raw(
        {
            "enabled": True,
            "max_active_per_underlying": 2,
            "min_hold_seconds": 180,
            "ema20_is_authoritative": True,
            "mapping": {
                "NIFTY_IDX": {
                    "TRENDING": ["put_momentum_scalper", "exclusive_nifty_ce_buy"],
                    "NORMAL": ["ema20_strategy"],
                    "CHOPPY": ["ce_orb"],
                    "HIGH_VOL": ["put_momentum_scalper", "exclusive_nifty_ce_buy"],
                    "NO_TRADE": [],
                }
            },
        }
    )
    selector = StrategySelector(cfg)

    # Simulate the March 13 10:35 bar in NO_TRADE regime
    selector.select(
        underlying="NIFTY_IDX",
        regime=Regime.NO_TRADE,
        allowed_strategies=["ema20_strategy", "put_momentum_scalper", "ce_orb"],
        now=_ts(0),
    )

    # With authoritative bypass: EMA20 is allowed through
    assert _simulate_is_strategy_selected(
        selector, cfg, "NIFTY_IDX", "ema20_strategy"
    ) is True, "EMA20 must pass through when ema20_is_authoritative=True even in NO_TRADE"

    # Other strategies in NO_TRADE remain suppressed
    assert _simulate_is_strategy_selected(
        selector, cfg, "NIFTY_IDX", "put_momentum_scalper"
    ) is False, "put_momentum_scalper must remain suppressed in NO_TRADE"

    assert _simulate_is_strategy_selected(
        selector, cfg, "NIFTY_IDX", "ce_orb"
    ) is False, "ce_orb must remain suppressed in NO_TRADE"


def test_incident_replay_banknifty_no_trade_ema20_authoritative_bypasses_selector():
    """
    Incident replay: BANKNIFTY_IDX in NO_TRADE regime on March 13, 2026 (13:55 bar).

    With ema20_is_authoritative=True, EMA20 must not be suppressed by selector.
    """
    cfg = StrategySelectorConfig.from_raw(
        {
            "enabled": True,
            "max_active_per_underlying": 2,
            "min_hold_seconds": 180,
            "ema20_is_authoritative": True,
            "mapping": {
                "BANKNIFTY_IDX": {
                    "TRENDING": ["put_momentum_scalper"],
                    "NORMAL": ["ema20_strategy"],
                    "CHOPPY": ["ce_orb"],
                    "HIGH_VOL": ["put_momentum_scalper"],
                    "NO_TRADE": [],
                }
            },
        }
    )
    selector = StrategySelector(cfg)

    selector.select(
        underlying="BANKNIFTY_IDX",
        regime=Regime.NO_TRADE,
        allowed_strategies=["ema20_strategy", "put_momentum_scalper"],
        now=_ts(0),
    )

    assert _simulate_is_strategy_selected(
        selector, cfg, "BANKNIFTY_IDX", "ema20_strategy"
    ) is True, "EMA20 must pass through for BANKNIFTY when ema20_is_authoritative=True"

    assert _simulate_is_strategy_selected(
        selector, cfg, "BANKNIFTY_IDX", "put_momentum_scalper"
    ) is False


def test_incident_replay_without_authoritative_ema20_is_suppressed():
    """
    Confirms the pre-fix behavior: without ema20_is_authoritative, EMA20 was
    silently suppressed by selector NO_TRADE->[] (root cause of March 13 incident).
    """
    cfg = StrategySelectorConfig.from_raw(
        {
            "enabled": True,
            "max_active_per_underlying": 2,
            "min_hold_seconds": 180,
            "ema20_is_authoritative": False,  # old behavior
            "mapping": {
                "NIFTY_IDX": {
                    "NORMAL": ["ema20_strategy"],
                    "NO_TRADE": [],
                }
            },
        }
    )
    selector = StrategySelector(cfg)

    selector.select(
        underlying="NIFTY_IDX",
        regime=Regime.NO_TRADE,
        allowed_strategies=["ema20_strategy"],
        now=_ts(0),
    )

    # Without the fix, EMA20 was suppressed — reproduce the old (broken) behavior
    assert _simulate_is_strategy_selected(
        selector, cfg, "NIFTY_IDX", "ema20_strategy"
    ) is False, "Without ema20_is_authoritative, EMA20 should have been suppressed (pre-fix behavior)"


def test_ema20_bypass_accepts_alias_names():
    """Alias names like 'ema20' and 'ema_20_strategy' are treated identically to 'ema20_strategy'."""
    cfg = StrategySelectorConfig.from_raw(
        {
            "enabled": True,
            "ema20_is_authoritative": True,
            "mapping": {"NIFTY_IDX": {"NO_TRADE": []}},
        }
    )
    selector = StrategySelector(cfg)
    selector.select(
        underlying="NIFTY_IDX",
        regime=Regime.NO_TRADE,
        allowed_strategies=[],
        now=_ts(0),
    )

    for alias in ("ema20", "ema_20_strategy", "ema20_strategy"):
        assert _simulate_is_strategy_selected(
            selector, cfg, "NIFTY_IDX", alias
        ) is True, f"Alias '{alias}' should resolve to ema20_strategy and bypass selector"


def test_ng_fut_selector_behavior_unchanged():
    """NG_FUT selector behavior is unaffected by the EMA20 authority rule."""
    cfg = StrategySelectorConfig.from_raw(
        {
            "enabled": True,
            "max_active_per_underlying": 1,
            "min_hold_seconds": 0,
            "ema20_is_authoritative": True,
            "mapping": {
                "NG_FUT": {
                    "TRENDING": ["ema20_strategy"],
                    "NORMAL": ["ema20_strategy"],
                    "NO_TRADE": [],
                }
            },
        }
    )
    selector = StrategySelector(cfg)
    # NG was always in the NO_TRADE->[] mapping but EMA20 had a NO_TRADE: [] profile
    # too, so bypass applies consistently for NG as well.
    selector.select(
        underlying="NG_FUT",
        regime=Regime.NO_TRADE,
        allowed_strategies=["ema20_strategy"],
        now=_ts(0),
    )
    # EMA20 bypasses for NG_FUT the same way (authoritative applies globally to ema20)
    assert _simulate_is_strategy_selected(
        selector, cfg, "NG_FUT", "ema20_strategy"
    ) is True


# ---------------------------------------------------------------------------
# Issue #25: Stale selector state invalidation across IST trading days
# ---------------------------------------------------------------------------

_IST = timezone(timedelta(hours=5, minutes=30))


def _ist_ts(year: int, month: int, day: int, hour: int = 9, minute: int = 30) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=_IST).astimezone(timezone.utc)


def test_stale_state_from_prior_day_is_invalidated_on_select():
    """State with switched_at on a previous IST date is discarded on the next day's select()."""
    cfg = StrategySelectorConfig.from_raw(
        {"enabled": True, "max_active_per_underlying": 1, "min_hold_seconds": 0}
    )
    selector = StrategySelector(cfg)

    # Seed state from April 19
    selector.select(
        underlying="NIFTY",
        regime=Regime.TRENDING,
        allowed_strategies=["ema20_strategy"],
        now=_ist_ts(2026, 4, 19, 10, 0),
    )
    assert "NIFTY" in selector._state
    assert selector._state["NIFTY"].regime == Regime.TRENDING

    # April 20 call — state should be invalidated
    decision = selector.select(
        underlying="NIFTY",
        regime=Regime.CHOPPY,
        allowed_strategies=["put_momentum_scalper"],
        now=_ist_ts(2026, 4, 20, 9, 30),
    )

    # Fresh selection based on new regime
    assert decision.changed is True
    assert len(selector._stale_invalidated) == 1
    assert "NIFTY" in selector._stale_invalidated
    assert selector._state["NIFTY"].regime == Regime.CHOPPY


def test_same_day_state_is_not_invalidated():
    """State from the same IST calendar day is retained normally."""
    cfg = StrategySelectorConfig.from_raw(
        {"enabled": True, "max_active_per_underlying": 1, "min_hold_seconds": 0}
    )
    selector = StrategySelector(cfg)

    selector.select(
        underlying="NIFTY",
        regime=Regime.TRENDING,
        allowed_strategies=["ema20_strategy"],
        now=_ist_ts(2026, 4, 20, 9, 30),
    )

    # Later call on same day — state preserved, not invalidated
    selector.select(
        underlying="NIFTY",
        regime=Regime.CHOPPY,
        allowed_strategies=["put_momentum_scalper"],
        now=_ist_ts(2026, 4, 20, 11, 0),
    )

    assert len(selector._stale_invalidated) == 0


def test_staleness_summary_reflects_stale_entries_before_select():
    """staleness_summary() reports stale entries that haven't been evicted yet."""
    cfg = StrategySelectorConfig.from_raw(
        {"enabled": True, "max_active_per_underlying": 1, "min_hold_seconds": 0}
    )
    selector = StrategySelector(cfg)

    # Directly plant a prior-day state entry
    from app.strategies.adaptive.strategy_selector import _SelectionState
    yesterday_ts = _ist_ts(2026, 4, 19, 10, 0)
    from app.strategies.adaptive.regime import Regime as _Regime
    selector._state["BANKNIFTY"] = _SelectionState(
        selected=("ema20_strategy",),
        regime=_Regime.TRENDING,
        reason="mapping",
        switched_at=yesterday_ts,
        hold_until=None,
        updated_at=yesterday_ts,
    )

    today = _ist_ts(2026, 4, 20, 9, 30)
    summary = selector.staleness_summary(now=today)

    assert "stale_count=1" in summary["status"]
    assert "BANKNIFTY" in summary["stale_underlyings"]
