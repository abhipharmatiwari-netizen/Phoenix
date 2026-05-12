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
- The **state-machine** transition back to `INACTIVE` happens only at `rearm`; `CLEARED` is a distinct terminal state until rearm fires. However, the **router-side entry block** is keyed on `KillSwitchManager.is_tripped()` which returns true only for `TRIPPED`/`CLEAR_PENDING` (`app/risk/kill_switch.py:467-476`), so once `confirm_clear` lands and the record is `CLEARED` the router stops blocking new entries even before rearm runs. Plan operator confirm-clear timing with this in mind (Codex #256 round-1 P1).
- In LIVE, **both** `confirm_clear` and `rearm` require single-use 5-minute step-up tokens (`action_class=kill_switch_clear` and `kill_switch_rearm` respectively; see §15.4 and `app/dashboard/admin_routes.py:1848-1864`, `app/dashboard/admin_routes.py:1923-1950`). Tokens are bound to actor + action class + resource id and are not interchangeable; PR #240 explicitly removed any dashboard auto-mint, so the operator must obtain each token via a separate `/admin/step-up/issue` call.
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

PR #231 (Issue #218) bridges legacy auto-trips into the durable manager. If they disagree, `/readyz` will surface `kill_switch_divergence=true` and `kill_switch_legacy_active=true` in the JSON payload, and fail readiness with `reason="kill_switch_divergence: legacy=True durable_global=False ..."` (PR #234 / Issue #222 — see `app/server.py:1346-1348` for the payload fields and `app/server.py:1397-1413` for the reason). **Do not** proceed to clear while divergence is reported — capture the evidence and re-run the bridge before touching the durable state.

### Step 2 — Verify broker-side flat directly in the broker terminal

Phoenix's internal `/positions` view is **not** trustworthy during a kill-switch-active window:

- `BROKER_SYNC` is intentionally suppressed while the kill switch is active, so the internal `StateStore` mirrors the last pre-trip snapshot rather than live broker state.
- External fills (from the broker mobile app, phone-in flatten, or in-flight orders that filled after the trip) will not be reflected until BROKER_SYNC is re-enabled.

Log directly into the Angel One terminal (web or mobile) and read the **broker-side** positions tab. That is the only authoritative view of what is actually open.

### Step 3 — If residual exposure exists, square off manually

If broker-side positions are non-zero, you have three options ordered from preferred to last-resort:

1. **Dashboard "Cancel ALL Open Orders"** (PR #240). Use this first to drain non-terminal child orders for every registered `AccountRunner`. It does not place exits; it only cancels working orders. See `dashboard-kill-switch.md` for the message-disambiguation semantics (`skipped` / `raced_filled` / `failed`).
2. **`POST /admin/break-glass/flatten`** (audited single-contract exit through the hub router at `BREAK_GLASS` mutation priority). Requires a `break_glass` step-up token in LIVE. See `break_glass_flatten.md` for the full request schema and required fields.

   > **HARD-trip caveat (Codex #256 round-1 P2).** `break-glass/flatten` submits an EXIT order through `OrderRouter.submit_order` (`app/dashboard/admin_routes.py:1017-1023`). The `GlobalKillSwitchInterceptor` rejects exit orders when any active matching kill-switch record has `block_exits=True` (`app/orders/interceptors.py:339-374`). **During a HARD trip, `break-glass/flatten` will therefore fail** with `kill_switch_manager_tripped_block_exits` and place no exit. Before invoking break-glass, either (a) downgrade the durable record to SOFT in-place via `KillSwitchManager.set_block_exits(block_exits=False, ...)` (audited as `kill_switch.set_block_exits`) so the interceptor admits the exit, or (b) skip directly to option 3 below (broker UI direct close).
3. **Broker UI direct close** — log into the Angel One terminal and square off positions there. This is the last-resort path; record the action in the incident log alongside the broker order id so the audit trail can be reconciled later.

Both the dashboard "Cancel ALL" and `break-glass/flatten` are audited (`resource_type=broker_orders` with `action=kill_switch_cancel_all` for cancel-all per `app/dashboard/admin_routes.py:2632-2636`; `action=break_glass_flatten` for flatten). A broker-UI direct close is **not** captured by Phoenix audit — the operator must paste the broker order id into the incident timeline.

### Step 4 — Only then follow the clear / rearm sequence

After (and only after) broker-side flat is confirmed and BROKER_SYNC has been re-enabled and the internal `StateStore` reflects the flat state:

1. `POST /admin/kill-switch/request-clear` (or the dashboard "Request Clear" button) — validation will refuse if `RECONCILING` / `ORPHAN_REVIEW` is still outstanding.
2. **In LIVE, FIRST obtain a `kill_switch_clear` step-up token** (Codex #256 round-1 P1; PR #240 round-2 review). `POST /admin/step-up/issue` with `action_class=kill_switch_clear`, `resource_id=GLOBAL` (or the specific scope id). Token TTL is 5 minutes, single-use, actor + action-class + resource bound.
3. `POST /admin/kill-switch/confirm-clear` with the `step_up_token` — moves to `CLEARED`. **Entries become eligible at this transition** in LIVE: `KillSwitchManager.is_tripped()` (`app/risk/kill_switch.py:467-476`) returns False once the record is `CLEARED`, so the router interceptor will admit new entries even before the separate rearm step. The dashboard pill turning blue `CLEARED` is therefore the point of restored entry flow. The LIVE step-up gate on confirm-clear (`app/dashboard/admin_routes.py:1848-1864`) is the secondary credential ceremony that protects this transition; it is **not** deferrable to rearm.
4. Obtain a separate `kill_switch_rearm` step-up token in LIVE (`POST /admin/step-up/issue` with `action_class=kill_switch_rearm`, `resource_id=GLOBAL` for the global scope). Tokens are not interchangeable across action classes.
5. `POST /admin/kill-switch/rearm` with the rearm token — moves `CLEARED → INACTIVE`. This step is required for state-machine hygiene (subsequent trips must originate from `INACTIVE`) and removes the `CLEARED` record from the dashboard; it does **not** flip entry eligibility a second time (that already happened at confirm-clear).

> **Earlier wording warning.** Prior versions of this runbook said entries are restored "only at rearm" — that is inaccurate for LIVE. The §132 callout below preserves the older language as a state-machine invariant (CLEARED is not INACTIVE) but the **router-side** entry block is keyed on `is_tripped() == TRIPPED|CLEAR_PENDING`, so confirm-clear is the operational restore point. Plan the confirm-clear timing accordingly — do not issue confirm-clear and then walk away.

Do **not** issue clear/rearm based on Phoenix's empty position view alone — see Step 2.

---

## Automated exit engines during kill-switch active

This subsection documents the status-quo behaviour of the automated exit engines (trailing-lock, profit-lock, EOD exit) when the kill switch is tripped. Issue #220 / PR #233 introduced the SOFT/HARD distinction below; PR #231 (Issue #218) gates trailing-lock on the durable `KillSwitchManager` so a legacy auto-trip is correctly observed by the hub-side exits.

| Trip mode | New entries | Strategy-driven exit orders (router) | Automated exit engines (trailing-lock, profit-lock, EOD) |
|---|---|---|---|
| **SOFT** (default) | Blocked at router | Allowed at the router (still pass risk-reducing gate) | **Trailing-lock skips exit submission entirely** — see warning below. Other engines that bypass the engine-side gate may still flow to the router. |
| **HARD** (`block_exits=True`) | Blocked at router | Blocked at router (`is_tripped_for_scope_with_block_exits → block_exits=True`) | Blocked. Operator must flatten manually in the broker UI. |

**Operator warning (Codex #256 round-1 P2 correction).** For any durable trip — **SOFT or HARD** — `PositionTrailingLockEngine` calls `KillSwitchManager.is_tripped_for_scope()` and **skips exit submission for tripped scopes** regardless of `block_exits` (`app/hub/exit_engines.py:2308-2358`). This is intentional: while the kill switch is durable-tripped, BROKER_SYNC is suppressed and internal position state is stale, so submitting trailing-lock exits against it produced the 2026-05-08 duplicate-fill incident. Therefore:

- **Do not rely on trailing-lock to flatten exposure during a SOFT trip.** It will not fire. The operator must flatten manually — either via dashboard "Cancel ALL Open Orders" plus broker UI close, or by clearing the kill switch (Step 4 SOP) once broker-side flat is confirmed.
- HARD vs SOFT chiefly affects strategy-driven and break-glass exits at the router. Trailing-lock is gated at the **engine** side regardless.
- If you want to allow the trailing-lock engine to drain exposure automatically again, the durable record must transition back to `CLEARED` (entries also unblocked) — there is no "let trailing-lock keep exiting" mode that leaves entries blocked.

**Auto-trip path (Codex #256 round-1 P2 correction).** Daily-loss / drawdown auto-trips from `RiskManager` always trip the durable manager at `GLOBAL` scope: the bridge calls `ksm.trip(KillSwitchScope.GLOBAL, "GLOBAL", ...)` unconditionally (`app/core/risk_manager.py:572-576`). There is **no** account or strategy-scoped auto-trip on this path; do not look for an account-scoped record during an auto-trip incident — clear and rearm the `GLOBAL` record. Auto-trips use SOFT (`block_exits=False`) by default — the policy intent is "stop new entry exposure", but per the trailing-lock semantics above, automated trailing-lock exits will also be suppressed for the duration of the trip. To escalate an auto-trip from SOFT to HARD in-place (without a clear/rearm cycle), call `KillSwitchManager.set_block_exits` or use the corresponding dashboard control; the action is audited as `kill_switch.set_block_exits`.

---

## How the kill switch is tripped

### Automatic — daily loss threshold

When realized plus unrealized PnL crosses `-abs(RISK_MAX_DAILY_LOSS)`, the system automatically activates the durable kill switch (Codex #256 round-1 P2 correction). **The auto-trip is always GLOBAL** — the bridge in `app/core/risk_manager.py:572-576` calls `ksm.trip(KillSwitchScope.GLOBAL, "GLOBAL", ...)` unconditionally, irrespective of which account / strategy crossed the threshold. There is no per-account or per-strategy durable record produced on this path. Operators clearing a daily-loss auto-trip must therefore look up and clear the `GLOBAL` record (one durable trip blocks the whole hub), not a per-account scope.

See [OCI LIVE Deployment — Sizing the daily-loss limit by capital tier](oci_live_deployment.md#sizing-the-daily-loss-limit-by-capital-tier) for the LIVE floor (default ₹5,000) and per-tier guidance.

Auto-trips are SOFT by default (see Automated exit engines above for the trailing-lock interaction).

### Manual — dashboard (preferred)

Use the Safety page's **Global Kill Switch** panel (PR #240). Tick **HARD trip** when you also need exits blocked. Reason is required and is persisted to the audit log. See `dashboard-kill-switch.md` for the full playbook.

### Manual — HTTP API

Direct API calls remain supported for cases where the dashboard is unavailable. See the HTTP API section below.

### Manual — global kill switch via environment

The env-var fallback is a coarse, restart-coupled path; prefer the dashboard or the durable API. To actually take effect at the router, **both** of the following must be set (Codex #256 round-1 P1 correction):

1. `ORDER_ROUTER_ENFORCE_GLOBAL_KILL_SWITCH=true` (or `1`) — gates the entire interceptor. `GlobalKillSwitchInterceptor.evaluate()` returns immediately without checking either the env var or the durable manager when `order_router_enforce_global_kill_switch` is false (`app/orders/interceptors.py:292-300`), and the setting defaults to **false** (`app/config/settings.py:279-282`).
2. `GLOBAL_KILL=1` (or `true`) — the env-var trip that the interceptor consults once the enforcement gate above is on.

Setting `GLOBAL_KILL=1` alone does **nothing** in the router. After updating both env vars, restart the backend so the settings are reloaded. To also block exits via the env path, additionally set `GLOBAL_KILL_BLOCK_EXITS=1`; this is the HARD-trip equivalent for the env fallback (`app/orders/interceptors.py:297-325`). All three env vars are evaluated per-request on the hot path; the restart is only required because `order_router_enforce_global_kill_switch` is loaded from `Settings` at startup.

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

`/readyz` exposes the following kill-switch fields in its JSON payload (Codex #256 round-1 P2 correction — earlier versions named the divergence field incorrectly):

- `kill_switch_active_count` (int) — count of non-INACTIVE durable records (`app/server.py:1374-1389`).
- `kill_switch_source` (str) — provenance of the count (`durable` / `unavailable`).
- `kill_switch_divergence` (bool) — true when legacy `RiskManager.kill_switch_activated` and durable `KillSwitchManager` disagree (`app/server.py:1346-1348`). **The field is `kill_switch_divergence`, not `kill_switch_divergence_detected`** — monitors must watch the former.
- `kill_switch_legacy_active` (bool) — value of the legacy in-memory flag at the time the readyz snapshot was taken.

When divergence is detected and `KILL_SWITCH_DIVERGENCE_FAILS_READY` is truthy (default true), `/readyz` returns HTTP 503 with `reason` of the form `kill_switch_divergence: legacy=True durable_global=False age_s=<seconds>` (`app/server.py:1397-1413`). Monitors should alert on either (a) `ready=false` plus `reason` starting with `kill_switch_divergence:`, or (b) `kill_switch_divergence=true` in the payload regardless of readiness gating.

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

`resource_type=kill_switch` covers `trip` / `request_clear` / `confirm_clear` / `rearm` / `set_block_exits`. **Cancel-all is a separate `resource_type` (Codex #256 round-1 P3 correction):** `/admin/kill-switch/cancel-all` emits `action=kill_switch_cancel_all` with `resource_type=broker_orders` (`app/dashboard/admin_routes.py:2632-2636`). Break-glass flatten emits `action=break_glass_flatten` with `resource_type=break_glass`. To reconstruct an end-to-end incident timeline, query all three:

```http
GET /admin/audit?resource_type=kill_switch
GET /admin/audit?resource_type=broker_orders
GET /admin/audit?resource_type=break_glass
```

The dashboard's Safety page already merges these three resource types into a single table for the common operator view.

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

# STEP 1 of confirm-clear — issue a kill_switch_clear step-up token (LIVE only)
# Codex #256 round-1 P1: confirm-clear is the transition that restores router
# entry eligibility, so it ALSO requires a dedicated step-up token (not just rearm).
POST /admin/step-up/issue
X-Admin-Key: <ADMIN_API_KEY>
{"action_class": "kill_switch_clear", "resource_id": "GLOBAL"}

# STEP 2 of confirm-clear — Confirm the clear (CLEAR_PENDING → CLEARED — entries restored at the router)
POST /admin/kill-switch/confirm-clear
X-Admin-Key: <ADMIN_API_KEY>
{"scope": "GLOBAL", "scope_id": "GLOBAL", "step_up_token": "<CLEAR_TOKEN_ID>"}

# STEP 1 of rearm — issue a separate kill_switch_rearm step-up token (LIVE only, 5-minute TTL, single-use, actor-bound)
POST /admin/step-up/issue
X-Admin-Key: <ADMIN_API_KEY>
{"action_class": "kill_switch_rearm", "resource_id": "GLOBAL"}

# STEP 2 of rearm — Re-arm (CLEARED → INACTIVE — state-machine hygiene; router already unblocked at confirm-clear)
POST /admin/kill-switch/rearm
X-Admin-Key: <ADMIN_API_KEY>
{"scope": "GLOBAL", "scope_id": "GLOBAL", "step_up_token": "<REARM_TOKEN_ID>"}

# Cancel all open broker orders (PR #240, audit-emitting)
POST /admin/kill-switch/cancel-all
X-Admin-Key: <ADMIN_API_KEY>
{"reason": "panic stop — strategy XYZ mis-firing", "broker_account_id": null}

# Query current state
GET /admin/kill-switch/state
X-Admin-Key: <ADMIN_API_KEY>
```

Most mutations in this API block are audited under `resource_type=kill_switch` and persisted to Postgres immediately. The exception (Codex #256 round-1 P3 correction): `/admin/kill-switch/cancel-all` emits `action=kill_switch_cancel_all` with `resource_type=broker_orders` (`app/dashboard/admin_routes.py:2632-2636`) because the cancelled artefacts are broker orders, not kill-switch state. Step-up token issuance and consumption are also audited (`resource_type=step_up_token`). When auditing a clear/rearm incident, query both `resource_type=kill_switch` and `resource_type=broker_orders` (plus `resource_type=break_glass` if `break-glass/flatten` was used) to get the full picture.

> **Important (§132 — corrected per Codex #256 round-1 P1).** The state-machine transition `CLEARED → INACTIVE` happens at `rearm`, but the **router-side entry block** is keyed on `KillSwitchManager.is_tripped()`, which only returns true for `TRIPPED` or `CLEAR_PENDING` (`app/risk/kill_switch.py:467-476`). Once `confirm_clear` lands and the record becomes `CLEARED`, the hub interceptor stops rejecting new entries — entry eligibility is restored at **confirm-clear**, not at rearm. Plan the confirm-clear timing accordingly.
>
> `rearm` is still required (§132 invariant) to return the durable record to `INACTIVE` so the next trip ceremony starts cleanly; until then the record stays `CLEARED` and the dashboard surfaces the post-clear pill, but the router no longer blocks.
>
> **In LIVE mode both `confirm-clear` and `rearm` require their own step-up tokens** (Architecture §15.4). `confirm-clear` consumes a `kill_switch_clear` token bound to the scope id (`app/dashboard/admin_routes.py:1848-1864`); `rearm` consumes a separate `kill_switch_rearm` token. Tokens are 5-minute TTL, single-use, actor-bound, and not interchangeable across action classes. Issue each one immediately before the call that consumes it.

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
- Confirm `kill_switch_divergence` is false and `kill_switch_legacy_active` is false in `/readyz` (PR #234; field names corrected per Codex #256 round-1 P2).

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

- Kill switch state is surfaced in `/readyz` (`kill_switch_active_count`, plus `kill_switch_divergence` / `kill_switch_legacy_active` post-PR #234) and via `GET /admin/kill-switch/state` but **not** in the `/health` endpoint (which is a liveness probe only).
- A broker-UI direct close (Step 3 last-resort path) is not captured by Phoenix audit. Operators must paste the broker order id into the incident timeline manually so reconciliation has the record.
- Migration 017 must be applied for `block_exits` to survive a restart. On the pre-017 schema a HARD trip is held in memory only; the manager logs a warning at save_state time (`kill_switch_state.block_exits column missing`). See `app/risk/kill_switch.py` save_state path.

---

## Related

- [Dashboard Kill Switch](dashboard-kill-switch.md)
- [Break-Glass Flatten](break_glass_flatten.md)
- [OCI LIVE Deployment](oci_live_deployment.md) — including `RISK_MAX_DAILY_LOSS` sizing
- [Orphan Review](resolve_orphan_review.md)
- `ARCHITECTURE.md` §12, §12.1, §15.4
