# Strategy Runtime Diagnostics

## Purpose

Diagnose stream-worker, bar dispatch, selector, and strategy attachment issues without changing LIVE runtime authority.

## Scope

This guide is read-only unless it explicitly tells the operator to redeploy through a current runbook. It applies to the current hub-authoritative stream-worker path. It does not approve source-code bind mounts, legacy-authoritative mode, or `DISABLE_STREAM_WORKER=true` in LIVE.

## Preconditions

- You have log access for the backend container or host log volume.
- You are operating the current OCI VM deployment described in
  [OCI VM Runtime Evidence](../OCI_VM_RUNTIME.md).
- You can capture `/readyz`, backend logs, and the startup snapshot artifact before making changes.

## `STRATEGY_BAR_SKIP` events

`app/runners/multi_instrument_stream.py` now emits structured `STRATEGY_BAR_SKIP` logs before a strategy bar callback is skipped for:

- `strategy_switch_disabled`
- `instrument_policy_blocked`
- `selector_blocked`
- `underlying_mismatch`
- `strict_intraday_cutoff_blocked`

Each event includes `underlying`, `strategy`, `reason`, `timeframe_seconds`, and `bar_start_ts`.

Dispatch happens once per attached strategy for each closed underlying bar, so skip logging is naturally throttled to one event per strategy/bar pair. If the blocking reason changes, the next closed bar emits the new reason.

## Put-momentum no-position exit rejections

Current live note: as of 2026-06-03, `put_momentum_scalper` is disabled in LIVE
routing. These diagnostics are for historical log review or stale-image
verification only; a current live backend should not attach or dispatch this
strategy.

Observed during 2026-06-03 OCI live monitoring: after a put-momentum exit filled and
broker-flat evidence was observed, stale in-memory strategy state could continue to
emit `PUT_MOM_EXIT_*` orders. The router correctly rejected those exits in LIVE with:

```
event_type=ORDER_EXIT_REJECTED_NO_POSITION_EVIDENCE
message=exit_order_missing_position_evidence
```

The strategy now treats that response as terminal stale-position evidence: it retires
the local put-momentum position, marks the adaptive policy position closed, resets exit
retry state, and does not open the exit circuit. A repeated
`ORDER_EXIT_REJECTED_NO_POSITION_EVIDENCE` for the same put-momentum symbol after this
change means the running image is stale or another strategy instance still owns stale
state. Verify the deployed image tag, then inspect strategy attachments and restart
through [oci_live_deployment.md](oci_live_deployment.md).

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
[INFO] app.core.ws_runner: WebSocket proxy configured: <PROXY_HOST>:8888
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

Both must return a value. The verified VM currently uses source-file bind mounts
for selected runtime files; treat those as current drift, not a pattern to
extend. If the proxy patch is absent, redeploy or restore only through
[oci_live_deployment.md](oci_live_deployment.md) and capture fresh VM evidence.

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

If this error reappears, the running image is stale. Redeploy a pinned image through the current deployment runbook.

### Watchdog suppresses restart: `non-retryable worker error`

```
[ERROR] Stream watchdog restart suppressed due to non-retryable worker error: Hub route validation failed
```

**Cause:** `strategy_configs` table has no rows for the active strategies. The watchdog
correctly stops retrying because retrying cannot fix a missing database row.

**Fix:** Seed the `strategy_configs` table. See [oci_live_deployment.md](oci_live_deployment.md).

## Dashboard health diagnostics

Use the authenticated or backend-local health summary when investigating
Overview or Safety cards:

```bash
docker exec phoenix-oci-backend curl -sS http://localhost:8080/health/summary
curl -sk -H "X-Admin-Key: ${ADMIN_KEY}" \
  https://127.0.0.1:8443/admin/health/summary
```

Public nginx `/health/summary` is redacted. `Unknown` for Schema Status,
Tracked Accounts, or Watchdog in a public or unauthenticated view is not enough
evidence for a runtime failure. If authenticated `/admin/health/summary` reports
`schema_status=ok`, a positive `tracked_account_count`, and
`watchdog_running=true`, the dashboard is seeing redaction rather than a stopped
component.

If the authenticated summary reports watchdog stopped, inspect the watchdog
container before changing backend code:

```bash
docker ps --filter name=phoenix-oci-watchdog
docker logs --tail=120 phoenix-oci-watchdog
docker inspect phoenix-oci-watchdog --format '{{json .Mounts}}'
```

The hardened watchdog is observe-only. It should have no mounts and should not
stop or start nginx. nginx stop/start logs indicate stale VM wiring or an
override drift, not normal watchdog behavior.

## Validation

Capture these after any diagnostic action:

```bash
docker logs --tail 300 phoenix-oci-backend
docker exec phoenix-oci-backend wget -qO- http://localhost:8080/readyz
```

Expected success evidence:

- `/readyz` reports stream-worker and balance-sync readiness for automated LIVE.
- backend logs show universe build, WebSocket subscription, and strategy attachment without fatal restart loops.
- the latest startup snapshot exists under the mounted log path.
- for current EMA20-only LIVE, enabled underlyings attach only `ema20_strategy`,
  non-EMA entries are disabled in `strategy_switch`, and selector mappings for
  enabled underlyings contain no non-EMA strategies.

## Failure handling and rollback

If diagnostics show stale code, missing strategy config, or broken market-data, keep automated entries disabled or stop the stack. Roll back to the last known-good image/config through the deployment runbook and repeat `/readyz` plus log validation before allowing automated LIVE entries.
