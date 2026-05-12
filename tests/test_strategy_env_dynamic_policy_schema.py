from __future__ import annotations

from pathlib import Path

import yaml


def test_all_ema20_blocks_include_dynamic_policy_stub() -> None:
    cfg_path = Path(__file__).resolve().parents[1] / "app" / "config" / "strategy_env.yaml"
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    strategies = raw.get("strategies") or []

    ema_rows = [
        row
        for row in strategies
        if isinstance(row, dict) and str(row.get("name", "")).strip() == "ema20_strategy"
    ]
    assert ema_rows, "No ema20_strategy blocks found in strategy_env.yaml"

    for row in ema_rows:
        params = row.get("params")
        assert isinstance(params, dict), "ema20_strategy params must be a mapping"
        dyn = params.get("dynamic_policy")
        assert isinstance(
            dyn, dict
        ), "Each ema20_strategy params block must include dynamic_policy stub"
        enabled = bool(dyn.get("enabled", False))
        policy_id = str(dyn.get("policy_id", "") or "").strip()
        if enabled:
            assert policy_id, "dynamic_policy.policy_id must be set when enabled=true"
            profiles = dyn.get("profiles", {})
            assert isinstance(
                profiles, dict
            ), "dynamic_policy.profiles must be a mapping when enabled=true"
            non_no_trade_profiles = [
                v
                for k, v in profiles.items()
                if str(k).upper() != "NO_TRADE" and isinstance(v, dict) and bool(v)
            ]
            assert (
                non_no_trade_profiles
            ), "enabled dynamic_policy requires at least one non-empty regime profile outside NO_TRADE"


def test_nifty_mapping_has_trending_up_and_down_split() -> None:
    """
    Issue #212: NIFTY_IDX selector mapping must declare the operator-recommended
    direction-aware split:
      - TRENDING_UP   -> include exclusive_nifty_ce_buy (bullish long-CE)
      - TRENDING_DOWN -> include put_momentum_scalper  (bearish long-PE)

    The legacy TRENDING bucket is retained for backward-compatible behaviour
    when the classifier cannot resolve direction (missing +DI/-DI/EMA ctx).
    """
    cfg_path = Path(__file__).resolve().parents[1] / "app" / "config" / "strategy_env.yaml"
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    mapping = (
        ((raw.get("strategy_selection") or {}).get("mapping") or {}).get("NIFTY_IDX") or {}
    )
    assert isinstance(mapping, dict), "NIFTY_IDX selector mapping must be a dict"

    assert "TRENDING_UP" in mapping, "NIFTY_IDX mapping must declare TRENDING_UP (issue #212)"
    assert "TRENDING_DOWN" in mapping, "NIFTY_IDX mapping must declare TRENDING_DOWN (issue #212)"
    assert "TRENDING" in mapping, "Legacy TRENDING must remain for backward-compat fallback"

    up = list(mapping["TRENDING_UP"])
    down = list(mapping["TRENDING_DOWN"])

    assert "exclusive_nifty_ce_buy" in up, (
        "TRENDING_UP must include exclusive_nifty_ce_buy (long CE matches bull-trend)"
    )
    assert "put_momentum_scalper" not in up, (
        "TRENDING_UP must not dispatch put_momentum_scalper "
        "(its internal direction-gate would reject every bar)"
    )
    assert "put_momentum_scalper" in down, (
        "TRENDING_DOWN must include put_momentum_scalper (long PE matches bear-trend)"
    )
    assert "exclusive_nifty_ce_buy" not in down, (
        "TRENDING_DOWN must not dispatch exclusive_nifty_ce_buy "
        "(its internal direction-gate would reject every bar)"
    )
