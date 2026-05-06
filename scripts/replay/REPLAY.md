# Phoenix v9 Replay

Offline replay executes Phoenix strategy code against historical `indicator_bars`
while keeping order routing strictly local to replay. It extends the existing
bridge-isolation path instead of building a disconnected simulator.

## Safety

- Replay uses `isolated_replay_flag(...)` and `isolated_replay_order_sink(...)`.
- `place_order_via_bridge(...)` fails fast if replay isolation is enabled without a replay sink.
- Replay never calls broker clients, live hub runtime, or control-plane account discovery.
- Replay treats `indicator_bars` as read-only. The replay harness patches
  `exclusive_nifty_ce_buy` so it cannot open/backfill the private indicator store.
- Live trading behavior is unchanged because all execution changes live under `scripts/replay/`
  plus the pre-existing replay short-circuit in `app/orders/strategy_bridge.py`.

## What Replay Uses

- Strategies:
  - `ema20_strategy` -> `NIFTY`, `BANKNIFTY`, `NG_FUT`
  - `put_momentum_scalper` -> `NIFTY`, `BANKNIFTY`
  - `exclusive_nifty_ce_buy` -> `NIFTY`
- Default timeframes:
  - `ema20_strategy` -> `300s`
  - `put_momentum_scalper` -> `300s` + `900s`
  - `exclusive_nifty_ce_buy` -> `30s`
- Data source:
  - PostgreSQL `indicator_bars`
  - Schema-aware loading adapts when optional columns are missing
  - `exclusive_nifty_ce_buy_ema20_30s` is consumed when present

## Event Model

- Bars are loaded ordered by `ts_start ASC`.
- Replay dispatches by effective bar-close time to avoid multi-timeframe look-ahead.
- Same-timestamp higher timeframes dispatch before lower timeframes.
- Replay patches strategy clocks to simulated time.
- Tick replay uses synthetic `open_close` or `ohlc` paths built from OHLC.
- Open positions are finalized explicitly at session boundaries and at replay-window end.

## Execution Model

Supported replay execution flags:

- `--fill-mode bar_close_fill`
- `--fill-mode next_bar_open_fill`
- `--tick-model open_close`
- `--tick-model ohlc`
- `--slippage-bps`
- `--fixed-slippage`
- `--spread-bps`
- `--latency-bars`

Replay returns bridge-compatible `OrderResponse` objects and writes fills only to
replay-local logs. Option fills use a deterministic underlying-driven ATM proxy;
they are still approximate and are called out in the report assumptions.

## CLI

```bash
python -m scripts.replay.run_replay \
  --dsn "postgresql://user:pass@host:5432/phoenix" \
  --mode both \
  --start-date 2026-01-01 \
  --end-date 2026-03-31 \
  --output-dir replay_output
```

Useful additive flags:

```bash
python -m scripts.replay.run_replay \
  --dsn "postgresql://..." \
  --mode optimize \
  --walk-forward \
  --train-days 60 \
  --test-days 20 \
  --step-days 20 \
  --fill-mode next_bar_open_fill \
  --tick-model ohlc \
  --slippage-bps 5 \
  --spread-bps 3
```

## Outputs

Replay writes:

- `replay_summary.txt`
- `replay_report.md`
- `replay_results.json`
- `recommendations.yaml`
- `recommendation_summary.json`
- `trades_<strategy>_<underlying>.csv`
- `fills_<strategy>_<underlying>.csv`
- `gate_summary_<strategy>_<underlying>.csv`
- `optimization_<strategy>_<underlying>.csv`
- `parameter_sensitivity_<strategy>_<underlying>.csv`

The report bundle includes:

- run summary
- trade log
- fill log
- gate summary
- loss buckets
- time-of-day summary
- regime summary
- optimization results
- parameter sensitivity
- review-only recommendation exports
- explicit replay assumptions and remaining approximations

## Optimization

- Uses strategy-specific grids instead of one generic search space.
- Loads current defaults from `app/config/strategy_env.yaml` when possible and falls back to replay defaults.
- Ranks by robustness, not raw PnL alone.
- Applies minimum-trade guardrails.
- Supports walk-forward out-of-sample scoring.
- Exports review-only YAML/JSON recommendations. Replay never auto-edits `strategy_env.yaml`.

### EMA20 profit-booking sweeps (PHX#182, #183, #184, #185)

`EMA20_GRIDS["default"]` covers `tp_pct ∈ {0.15, 0.20, 0.25, 0.30, 0.35, 0.45, 0.50}` —
the explicit sweep called for in PHX#185. Per-regime tuning
(TRENDING / NORMAL / CHOPPY / HIGH_VOL) is achieved by segmenting replay
output by `regime_at_entry` from `exit_attribution.jsonl` and ranking each
slice independently in post-processing.

The new profit-booking params (PHX#182 TP1, PHX#183 give-back, PHX#184 decay)
are NOT auto-merged into the default grid — combinatorial blow-up would dilute
coverage. Use `extended_ema20_grid(...)` from `optimizer_runtime` to compose
focused sweeps:

```python
from scripts.replay.optimizer_runtime import extended_ema20_grid

# PHX#185 tp_pct sweep, baseline (no profit-booking enhancements)
grid = extended_ema20_grid("NIFTY")  # ~504 combos

# Enable TP1 sweep alongside tp_pct
grid = extended_ema20_grid("NIFTY", include_tp1=True)  # ~6048 combos

# Full sweep — requires --max-combos 100000+
grid = extended_ema20_grid(
    "NIFTY",
    include_tp1=True,
    include_giveback=True,
    include_decay=True,
)
```

Sequencing recommended in PHX#185:
1. Run baseline `tp_pct` sweep (`grid = extended_ema20_grid("NIFTY")`) — pick top
   3 `tp_pct` values per regime.
2. Hold `tp_pct` to top values, sweep `tp1_pct + tp1_qty_pct`
   (`include_tp1=True`).
3. Smoke-test final config in DEMO mode for one full trading day before LIVE
   rollout.

## Remaining Approximations

- Historical option chains are still unavailable, so option marks/fills remain proxy-based.
- Volume-sensitive logic is limited by what exists in `indicator_bars`.
- Synthetic tick ordering improves realism but cannot reproduce true intra-bar tick tape.
- Walk-forward validation evaluates held-out windows; it is still not a substitute for full live shadowing.
