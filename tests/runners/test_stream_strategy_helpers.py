from __future__ import annotations

from app.runners.stream_strategy_helpers import (
    iter_instruments,
    normalize_strategy_entries,
    resolve_delta_qty_config,
)


def test_normalize_strategy_entries_supports_list_and_mapping_shapes():
    assert normalize_strategy_entries([{"name": "ema20_strategy"}]) == [
        {"name": "ema20_strategy"}
    ]
    assert normalize_strategy_entries(
        {"ema20_strategy": {"enabled": True}}
    ) == [{"enabled": True, "name": "ema20_strategy"}]


def test_iter_instruments_normalizes_string_and_mapping_entries():
    items = list(
        iter_instruments(
            [
                "NIFTY_IDX",
                {"underlying": "BANKNIFTY_IDX", "enabled": True},
                {"instrument": "NG_FUT"},
            ]
        )
    )

    assert items == [
        {"label": "NIFTY_IDX"},
        {"underlying": "BANKNIFTY_IDX", "enabled": True, "label": "BANKNIFTY_IDX"},
        {"instrument": "NG_FUT", "label": "NG_FUT"},
    ]


def test_resolve_delta_qty_config_parses_default_and_overrides():
    qty, overrides = resolve_delta_qty_config(
        {
            "delta": {
                "delta_qty_per_leg": "2",
                "delta_qty_per_leg_by_underlying": {
                    "nifty_idx": "3",
                    "banknifty_idx": 0,
                },
            }
        }
    )

    assert qty == 2
    assert overrides == {"NIFTY_IDX": 3}
