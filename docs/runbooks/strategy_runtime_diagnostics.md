# Strategy Runtime Diagnostics

## `STRATEGY_BAR_SKIP` events

`app/runners/multi_instrument_stream.py` now emits structured `STRATEGY_BAR_SKIP` logs before a strategy bar callback is skipped for:

- `strategy_switch_disabled`
- `instrument_policy_blocked`
- `selector_blocked`
- `underlying_mismatch`
- `strict_intraday_cutoff_blocked`

Each event includes `underlying`, `strategy`, `reason`, `timeframe_seconds`, and `bar_start_ts`.

Dispatch happens once per attached strategy for each closed underlying bar, so skip logging is naturally throttled to one event per strategy/bar pair. If the blocking reason changes, the next closed bar emits the new reason.

## Startup snapshot artifact

At process startup the stream runner writes:

`logs/<YYYY-MM-DD>/strategy_runtime_startup_snapshot_<UTC timestamp>.json`

Key fields:

- `process_start_ts`: UTC process start timestamp for the stream worker.
- `config_hash`: SHA-256 hash of the startup strategy config, selected env overrides, selector settings, and strict intraday settings.
- `attached_strategies_by_underlying`: final runtime attachments after startup pruning.
- `instrument_policies`: current enabled flags and allowed strategy lists per underlying.
- `strategy_switch`: startup strategy enable flags.
- `selector`: selector config, including `ema20_is_authoritative`.
- `selector_state`: selector warmup state at startup, if available.
- `strict_intraday`: strict intraday enforcement settings.
- `env_overrides`: relevant startup env overrides. Sensitive keys are redacted.
