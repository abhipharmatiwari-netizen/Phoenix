# Kill Switch — Operational Reference

**Architecture reference:** §12, §12.1, P0 rules

---

## What the kill switch does

The kill switch halts new entry orders for the affected scope. Exit orders that reduce exposure remain allowed.

Scopes: `GLOBAL`, `TENANT`, `ACCOUNT`, `STRATEGY`.

In the current hub-authoritative runtime:

- The hub-path `GlobalKillSwitchInterceptor` (`app/orders/interceptors.py`) blocks new entry orders when `GLOBAL_KILL` environment variable is truthy and `order_router_enforce_global_kill_switch=true`.
- The legacy stream path uses `RiskManager.kill_switch_activated` — a boolean that is persisted to `risk_positions.json` in the legacy path.
- In LIVE hub mode, kill switch durability must use the durable Postgres-backed kill switch state. The `KillSwitchManager` state machine (`app/risk/kill_switch.py`) implements the formal `INACTIVE → TRIPPED → CLEAR_PENDING → CLEARED → INACTIVE` workflow.

---

## How the kill switch is tripped

### Automatic — daily loss threshold

When realized plus unrealized PnL for an account/strategy crosses the configured daily loss threshold, the system automatically activates the kill switch for that scope.

### Manual — global kill switch via environment

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
Authorization: Bearer <ADMIN_API_KEY>
```

Filter for `resource_type=kill_switch` or `action=kill_switch.trip`.

---

## Kill switch clear workflow

The `KillSwitchManager` (`app/risk/kill_switch.py`) implements a formal four-step state machine for clearing:

```
TRIPPED → (request_clear + validation) → CLEAR_PENDING → (confirm_clear) → CLEARED → (rearm) → INACTIVE
```

Rules enforced before clearing (per ARCHITECTURE.md §12.1):
- Required control inputs must be fresh and available.
- No unresolved `RECONCILING` or `ORPHAN_REVIEW` positions may exist for the same control scope (unless a separately audited break-glass override is used).
- Clearing never happens implicitly on restart, on a profitable tick, or on a broker poll.

### Current API status

**The kill switch clear / rearm HTTP API is not yet exposed.** The state machine exists in `app/risk/kill_switch.py` but no `POST /admin/kill-switch/clear` or equivalent endpoint is currently wired.

---

## Practical clear procedures

### Hub path — global kill switch via environment variable

If the kill switch was activated by setting `GLOBAL_KILL=1`:

1. Confirm the triggering condition has been resolved (loss threshold no longer breached, or operator has decided to resume).
2. Update the deployment environment to remove or unset `GLOBAL_KILL`.
3. Restart the backend:
   ```powershell
   docker compose -f .\docker-compose.live.single.yml restart backend
   ```
4. Verify in logs that startup validation passes and no kill switch activation is logged.
5. Confirm fresh entries are flowing through the order router without `ORDER_REJECTED_GLOBAL_KILL` blocks.

### Legacy path — in-memory kill switch

The legacy `RiskManager.kill_switch_activated` is reset on restart because it is in-memory. In LIVE hub mode, do not rely on restart alone to clear a durable kill switch — verify via logs and audit trail that the activating condition is resolved first.

### Postgres-backed kill switch

If the kill switch state is persisted to Postgres and a restart does not clear it:

1. Review the kill switch record in the database:
   ```sql
   SELECT * FROM kill_switch_state WHERE state != 'INACTIVE' ORDER BY updated_at DESC;
   ```
2. Confirm the triggering condition is resolved.
3. Verify no `RECONCILING` or `ORPHAN_REVIEW` positions exist for the affected scope.
4. Update the record state to `CLEARED` only after validation:
   ```sql
   UPDATE kill_switch_state
   SET state = 'CLEARED', cleared_by = 'operator', clear_reason = '<reason>', updated_at = NOW()
   WHERE id = '<record_id>';
   ```
5. Restart the backend to reload state from Postgres.

> **Warning:** Only perform the direct DB update after completing the validation steps in ARCHITECTURE.md §12.1. Auto-clearing on restart or without completing validation is explicitly forbidden.

---

## After clearing

- Verify new entry orders are accepted by the router (monitor logs for clean order flow).
- Confirm kill switch state is `INACTIVE` or absent from audit log.
- Confirm position reconciliation is current and no scopes are in `RECONCILING` or `ORPHAN_REVIEW` before resuming automated live trading.

---

## Known gaps

- No HTTP endpoint for `request_clear`, `confirm_clear`, or `rearm` operations is currently exposed. Operators must use the restart or direct DB approach above.
- Kill switch state is not surfaced in the `/health` or `/health/summary` response directly.

---

## Related

- [Break-Glass Flatten](break_glass_flatten.md)
- [Orphan Review](resolve_orphan_review.md)
- `ARCHITECTURE.md` §12, §12.1
