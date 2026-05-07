# Kill Switch â€” Operational Reference

**Architecture reference:** Â§12, Â§12.1, P0 rules

---

## Purpose

Trip, inspect, clear, and re-arm kill switches without relying on restart side effects.

## Scope

This runbook applies to hub-authoritative Phoenix deployments. The HTTP API is the supported operational path. Direct database mutation is not a routine LIVE procedure.

## What the kill switch does

The kill switch halts new entry orders for the affected scope. Exit orders that reduce exposure remain allowed.

Scopes: `GLOBAL`, `TENANT`, `ACCOUNT`, `STRATEGY`.

In the current hub-authoritative runtime:

- The hub-path `GlobalKillSwitchInterceptor` (`app/orders/interceptors.py`) blocks new entry orders when `GLOBAL_KILL` environment variable is truthy and `order_router_enforce_global_kill_switch=true`.
- The legacy stream path uses `RiskManager.kill_switch_activated` â€” a boolean that is persisted to `risk_positions.json` in the legacy path.
- In LIVE hub mode, kill switch durability must use the durable Postgres-backed kill switch state. The `KillSwitchManager` state machine (`app/risk/kill_switch.py`) implements the formal `INACTIVE â†’ TRIPPED â†’ CLEAR_PENDING â†’ CLEARED â†’ INACTIVE` workflow.

---

## How the kill switch is tripped

### Automatic â€” daily loss threshold

When realized plus unrealized PnL for an account/strategy crosses the configured daily loss threshold, the system automatically activates the kill switch for that scope.

### Manual â€” global kill switch via environment

Setting `GLOBAL_KILL=1` (or `true`) in the backend environment and restarting blocks all new entry orders at the router interceptor level regardless of PnL.

---

## Detecting kill switch state

### Via health endpoint

```powershell
curl.exe http://localhost/health
```

The health response includes `stream_worker_running` and `leader_lease_status` but does not currently surface kill switch state directly.

### Via logs

Search the backend logs for:

```
ORDER_REJECTED_GLOBAL_KILL
kill_switch_activated
kill_switch TRIPPED
```

```powershell
docker compose -f .\docker-compose.live.single.yml logs --tail 500 backend | Select-String "kill_switch"
```

### Via dashboard

The WebSocket dashboard (`WS /ws/dashboard`) includes runtime risk state; look for `kill_switch_active` in the risk state payload.

### Via audit log

```http
GET /admin/audit
X-Admin-Key: <ADMIN_API_KEY>
```

Filter for `resource_type=kill_switch` or `action=kill_switch.trip`.

---

## Kill switch clear workflow

The `KillSwitchManager` (`app/risk/kill_switch.py`) implements a formal four-step state machine for clearing:

```
TRIPPED â†’ (request_clear + validation) â†’ CLEAR_PENDING â†’ (confirm_clear) â†’ CLEARED â†’ (rearm) â†’ INACTIVE
```

Rules enforced before clearing (per ARCHITECTURE.md Â§12.1):
- Required control inputs must be fresh and available.
- No unresolved `RECONCILING` or `ORPHAN_REVIEW` positions may exist for the same control scope (unless a separately audited break-glass override is used).
- Clearing never happens implicitly on restart, on a profitable tick, or on a broker poll.

### HTTP API (authenticated, requires OPERATOR role)

The following endpoints are now wired and fully operational:

```http
# Trip a kill switch
POST /admin/kill-switch/trip
X-Admin-Key: <ADMIN_API_KEY>
{"scope": "GLOBAL", "scope_id": "GLOBAL", "reason": "Daily loss threshold exceeded"}

# Request a clear (TRIPPED â†’ CLEAR_PENDING)
POST /admin/kill-switch/request-clear
X-Admin-Key: <ADMIN_API_KEY>
{"scope": "GLOBAL", "scope_id": "GLOBAL", "reason_code": "loss_resolved", "break_glass": false}

# Confirm the clear (CLEAR_PENDING â†’ CLEARED)
POST /admin/kill-switch/confirm-clear
X-Admin-Key: <ADMIN_API_KEY>
{"scope": "GLOBAL", "scope_id": "GLOBAL"}

# Re-arm (CLEARED â†’ INACTIVE â€” trading resumes)
POST /admin/kill-switch/rearm
X-Admin-Key: <ADMIN_API_KEY>
{"scope": "GLOBAL", "scope_id": "GLOBAL"}

# Query current state
GET /admin/kill-switch/state
X-Admin-Key: <ADMIN_API_KEY>
```

