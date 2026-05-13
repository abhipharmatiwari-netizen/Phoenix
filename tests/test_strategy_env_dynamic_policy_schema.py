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
    Issue #212 (round-2, after Codex review on PR #265):

    The NIFTY_IDX selector mapping must declare TRENDING_UP / TRENDING_DOWN
    with the direction-matched strategy at POSITION 1 so it survives the
    AUTO_STRATEGY_MAX_ACTIVE_PER_UNDERLYING cap (even at cap=1):
      - TRENDING_UP:   exclusive_nifty_ce_buy first  (long CE bull-trend)
      - TRENDING_DOWN: put_momentum_scalper  first   (long PE bear-trend)

    The OPPOSITE-direction strategy must be RETAINED in each split
    (Codex round-1 P1 on PR #265). Selector membership gates
    `on_bar`/`on_tick` dispatch, which in turn runs each strategy's
    SL/TP/EOD exit logic. Dropping the opposite-direction strategy would
    leave any open position from the prior direction unmanaged after
    a regime flip. The opposite-direction strategy stays selected for
    MANAGEMENT only — its internal direction-gate self-rejects entries.

    Legacy TRENDING is retained for backward compatibility.
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

    # --- Direction-matched strategy at position 1 (cap=1 survival) ---
    assert up[0] == "exclusive_nifty_ce_buy", (
        "TRENDING_UP position-1 must be exclusive_nifty_ce_buy so it "
        "survives AUTO_STRATEGY_MAX_ACTIVE_PER_UNDERLYING=1; "
        f"got {up!r}"
    )
    assert down[0] == "put_momentum_scalper", (
        "TRENDING_DOWN position-1 must be put_momentum_scalper so it "
        "survives AUTO_STRATEGY_MAX_ACTIVE_PER_UNDERLYING=1; "
        f"got {down!r}"
    )

    # --- Opposite-direction strategy retained for cross-flip management ---
    # (Codex round-1 P1 on PR #265: open CE position from TRENDING_UP must
    #  remain managed if regime flips to TRENDING_DOWN; ditto open PE.)
    assert "put_momentum_scalper" in up, (
        "TRENDING_UP must retain put_momentum_scalper for cross-flip "
        "management of open PE positions (PR #265 round-1 P1); "
        f"got {up!r}"
    )
    assert "exclusive_nifty_ce_buy" in down, (
        "TRENDING_DOWN must retain exclusive_nifty_ce_buy for cross-flip "
        "management of open CE positions (PR #265 round-1 P1); "
        f"got {down!r}"
    )

    # --- Spread retained for management in both splits ---
    assert "nifty_weekly_credit_spreads" in up, (
        f"TRENDING_UP must include nifty_weekly_credit_spreads; got {up!r}"
    )
    assert "nifty_weekly_credit_spreads" in down, (
        f"TRENDING_DOWN must include nifty_weekly_credit_spreads; got {down!r}"
    )

    # --- Cap=3 set-membership invariance across direction flips ---
    # LIVE deployment must run with AUTO_STRATEGY_MAX_ACTIVE_PER_UNDERLYING=3
    # (env default is 2 — see app/config/settings.py — and must be
    # overridden in /opt/phoenix/phoenix-deploy.env). Under cap=3 the
    # active-strategy SET is identical across the two splits; only the
    # ordering differs. A fast direction flip during
    # AUTO_STRATEGY_MIN_HOLD_SECONDS therefore does not change the
    # dispatched strategy set — the hold window cannot defer management
    # coverage (PR #265 Codex round-1 P2 #2 fix). The cap=3 prerequisite
    # itself is the PR #265 Codex round-2 P1 fix (without it, the spread
    # is truncated out of the TRENDING_UP/DOWN selection at position 3
    # and any spread carried over from NORMAL/CHOPPY/HIGH_VOL would lose
    # its on_bar/on_tick management).
    assert len(up) >= 3 and len(down) >= 3, (
        "TRENDING_UP / TRENDING_DOWN must list at least 3 strategies "
        "(direction-matched, opposite-direction, spread) so cap=3 "
        f"captures all three; got up={up!r} down={down!r}"
    )
    assert set(up[:3]) == set(down[:3]), (
        "Cap=3 selection must have identical set-membership across "
        f"TRENDING_UP[:3]={up[:3]!r} and TRENDING_DOWN[:3]={down[:3]!r}"
    )

    # --- Legacy TRENDING must stay capped at 2 entries ---
    # PR #265 round-6 (Codex round-5 P2 "Preserve legacy TRENDING
    # truncation when raising the cap"): the committed cap=3 default
    # (round-4) inadvertently let ``exclusive_nifty_ce_buy`` fire on the
    # legacy TRENDING fallback path (the previous env-default cap=2
    # truncated it). Trim legacy TRENDING back to 2 entries so direction-
    # fallback dispatch matches the pre-#265 behaviour.
    legacy_trending = list(mapping["TRENDING"])
    assert len(legacy_trending) == 2, (
        f"Legacy TRENDING must be 2 entries (cap=3 default would otherwise "
        f"fire all 3); got {legacy_trending!r}"
    )
    assert "exclusive_nifty_ce_buy" not in legacy_trending, (
        "Legacy TRENDING must NOT include exclusive_nifty_ce_buy after "
        "round-6 trim — direction-fallback should match pre-#265 "
        f"dispatch [spread, put_momentum]; got {legacy_trending!r}"
    )
    # --- HIGH_VOL retains CE for post-TRENDING_UP management ---
    # Codex P1 follow-up on PR #265: a CE opened during TRENDING_UP must
    # continue receiving on_bar/on_tick management after a later move into
    # HIGH_VOL. Keep the historical first-two prefix for spread / put, but
    # include CE as the third cap=3 member.
    high_vol = list(mapping["HIGH_VOL"])
    assert high_vol[:2] == [
        "nifty_weekly_credit_spreads",
        "put_momentum_scalper",
    ], f"HIGH_VOL must preserve the historical cap=2 prefix; got {high_vol!r}"
    assert "exclusive_nifty_ce_buy" in high_vol[:3], (
        "HIGH_VOL must retain exclusive_nifty_ce_buy within the cap=3 "
        "selection so CE positions opened in TRENDING_UP continue to be "
        f"managed after a HIGH_VOL transition; got {high_vol!r}"
    )
