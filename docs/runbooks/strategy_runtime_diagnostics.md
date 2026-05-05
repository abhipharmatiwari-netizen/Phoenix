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

---

## WebSocket connectivity diagnostics

### Expected startup sequence (OCI / proxy deployments)

After the universe build completes, the stream worker initialises the WebSocket runner.
On a healthy OCI deployment the logs should show in order:

```
[INFO] app.core.ws_runner: WebSocket proxy configured: 65.20.69.50:8888
[INFO] app.core.ws_runner: Connecting WebSocket 2.0 for multi-instrument universe...
[INFO] app.core.ws_runner: WebSocket opened, subscribing to multiuniv1 (mode=2) with N tokens...
[INFO] app.core.ws_runner: Subscribed N tokens across M batches
```

### WebSocket disconnects every ~2 minutes

```
[W] smartWebSocketV2: Connection closed due to max retry attempts reached.
[ERROR] websocket: [Errno 110] Connection timed out - goodbye
```

**Cause:** Angel One's firewall blocking direct OCI IP. The WebSocket proxy patch in
`ws_runner.py` is not active.

**Checks:**
```bash
docker exec phoenix-oci-backend env | grep ANGEL_HTTPS_PROXY
docker exec phoenix-oci-backend grep -c 'proxy_type' /app/app/core/ws_runner.py
```

Both must return a value. If not, the bind mount is missing — redeploy with the override.

### `Only http, socks4, socks5 proxy protocols are supported`

websocket-client 1.9.x uses `proxy_type` (not `http_proxy_type`) in `run_forever()`.
Ensure `ws_runner.py` passes `proxy_type="http"` to `run_forever()`.

### `WebSocketApp.run_forever() got an unexpected keyword argument 'http_proxy_type'`

Same root cause — wrong parameter name. Must be `proxy_type` for websocket-client ≥ 1.9.

---

## Stream watchdog diagnostics

### `Stream watchdog failed to restart worker: int too large to convert to float`

**Cause:** Watchdog backoff uses integer exponentiation (`2 ** attempts`). After ~1038
failed restart attempts the integer exceeds Python float max and `float(int)` raises
`OverflowError`.

**Fix (already applied):** `app_runtime.py` uses `2.0 ** attempts` (float base), which
naturally clamps to `float('inf')` for large exponents. `min(300.0, inf) == 300.0`.

If this error reappears, the `app_runtime.py` bind mount has been lost. Redeploy.

### Watchdog suppresses restart: `non-retryable worker error`

```
[ERROR] Stream watchdog restart suppressed due to non-retryable worker error: Hub route validation failed
```

**Cause:** `strategy_configs` table has no rows for the active strategies. The watchdog
correctly stops retrying because retrying cannot fix a missing database row.

**Fix:** Seed the `strategy_configs` table. See [oci_live_deployment.md](oci_live_deployment.md).