All mutations are audited (resource_type=kill_switch) and persisted to Postgres immediately.

> **Important (Â§132):** `CLEARED` does **not** restore entry eligibility.
> After `confirm_clear` succeeds, the state is `CLEARED` but new entries are
> still blocked until the operator explicitly calls `rearm` to transition to
> `INACTIVE`.  Failing to call `rearm` will leave the system in `CLEARED`
> state indefinitely â€” strategy signals will fire but orders will be blocked.
> Current implementation note: `rearm` requires OPERATOR role authentication but
> does not currently require a step-up token. Treat re-arm as a maker-checker
> operational action outside the app until step-up enforcement is wired for this
> endpoint.

---

## Practical clear procedures

### Hub path â€” global kill switch via environment variable

If the kill switch was activated by setting `GLOBAL_KILL=1`:

1. Confirm the triggering condition has been resolved (loss threshold no longer breached, or operator has decided to resume).
2. Update the deployment environment to remove or unset `GLOBAL_KILL`.
3. Restart the backend:
   ```powershell
   docker compose -f .\docker-compose.live.single.yml restart backend
   ```
4. Verify in logs that startup validation passes and no kill switch activation is logged.
5. Confirm fresh entries are flowing through the order router without `ORDER_REJECTED_GLOBAL_KILL` blocks.

### Legacy path â€” in-memory kill switch

The legacy `RiskManager.kill_switch_activated` is reset on restart because it is in-memory. In LIVE hub mode, do not rely on restart alone to clear a durable kill switch â€” verify via logs and audit trail that the activating condition is resolved first.

### Postgres-backed kill switch

The `kill_switch_state` table is created by migration `007_kill_switch_state.sql`. Non-INACTIVE
records survive restarts via `KillSwitchManager.load_state()`.

If the kill switch state is persisted to Postgres and a restart does not clear it:

1. Review active kill switch records in the database:
   ```sql
   SELECT id, scope, scope_id, state, tripped_at, trip_reason, updated_at
   FROM kill_switch_state
   WHERE state != 'INACTIVE'
   ORDER BY updated_at DESC;
   ```
2. Confirm the triggering condition is resolved.
3. Verify no `RECONCILING` or `ORPHAN_REVIEW` positions exist for the affected scope.
4. Use the HTTP clear and rearm API above. Do not manually update `kill_switch_state` for routine LIVE operation.
5. If the API is unavailable, hold the stack stopped and escalate to incident recovery. Any direct DB repair must be separately approved, captured as break-glass evidence, and followed by a restart plus `/readyz` validation.


---

## After clearing

- Verify new entry orders are accepted by the router (monitor logs for clean order flow).
- Confirm kill switch state is `INACTIVE` or absent from audit log.
- Confirm position reconciliation is current and no scopes are in `RECONCILING` or `ORPHAN_REVIEW` before resuming automated live trading.

## Rollback / recovery

If a clear or rearm was issued incorrectly, immediately trip the same scope again, capture the request/response/audit evidence, and keep automated entries blocked until the triggering condition and reconciliation state are reviewed.

---

## Known gaps

- Legacy `RiskManager.kill_switch_activated` (in-memory + `risk_positions.json`) remains active in the stream runner path. The `KillSwitchManager` is the authoritative source for hub-routed LIVE orders; stream-side legacy exits may still be gated by the legacy flag.
- Kill switch state is surfaced in `/readyz` (`kill_switch_active_count`) and via `GET /admin/kill-switch/state` but not in the `/health` endpoint (which is a liveness probe only).

---

## Related

- [Break-Glass Flatten](break_glass_flatten.md)
- [Orphan Review](resolve_orphan_review.md)
- `ARCHITECTURE.md` Â§12, Â§12.1
