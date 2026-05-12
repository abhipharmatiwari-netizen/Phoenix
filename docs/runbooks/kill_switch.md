# Kill Switch — Operational Reference

**Architecture reference:** §12, §12.1, P0 rules

**Related runbooks:**
- [Dashboard Kill Switch](dashboard-kill-switch.md) — admin UI on the Safety page (PR #240)
- [Break-Glass Flatten](break_glass_flatten.md) — audited single-contract exit
- [OCI LIVE Deployment](oci_live_deployment.md) — including the `RISK_MAX_DAILY_LOSS` sizing section (#221 / #243)

---

## Purpose

Trip, inspect, clear, and re-arm kill switches without relying on restart side effects.

## Scope

This runbook applies to hub-authoritative Phoenix deployments. The HTTP API (and the matching dashboard panel) is the supported operational path. Direct database mutation is not a routine LIVE procedure.

## What the kill switch does

The kill switch halts new entry orders for the affected scope. Whether exit orders are also blocked depends on the trip mode:

- **SOFT trip** (`block_exits=False`, the default for daily-loss auto-trips): new entries are blocked, exit orders that reduce exposure remain allowed.
- **HARD trip** (`block_exits=True`, available via the dashboard "HARD trip" checkbox or the `block_exits` field on `POST /admin/kill-switch/trip`): both entries **and** exits are blocked at the router interceptor. Operators must flatten manually in the broker UI.

The SOFT/HARD distinction was introduced in PR #233 (Issue #220). The interceptor (`GlobalKillSwitchInterceptor` in `app/orders/interceptors.py`) consults `KillSwitchManager.is_tripped_for_scope_with_block_exits` and, on a HARD trip, rejects exit-reducing orders too.

Scopes: `GLOBAL`, `TENANT`, `ACCOUNT`, `STRATEGY`.

In the current hub-authoritative runtime:

- The hub-path `GlobalKillSwitchInterceptor` blocks orders when the durable `KillSwitchManager` reports tripped for any matching scope, and additionally when `GLOBAL_KILL` environment variable is truthy and `order_router_enforce_global_kill_switch=true`.
- The legacy stream path uses `RiskManager.kill_switch_activated`. PR #231 (Issue #218) bridges legacy auto-trips into the durable `KillSwitchManager` so the hub interceptor sees them; PR #234 (Issue #222) surfaces any remaining legacy↔durable divergence in `/readyz`.
- In LIVE hub mode the durable Postgres-backed state is authoritative. `KillSwitchManager` (`app/risk/kill_switch.py`) implements the formal `INACTIVE → TRIPPED → CLEAR_PENDING → CLEARED → INACTIVE` workflow.

---

## State machine and invariants

```
INACTIVE ──trip──▶ TRIPPED ──request_clear──▶ CLEAR_PENDING ──confirm_clear──▶ CLEARED ──rearm──▶ INACTIVE
```

Each transition is a separate, explicit, audited operator call. None happen automatically.

**Key invariants** (enforced by `KillSwitchManager`; see `app/risk/kill_switch.py`):

- `trip()` rejects any record whose current state is **not** `INACTIVE` with `ValueError`. In particular, `CLEARED` is not a valid source for a new trip — the operator must rearm to `INACTIVE` first, then trip again. The dashboard surfaces only the state-appropriate action button.
- `CLEARED` does **not** restore entry eligibility — `rearm` is still required to move to `INACTIVE`.
- In LIVE, `rearm` requires a single-use 5-minute step-up token (`action_class=kill_switch_rearm`, see §15.4). Tokens are bound to actor + action class + resource id; PR #240 explicitly removed the dashboard auto-mint, so the operator must obtain the token via a separate ceremony.
- `block_exits` can be upgraded or downgraded on an existing `TRIPPED` / `CLEAR_PENDING` record via `KillSwitchManager.set_block_exits` (audited as `kill_switch.set_block_exits`) without a clear/rearm cycle.

---

## Operator SOP — what to do when the kill switch trips

Use this checklist for every trip, automatic or manual. Steps 1–3 are verification; only after they pass do you progress to clear/rearm.

### Step 1 — Confirm both legacy and durable state agree

Two stores represent kill-switch state and they can disagree:

| Store | Source of truth for | How to read |
|---|---|---|
| Durable `KillSwitchManager` (Postgres `kill_switch_state` table) | Hub interceptor decisions in LIVE | `GET /admin/kill-switch/state` — admin auth required |
| Legacy `RiskManager.kill_switch_activated` (in-memory + `risk_positions.json`) | Stream-path legacy exits | Search backend logs for `kill_switch_activated`, or inspect `risk_positions.json` |

PR #231 (Issue #218) bridges legacy auto-trips into the durable manager. If they disagree, `/readyz` will surface `kill_switch_divergence_detected` (PR #234 / Issue #222). **Do not** proceed to clear while divergence is reported — capture the evidence and re-run the bridge before touching the durable state.

### Step 2 — Verify broker-side flat directly in the broker terminal

Phoenix's internal `/positions` view is **not** trustworthy during a kill-switch-active window:

- `BROKER_SYNC` is intentionally suppressed while the kill switch is active, so the internal `StateStore` mirrors the last pre-trip snapshot rather than live broker state.
- External fills (from the broker mobile app, phone-in flatten, or in-flight orders that filled after the trip) will not be reflected until BROKER_SYNC is re-enabled.

Log directly into the Angel One terminal (web or mobile) and read the **broker-side** positions tab. That is the only authoritative view of what is actually open.

### Step 3 — If residual exposure exists, square off manually

If broker-side positions are non-zero, you have three options ordered from preferred to last-resort:

1. **Dashboard "Cancel ALL Open Orders"** (PR #240). Use this first to drain non-terminal child orders for every registered `AccountRunner`. It does not place exits; it only cancels working orders. See `dashboard-kill-switch.md` for the message-disambiguation semantics (`skipped` / `raced_filled` / `failed`).
2. **`POST /admin/break-glass/flatten`** (audited single-contract exit through the hub router at `BREAK_GLASS` mutation priority). Requires a `break_glass` step-up token in LIVE. See `break_glass_flatten.md` for the full request schema and required fields.
3. **Broker UI direct close** — log into the Angel One terminal and square off positions there. This is the last-resort path; record the action in the incident log alongside the broker order id so the audit trail can be reconciled later.

Both the dashboard "Cancel ALL" and `break-glass/flatten` are audited (`resource_type=broker_orders` and `action=break_glass_flatten` respectively). A broker-UI direct close is **not** captured by Phoenix audit — the operator must paste the broker order id into the incident timeline.

### Step 4 — Only then follow the clear / rearm sequence

After (and only after) broker-side flat is confirmed and BROKER_SYNC has been re-enabled and the internal `StateStore` reflects the flat state:

1. `POST /admin/kill-switch/request-clear` (or the dashboard "Request Clear" button) — validation will refuse if `RECONCILING` / `ORPHAN_REVIEW` is still outstanding.
2. `POST /admin/kill-switch/confirm-clear` — moves to `CLEARED`.
3. Obtain a `kill_switch_rearm` step-up token in LIVE (`POST /admin/step-up/issue` with `action_class=kill_switch_rearm`, `resource_id=GLOBAL` for the global scope).
4. `POST /admin/kill-switch/rearm` with the token — moves back to `INACTIVE`. Entry eligibility is restored only at this step.

Do **not** issue clear/rearm based on Phoenix's empty position view alone — see Step 2.

---

## Automated exit engines during kill-switch active

This subsection documents the status-quo behaviour of the automated exit engines (trailing-lock, profit-lock, EOD exit) when the kill switch is tripped. Issue #220 / PR #233 introduced the SOFT/HARD distinction below; PR #231 (Issue #218) gates trailing-lock on the durable `KillSwitchManager` so a legacy auto-trip is correctly observed by the hub-side exits.

| Trip mode | New entries | Strategy-driven exit orders | Automated exit engines (trailing-lock, profit-lock, EOD) |
|---|---|---|---|
| **SOFT** (default) | Blocked at router | Allowed (still pass risk-reducing gate) | Allowed — the engines can still close exposure |
| **HARD** (`block_exits=True`) | Blocked at router | Blocked at router (`is_tripped_for_scope_with_block_exits → block_exits=True`) | Blocked. Operator must flatten manually in the broker UI. |

**Operator warning.** On a SOFT trip the trailing-lock and EOD-exit engines continue to fire on stream-path market-data updates. If you intend "stop everything including exits", you must trip **HARD** (the dashboard panel exposes the checkbox). Otherwise these engines can produce duplicate fills against stale internal position state during the BROKER_SYNC-suppressed window.

**Auto-trip path.** Daily-loss / drawdown auto-trips from `RiskManager` use SOFT by default — the policy intent is "stop new exposure, let existing exits work". PR #231 bridges these into the durable manager so the hub interceptor and the dashboard observe them. To escalate an auto-trip from SOFT to HARD in-place (without a clear/rearm cycle), call `KillSwitchManager.set_block_exits` or use the corresponding dashboard control; the action is audited as `kill_switch.set_block_exits`.

---

## How the kill switch is tripped

### Automatic — daily loss threshold

When realized plus unrealized PnL for an account/strategy crosses `-abs(RISK_MAX_DAILY_LOSS)`, the system automatically activates the kill switch for that scope. See [OCI LIVE Deployment — Sizing the daily-loss limit by capital tier](oci_live_deployment.md#sizing-the-daily-loss-limit-by-capital-tier) for the LIVE floor (default ₹5,000) and per-tier guidance.

Auto-trips are SOFT by default (see Automated exit engines above).

### Manual — dashboard (preferred)

Use the Safety page's **Global Kill Switch** panel (PR #240). Tick **HARD trip** when you also need exits blocked. Reason is required and is persisted to the audit log. See `dashboard-kill-switch.md` for the full playbook.

### Manual — HTTP API

Direct API calls remain supported for cases where the dashboard is unavailable. See the HTTP API section below.

### Manual — global kill switch via environment

Setting `GLOBAL_KILL=1` (or `true`) in the backend environment and restarting blocks all new entry orders at the router interceptor level regardless of PnL. This is a coarse, restart-coupled fallback; prefer the dashboard or the durable API.

---

## Detecting kill switch state

### Via dashboard

Safety page top-of-page card. Coloured state pill:

- 🟩 **INACTIVE** — normal operation
- 🟥 **TRIPPED** — new orders blocked (HARD also blocks exits)
- 🟧 **CLEAR_PENDING** — clear requested, awaiting confirmation
- 🟦 **CLEARED** — confirmed, ready to rearm

### Via the durable API

```http
GET /admin/kill-switch/state
X-Admin-Key: <ADMIN_API_KEY>
```

### Via `/readyz`

`/readyz` exposes `kill_switch_active_count` and, post-PR #234, `kill_switch_divergence_detected` (true when legacy and durable stores disagree).

### Via health endpoint

```powershell
curl.exe http://localhost/health
```

`/health` is a liveness probe and does not surface kill-switch state directly.

### Via logs

Search the backend logs for:

```
ORDER_REJECTED_GLOBAL_KILL
kill_switch_activated
kill_switch TRIPPED
kill_switch CLEAR_PENDING
kill_switch CLEARED
kill_switch RE-ARMED
kill_switch BLOCK_EXITS UPDATED
```

```powershell
docker compose -f .\docker-compose.live.single.yml logs --tail 500 backend | Select-String "kill_switch"
```

### Via audit log

```http
GET /admin/audit?resource_type=kill_switch
X-Admin-Key: <ADMIN_API_KEY>
```

The dashboard's Safety page also merges `kill_switch`, `broker_orders` (cancel-all bulk events), and `break_glass` audit events into a single table.

---

## HTTP API reference (authenticated, ADMIN role)

```http
# Trip
POST /admin/kill-switch/trip
X-Admin-Key: <ADMIN_API_KEY>
{"scope": "GLOBAL", "scope_id": "GLOBAL", "reason": "Daily loss threshold exceeded", "block_exits": false}

# Request a clear (TRIPPED → CLEAR_PENDING)
POST /admin/kill-switch/request-clear
X-Admin-Key: <ADMIN_API_KEY>
{"scope": "GLOBAL", "scope_id": "GLOBAL", "reason_code": "loss_resolved", "break_glass": false}

# Confirm the clear (CLEAR_PENDING → CLEARED)
POST /admin/kill-switch/confirm-clear
X-Admin-Key: <ADMIN_API_KEY>
{"scope": "GLOBAL", "scope_id": "GLOBAL"}

# STEP 1 of rearm — issue a step-up token (LIVE only, 5-minute TTL, single-use, actor-bound)
POST /admin/step-up/issue
X-Admin-Key: <ADMIN_API_KEY>
{"action_class": "kill_switch_rearm", "resource_id": "GLOBAL"}

# STEP 2 of rearm — Re-arm (CLEARED → INACTIVE — trading resumes)
POST /admin/kill-switch/rearm
X-Admin-Key: <ADMIN_API_KEY>
{"scope": "GLOBAL", "scope_id": "GLOBAL", "step_up_token": "<TOKEN_ID>"}

# Cancel all open broker orders (PR #240, audit-emitting)
POST /admin/kill-switch/cancel-all
X-Admin-Key: <ADMIN_API_KEY>
{"reason": "panic stop — strategy XYZ mis-firing", "broker_account_id": null}

# Query current state
GET /admin/kill-switch/state
X-Admin-Key: <ADMIN_API_KEY>
```

All mutations are audited (`resource_type=kill_switch`) and persisted to Postgres immediately. Step-up token issuance and consumption are also audited (`resource_type=step_up_token`).

> **Important (§132):** `CLEARED` does **not** restore entry eligibility.
> After `confirm_clear` succeeds, the state is `CLEARED` but new entries are
> still blocked until the operator explicitly calls `rearm` to transition to
> `INACTIVE`. Failing to call `rearm` will leave the system in `CLEARED`
> state indefinitely — strategy signals will fire but orders will be blocked.
>
> **In LIVE mode, `rearm` requires a valid `kill_switch_rearm` step-up token
> (Architecture §15.4). Issue the token immediately before calling rearm — the
> TTL is 5 minutes and tokens are single-use.**

---

## Practical clear procedures

### Hub path — global kill switch via environment variable

If the kill switch was activated by setting `GLOBAL_KILL=1`:

1. Confirm the triggering condition has been resolved.
2. Update the deployment environment to remove or unset `GLOBAL_KILL`.
3. Restart the backend:
   ```powershell
   docker compose -f .\docker-compose.live.single.yml restart backend
   ```
4. Verify in logs that startup validation passes and no kill switch activation is logged.
5. Confirm fresh entries flow through the order router without `ORDER_REJECTED_GLOBAL_KILL` blocks.

### Legacy path — in-memory kill switch

The legacy `RiskManager.kill_switch_activated` is reset on restart because it is in-memory. PR #231 bridges legacy auto-trips into the durable manager, so restarting the backend will **not** clear a durable kill switch even if the legacy flag was the original trigger. Use the dashboard or HTTP API.

### Postgres-backed kill switch

The `kill_switch_state` table is created by migration `007_kill_switch_state.sql`, with migration `017` adding the `block_exits` column for SOFT/HARD persistence. Non-INACTIVE records survive restarts via `KillSwitchManager.load_state()`.

If the kill switch state is persisted to Postgres and a restart does not clear it:

1. Review active records:
   ```sql
   SELECT id, scope, scope_id, state, tripped_at, trip_reason, block_exits, updated_at
   FROM kill_switch_state
   WHERE state != 'INACTIVE'
   ORDER BY updated_at DESC;
   ```
2. Confirm the triggering condition is resolved (Step 1 of the SOP above).
3. Confirm broker-side flat (Step 2).
4. Verify no `RECONCILING` or `ORPHAN_REVIEW` positions exist for the affected scope.
5. Use the HTTP clear / rearm API or the dashboard. Do not manually update `kill_switch_state` for routine LIVE operation.
6. If the API is unavailable, hold the stack stopped and escalate to incident recovery. Any direct DB repair must be separately approved, captured as break-glass evidence, and followed by a restart plus `/readyz` validation.

---

## After clearing

- Verify new entry orders are accepted by the router (monitor logs for clean order flow).
- Confirm kill switch state is `INACTIVE` or absent from the audit log.
- Confirm position reconciliation is current and no scopes are in `RECONCILING` or `ORPHAN_REVIEW` before resuming automated live trading.
- Confirm `kill_switch_divergence_detected` is false in `/readyz` (PR #234).

## Rollback / recovery

If a clear or rearm was issued incorrectly, immediately trip the same scope again, capture the request/response/audit evidence, and keep automated entries blocked until the triggering condition and reconciliation state are reviewed. Note: after `rearm` returns the record to `INACTIVE`, a fresh `trip` is permitted (the trip-from-CLEARED guard does not apply to `INACTIVE`).

---

## Known incidents

### 2026-05-08 NATURALGAS22MAY26265CE — 23-minute legacy↔durable gap

**Summary.** A legacy `RiskManager` auto-trip on `total_dd > limit` was not propagated to the durable `KillSwitchManager`, so the hub-side router interceptor continued to accept orders for ~23 minutes. After a manual GLOBAL trip closed the entry path, three additional 3-lot SELLs / external fills still landed before broker-side flat was confirmed in the Angel One terminal. Total unrealised giveback was approximately ₹84,000.

**Timeline (UTC).**

| Time | Event |
|---|---|
| 13:07:02 | Legacy auto-trip: `total_dd > limit` triggered `RiskManager.kill_switch_activated=True` (in-memory only). |
| 13:07:02 – 13:30:26 | Hub-side `KillSwitchManager` remained `INACTIVE`; 23 minutes of trailing-lock and ema20 entry routing continued through the router. |
| 13:30:26 | Operator issued manual GLOBAL trip (durable). |
| 13:30:32 / 13:31:33 / 13:33:03 | Three additional 3-lot SELL fills / external fills landed after the manual trip — the dashboard "Cancel ALL Open Orders" flow did not yet exist, and BROKER_SYNC suppression made the internal `[]` position view misleading. |

**Root causes.**

1. Legacy `RiskManager` had no bridge into the durable `KillSwitchManager`.
2. There was no operator-facing surface to cancel all in-flight broker orders without shell access.
3. The trailing-lock evaluator did not check the durable manager.
4. The runbook did not warn operators that the internal empty-position view is unreliable while the kill switch is active and BROKER_SYNC is suppressed.

**Code-side fixes (in-flight / landed).**

- **PR #231 (Issue #218)** — bridges legacy `RiskManager` kill-switch trips into the durable `KillSwitchManager`, and gates trailing-lock evaluation on the durable state. Closes root cause 1 and 3.
- **PR #233 (Issue #220)** — adds SOFT/HARD trip via `block_exits`; HARD blocks exits at the router too. Closes the "exit engines kept firing" leg of root cause 3.
- **PR #234 (Issue #222)** — surfaces legacy↔durable divergence in `/readyz`. Adds an observability safety net for any remaining bridge gaps.
- **PR #240 (Issue #238)** — admin dashboard kill-switch panel with **Cancel ALL Open Orders**, SOFT/HARD trip, fail-closed LIVE persistence. Closes root cause 2.
- **PR #243 (Issue #221)** — `RISK_MAX_DAILY_LOSS` LIVE floor (default ₹5,000) and per-capital-tier sizing guidance. Prevents the original tiny-default auto-trip on noise.

**Operator-facing fix.** This runbook (the doc you are reading) — the new "Operator SOP" section above codifies the four-step verification flow so the next operator does not clear the kill switch based on Phoenix's empty position view alone.

---

## Known gaps

- Kill switch state is surfaced in `/readyz` (`kill_switch_active_count`, plus `kill_switch_divergence_detected` post-PR #234) and via `GET /admin/kill-switch/state` but **not** in the `/health` endpoint (which is a liveness probe only).
- A broker-UI direct close (Step 3 last-resort path) is not captured by Phoenix audit. Operators must paste the broker order id into the incident timeline manually so reconciliation has the record.
- Migration 017 must be applied for `block_exits` to survive a restart. On the pre-017 schema a HARD trip is held in memory only; the manager logs a warning at save_state time (`kill_switch_state.block_exits column missing`). See `app/risk/kill_switch.py` save_state path.

---

## Related

- [Dashboard Kill Switch](dashboard-kill-switch.md)
- [Break-Glass Flatten](break_glass_flatten.md)
- [OCI LIVE Deployment](oci_live_deployment.md) — including `RISK_MAX_DAILY_LOSS` sizing
- [Orphan Review](resolve_orphan_review.md)
- `ARCHITECTURE.md` §12, §12.1, §15.4
