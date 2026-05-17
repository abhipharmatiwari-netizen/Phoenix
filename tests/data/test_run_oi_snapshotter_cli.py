from __future__ import annotations

from scripts.data.run_oi_snapshotter import parse_args


def test_run_oi_snapshotter_cli_leaves_env_gates_available_when_flags_omitted():
    args = parse_args([])

    assert args.enable is None
    assert args.once is None
    assert args.expiry is None


def test_run_oi_snapshotter_cli_enable_flag_overrides_env_gate():
    args = parse_args(["--enable", "--once", "--expiry", "2026-05-19"])

    assert args.enable is True
    assert args.once is True
    assert args.expiry == "2026-05-19"
