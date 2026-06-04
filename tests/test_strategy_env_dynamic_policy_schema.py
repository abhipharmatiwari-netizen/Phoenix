from __future__ import annotations

from pathlib import Path

import yaml


def _strategy_env() -> dict:
    cfg_path = Path(__file__).resolve().parents[1] / "app" / "config" / "strategy_env.yaml"
    return yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}


def test_all_ema20_blocks_include_dynamic_policy_stub() -> None:
    raw = _strategy_env()
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


def test_live_strategy_env_allows_only_ema20_strategy() -> None:
    raw = _strategy_env()
    strategies = raw.get("strategies") or []

    enabled_names = {
        str(row.get("name", "")).strip()
        for row in strategies
        if isinstance(row, dict) and bool(row.get("enabled", True))
    }
    assert enabled_names == {"ema20_strategy"}

    instruments = raw.get("instruments") or {}
    for name, cfg in instruments.items():
        if not isinstance(cfg, dict) or not bool(cfg.get("enabled", True)):
            continue
        allowed = list(cfg.get("allowed_strategies") or [])
        assert allowed == ["ema20_strategy"], (
            f"{name} must only allow ema20_strategy in live mode; got {allowed!r}"
        )

    selector = raw.get("strategy_selection") or {}
    assert int(selector.get("max_active_per_underlying", 0)) == 1
    mapping = selector.get("mapping") or {}
    for underlying, regime_map in mapping.items():
        assert isinstance(regime_map, dict), f"{underlying} selector mapping must be a dict"
        for regime, selected in regime_map.items():
            listed = list(selected or [])
            assert set(listed).issubset({"ema20_strategy"}), (
                f"{underlying}.{regime} must not route non-EMA strategies; got {listed!r}"
            )
