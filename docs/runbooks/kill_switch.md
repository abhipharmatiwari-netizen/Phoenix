# Kill Switch — Operational Reference

**Architecture reference:** §12, §12.1, P0 rules

**Related runbooks:**
- [Dashboard Kill Switch](dashboard-kill-switch.md) — admin UI on the Safety page (PR #240)
- [Break-Glass Flatten](break_glass_flatten.md) — audited single-contract exit
- [OCI LIVE Deployment](oci_live_deployment.md)
- [Capital Limits Configuration](capital_limits_configuration.md)

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

- The hub-path `GlobalKillSwitchInterceptor` is gated by a single feature flag (`order_router_enforce_global_kill_switch`, env `ORDER_ROUTER_ENFORCE_GLOBAL_KILL_SWITCH`). **The setting defaults to `false`**; when false, `GlobalKillSwitchInterceptor.evaluate()` returns immediately and **neither the `GLOBAL_KILL` env var nor the durable `KillSwitchManager` is consulted** (`app/orders/interceptors.py:292-295`, `app/config/settings.py:279-282`). With the flag set to `true`, the interceptor first checks the `GLOBAL_KILL` env var (HARD-blockable via `GLOBAL_KILL_BLOCK_EXITS`) and then the durable `KillSwitchManager` for any matching scope (`app/orders/interceptors.py:297-374`).
- **LIVE pre-flight requirement (Codex #256 round-2 P1 / round-3 P2 correction).** `ORDER_ROUTER_ENFORCE_GLOBAL_KILL_SWITCH=true` must be set in the deployment env and the backend restarted before going LIVE. The bundled LIVE Compose manifests set it explicitly, and LIVE startup validation now rejects a missing/false value. Without it, **all** kill-switch trips — durable, env-var, manual, and auto — are silently inert at the router. There is **no** `/admin/runtime-settings` HTTP endpoint (earlier runbook wording was wrong — `get_runtime_settings` in `app/config/runtime_config.py` is an in-process helper, not a route). The pre-LIVE checklist must therefore confirm the flag is on via one of these working alternatives:
    1. **Container/process env inspection** (most reliable, works without auth):
       ```powershell
       docker compose -f .\docker-compose.live.single.yml exec backend printenv ORDER_ROUTER_ENFORCE_GLOBAL_KILL_SWITCH
       ```
       Must print `true` (or `1`). An empty result means the flag is off and the interceptor will short-circuit.
    2. **Behavioural verification** — trip a SOFT kill switch at GLOBAL scope **via the durable manager** (dashboard or `POST /admin/kill-switch/trip`), attempt a paper-mode test order, confirm the router emits `ORDER_REJECTED_KILL_SWITCH_MANAGER` in the backend logs (`app/orders/interceptors.py:355-361`), then clear and rearm. **Do not** look for `ORDER_REJECTED_GLOBAL_KILL` — that event name is emitted only by the env-var path (`GLOBAL_KILL=1`, `app/orders/interceptors.py:308-325`); a correctly-enforced durable trip will not produce it, so monitors filtering on `ORDER_REJECTED_GLOBAL_KILL` will conclude the interceptor is disabled when the durable path is in fact working. If the test order is accepted instead, the interceptor is disabled regardless of what the env claims. (Codex #256 round-4 P2 correction — earlier preflight wording named the env-var event for a durable-manager drill.)

    The dashboard kill-switch panel does **not** warn you when the underlying interceptor is disabled, so this pre-flight step is non-negotiable for LIVE.
- The legacy stream path uses `RiskManager.kill_switch_activated`. PR #231 (Issue #218) bridges legacy auto-trips into the durable `KillSwitchManager` so the hub interceptor sees them (subject to the enforcement gate above); PR #234 (Issue #222) surfaces any remaining legacy↔durable divergence in `/readyz`.
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

Use this checklist for every trip. Steps 1–3 are verification; only after they pass do you progress to clear/rearm.

**Auto-trip vs manual-trip scope of Step 1 (Codex #256 round-2 P2 + round-4 P2 reinforcement).** Step 1 is composed of two distinct checks: (a) the legacy↔durable **divergence** boolean and (b) the standalone `kill_switch_legacy_active` flag. Divergence is defined as `legacy_active=True AND durable_global_active=False` (`app/hub/runtime.py:1029`); a manual operator trip via the dashboard or `POST /admin/kill-switch/trip` only calls `KillSwitchManager.trip(...)` (`app/dashboard/admin_routes.py:1660-1666`) and does **not** mutate `RiskManager.kill_switch_activated`. So a manual trip on a clean system starts with `legacy=False, durable=True` — divergence is False by definition. **However**, when a manual durable trip is laid on top of an already-active legacy `RiskManager` flag (e.g. a stream-path auto-trip the operator did not realise was active), the durable=True term *masks* divergence: the operator sees `kill_switch_divergence=false` while `kill_switch_legacy_active=true` remains in the `/readyz` payload (`app/server.py:1346-1348`). If the operator then confirm-clears the durable record without inspecting `kill_switch_legacy_active`, the durable mask is removed, divergence reappears, **and** entries become eligible at the router while the legacy halt is still active — re-creating exactly the 2026-05-08 incident shape from the other direction. Therefore on **every** trip — auto **and** manual — Step 1 must check **both** `kill_switch_divergence` AND `kill_switch_legacy_active` in the `/readyz` payload before progressing to clear. If `kill_switch_legacy_active=true` you have a pre-existing legacy auto-trip to investigate and re-bridge **regardless** of what the divergence boolean reports.

### Step 1 — Confirm both legacy and durable kill-switch state agree (every trip)

Two stores represent kill-switch state and they can disagree:

| Store | Source of truth for | How to read |
|---|---|---|
| Durable `KillSwitchManager` (Postgres `kill_switch_state` table) | Hub interceptor decisions in LIVE | `GET /admin/kill-switch/state` — admin auth required |
| Legacy `RiskManager.kill_switch_activated` (in-memory + `risk_positions.json`) | Stream-path legacy exits | Search backend logs for `kill_switch_activated`, or inspect `risk_positions.json` |

PR #231 (Issue #218) bridges legacy auto-trips into the durable manager. If the bridge fails (auto-trip on the stream path but durable manager still INACTIVE), `/readyz` will surface `kill_switch_divergence=true` and `kill_switch_legacy_active=true` in the JSON payload, and fail readiness with `reason="kill_switch_divergence: legacy=True durable_global=False ..."` (PR #234 / Issue #222 — see `app/server.py:1346-1348` for the payload fields and `app/server.py:1397-1413` for the reason). **Do not** proceed to clear while either `kill_switch_divergence=true` **or** `kill_switch_legacy_active=true` is reported. Per the round-4 P2 correction above, `kill_switch_legacy_active=true` with `kill_switch_divergence=false` is the masked-divergence shape that appears when a manual durable trip is laid on top of a pre-existing legacy auto-trip — clearing the durable record alone will re-expose the legacy halt and reopen entries against it. Capture the evidence and re-run the legacy bridge before touching the durable state.

### Step 2 — Verify broker-side flat directly in the broker terminal

**Codex #256 round-2 P2 correction.** Earlier wording of this step claimed Phoenix's `/positions` view is stale during a kill-switch-active window because `BROKER_SYNC` is suppressed. **That is inaccurate for the hub-authoritative `/positions` endpoint:** `app/server.py:354+` reads from `runtime.state_store.get_positions(...)`, and `AccountRunner._sync_positions` writes the latest broker positions into `StateStore` on every successful fetch unconditionally (`app/hub/account_runner.py:419-423`) — there is no kill-switch gate around the StateStore write path. The kill-switch suppression in `app/core/position_sync.py:792-806` only suppresses **registering broker positions into the legacy `RiskManager`** (the `_register_broker_only_position` branch at `app/core/position_sync.py:857+`), not the StateStore update that backs `/positions`.

So `/positions` will continue to reflect broker reality at the cadence of `AccountRunner._sync_positions`. The reason to still log into the broker terminal directly is different:

- **AccountRunner polls broker positions on a tick interval** (`_positions_interval`), so there is a normal small lag between a broker-side fill and `/positions` reflecting it; during incident response this lag matters.
- **Broker-side fills that occur via the mobile app, phone-in flatten, or in-flight orders that fill after the trip** become visible at the next poll, not instantly. While the trip is fresh, treat the broker terminal as the lower-latency source of truth.
- A broker-side close also gives you the broker order id for the incident timeline (Phoenix audit captures only its own actions).

Log into the Angel One terminal (web or mobile) and read the broker-side positions tab as the authoritative, lowest-latency view. Phoenix's `/positions` remains a valid corroborating view — it is **not** disabled by a durable trip.

### Step 3 — Cancel ALL open broker orders, then (if positions non-zero) square off

This step has two distinct sub-actions. **Read both carefully — they are NOT interchangeable.**

#### 3a — Cancel ALL open broker orders (MANDATORY, regardless of position state)

**Codex #256 round-3 P1 correction.** This sub-step is **always required** before progressing to Step 4 (request-clear). Do **not** condition it on broker-side positions being non-zero — working / pending broker orders can exist even when current positions are flat (e.g. partially-filled exits whose remaining quantity is still resting on the book, stale stop-loss orders that never triggered, or in-flight entries that were placed seconds before the trip). Cancel-all is the **only** step that drains those broker orders, and Step 4 (`request-clear` → `confirm-clear`) restores router entry eligibility — if open broker orders survive into the cleared window they can still fill against fresh entries placed by automated strategies.

Execute the dashboard **"Cancel ALL Open Orders"** action (PR #240) — or `POST /admin/kill-switch/cancel-all` directly — even when `/positions` and the broker terminal both show flat. The endpoint is idempotent on the broker side and will silently no-op accounts that genuinely have nothing to cancel; it does not place exits, it only cancels working orders. See `dashboard-kill-switch.md` for the per-account message disambiguation (`skipped` / `raced_filled` / `failed`).

> **Precondition (Codex #256 round-3 P2 correction).** `/admin/kill-switch/cancel-all` enforces a hierarchical trip-before-cancel guard: it returns **HTTP 409** unless the durable `KillSwitchManager` has a record in `TRIPPED` or `CLEAR_PENDING` state in the `GLOBAL → TENANT → ACCOUNT` hierarchy covering the target scope (`app/dashboard/admin_routes.py:2041-2149`). **The env-only `GLOBAL_KILL=1` trip does NOT satisfy this precondition** — env-var trips bypass the durable manager entirely (see "Manual — global kill switch via environment" below). If the original trip path was env-var-only, you must either (a) issue a durable `POST /admin/kill-switch/trip` first so cancel-all is permitted, or (b) cancel broker orders directly in the Angel One terminal (last-resort path; not audited by Phoenix).

#### 3b — If broker-side positions are non-zero, square off manually

After Step 3a has drained working orders, inspect broker-side positions (Step 2 already established the broker terminal as the lowest-latency view). If positions are flat, skip to Step 4. If they are non-zero, you have two options:

1. **`POST /admin/break-glass/flatten`** (audited single-contract exit through the hub router at `BREAK_GLASS` mutation priority). Requires a `break_glass` step-up token in LIVE. See `break_glass_flatten.md` for the full request schema and required fields.

   > **HARD-trip caveat — break-glass flatten DOES NOT WORK during a HARD trip (Codex #256 round-1 P2 / round-2 P2 reinforcement).** `break-glass/flatten` submits an EXIT order through `OrderRouter.submit_order` (`app/dashboard/admin_routes.py:1017-1023`), and `GlobalKillSwitchInterceptor` **rejects** that exit when any active matching kill-switch record has `block_exits=True` (`app/orders/interceptors.py:339-374`). The interceptor returns `kill_switch_manager_tripped_block_exits` and **no exit order reaches the broker**. **Do not** attempt break-glass flatten as a recovery path while a HARD trip is in force. Your only working paths during a HARD trip are: (a) **temporarily downgrade the durable record from HARD to SOFT in-place** before invoking break-glass — this preserves the entry block while admitting the break-glass exit at the interceptor (see "HARD → SOFT downgrade — actual paths" below for the exact API/SQL); or (b) **use the broker UI direct close (option 2 below)** — the recommended last-resort path that always works regardless of the durable trip mode.
2. **Broker UI direct close** — log into the Angel One terminal and square off positions there. This is the last-resort path; record the action in the incident log alongside the broker order id so the audit trail can be reconciled later.

Both the dashboard "Cancel ALL" and `break-glass/flatten` are audited (cancel-all emits `action=kill_switch_cancel_all` with `resource_type=broker_orders` per `app/dashboard/admin_routes.py:2632-2636`; **break-glass flatten emits `action=break_glass_flatten` with `resource_type=position`** per `app/dashboard/admin_routes.py:1114-1118` — **not** `resource_type=break_glass` as earlier wording suggested, Codex #256 round-2 P2 correction). A broker-UI direct close is **not** captured by Phoenix audit — the operator must paste the broker order id into the incident timeline.

#### HARD → SOFT downgrade — actual paths (Codex #256 round-3 P2 correction)

Earlier wording said operators could downgrade an existing HARD trip back to SOFT via the dashboard `block_exits` toggle. **That control does not exist** — the Safety panel only renders **"Upgrade SOFT → HARD"** and only when `!globalRecord?.block_exits` (`frontend/src/components/KillSwitchPanel.tsx:521-528`). There is no dashboard "Downgrade HARD → SOFT" button. The two actually-supported paths are:

1. **HTTP API re-trip (preferred, audited).** `POST /admin/kill-switch/trip` with `block_exits=false` against the same scope+scope_id. When the durable record is already `TRIPPED` or `CLEAR_PENDING` with a different `block_exits` value, the endpoint detects the in-place upgrade/downgrade case and routes through `KillSwitchManager.set_block_exits` rather than rejecting with 409 (`app/dashboard/admin_routes.py:1667-1703`). The action is audited as `kill_switch_block_exits_upgraded` and the response carries `upgraded_in_place=true`. In LIVE this still requires ADMIN role and a non-empty `reason`.
2. **Direct Postgres UPDATE (break-glass, manual audit only).** When the API is unavailable, the downgrade SQL must match the **actual scope** of the active HARD record — the runbook supports `GLOBAL`, `TENANT`, `ACCOUNT`, and `STRATEGY` scopes (`app/risk/kill_switch.py` `KillSwitchScope`), and a literal `WHERE scope='GLOBAL' AND scope_id='GLOBAL'` will update zero rows when the active HARD trip is account- or tenant-scoped (Codex #256 round-4 P2 correction). First confirm the active record:

   ```sql
   -- Identify the active HARD trip(s) to downgrade
   SELECT id, scope, scope_id, state, block_exits, trip_reason, updated_at
   FROM kill_switch_state
   WHERE block_exits = true
     AND state IN ('TRIPPED','CLEAR_PENDING')
   ORDER BY updated_at DESC;
   ```

   Then run a parameterised UPDATE against the specific `scope` / `scope_id` pair you observed. The placeholders below use `psql`'s `\set` form for clarity; substitute the matching driver syntax (`%(scope)s` for psycopg, `$1`/`$2` for asyncpg, `?` for sqlite shells) as appropriate:

   ```sql
   -- psql example. Replace :scope / :scope_id with the values from the SELECT above
   \set scope     'ACCOUNT'
   \set scope_id  'PRIMARY'
   UPDATE kill_switch_state
   SET block_exits = false,
       updated_at  = now()
   WHERE scope    = :'scope'
     AND scope_id = :'scope_id'
     AND state IN ('TRIPPED','CLEAR_PENDING');
   ```

   For a `GLOBAL` downgrade the invocation is `\set scope 'GLOBAL'` / `\set scope_id 'GLOBAL'`; for a tenant-scoped trip use `\set scope 'TENANT'` / `\set scope_id '<tenant_id>'`. Verify `UPDATE 1` (or `UPDATE N` matching the number of active records you intended to downgrade) — `UPDATE 0` means the predicate did not match and the HARD trip is still in force. Follow with a backend restart so `KillSwitchManager.load_state()` picks up the change. This is **not** audited by Phoenix; capture the SELECT + UPDATE evidence in the incident log.

### Step 4 — Only then follow the clear / rearm sequence

After (and only after) broker-side flat is confirmed and the next `AccountRunner._sync_positions` tick has refreshed `StateStore` so `/positions` reflects the flat state (recall from Step 2 that the StateStore write is **not** gated by the kill switch — there is no separate "re-enable BROKER_SYNC" step in the durable flow):

1. `POST /admin/kill-switch/request-clear` (or the dashboard "Request Clear" button) — validation will refuse if any order-lifecycle record for an `AccountRunner` is in `RECONCILING` or `MANUAL_REVIEW` state (`app/dashboard/admin_routes.py:1769-1787`). **Note (Codex #256 round-2 P2 + round-4 P3 + round-5 P2 corrections).** Earlier runbook wording said `request-clear` rejects `ORPHAN_REVIEW` ownership records — that is **not** what the endpoint does. The validator only walks `OrderLifecycle._position_records` and checks `position_state in {RECONCILING, MANUAL_REVIEW}`. The position-ownership store's `ORPHAN_REVIEW` state is **not** consulted by this validator. There is also **no** `GET /admin/positions/ownership` route — earlier wording referencing it was wrong (repo-wide search finds only `POST /admin/resolve-orphan-review` and `POST /state/clear-position-record` in `app/dashboard/admin_routes.py`; no ownership listing route exists).

   **Ownership-state `ORPHAN_REVIEW` is in-memory only.** Round-4 wording proposed a `SELECT ... FROM position_ownership_ledger WHERE state='ORPHAN_REVIEW'` query — **that SQL is invalid** (Codex #256 round-5 P2 correction). Migration `008_position_ownership_ledger.sql` defines `position_ownership_ledger` as a strategy/net-quantity ledger with no `ownership_key`, `state`, or `evidence` columns; the table only persists `(tenant_id, broker_account_id, contract_key_tuple, strategy_id, authority_path, net_qty, updated_at)`. The runtime `OwnershipRecord` (which carries the `ORPHAN_REVIEW` state) lives in `PositionOwnershipStore._ownership_records` (`app/orders/position_ownership.py:937-940`) and is **not** mirrored to Postgres. Use one of the two working enumeration paths instead:

   **(a) Order-lifecycle proxy via `internal_position_records` (migration 009).** When the reconciliation watcher escalates a scope to `ORPHAN_REVIEW`, the matching `OrderLifecycle._position_records` entry is updated to `position_state='MANUAL_REVIEW'` (`app/core/position_sync.py:1070-1075`) and persisted into `internal_position_records.position_state`. This is the **same** state the `request-clear` validator inspects, so querying it is the operationally-meaningful pre-clear check:

   ```sql
   SELECT scope_key, tenant_id, account_id, strategy_id, ownership_key,
          position_state, state_reason, updated_at
   FROM internal_position_records
   WHERE position_state IN ('RECONCILING', 'MANUAL_REVIEW')
   ORDER BY updated_at DESC;
   ```

   Any rows returned will cause `request-clear` to fail with HTTP 409.

   **(b) Backend log search for the watcher escalation event.** `ReconciliationTimeoutWatcher.check_and_escalate` emits a `WARNING` log line `reconciliation_timeout_watcher: escalating scope <key> to ORPHAN_REVIEW ...` (`app/core/reconciliation_timeout_watcher.py:162-166`) at the moment of escalation. Tail the backend logs across the incident window:

   ```powershell
   docker compose -f .\docker-compose.live.single.yml logs --since 24h backend | Select-String "escalating scope.*ORPHAN_REVIEW"
   ```

   Each match identifies the `scope_key` of an active ORPHAN_REVIEW; resolve via `POST /admin/resolve-orphan-review` — see `resolve_orphan_review.md` for the request schema and decision codes. If neither query nor log search returns anything, no ORPHAN_REVIEW work is pending.
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

**Operator warning (Codex #256 round-1 P2 + round-4 P2 corrections).** For any durable trip — **SOFT or HARD** — `PositionTrailingLockEngine` calls `KillSwitchManager.is_tripped_for_scope()` and **skips exit submission for tripped scopes** regardless of `block_exits` (`app/hub/exit_engines.py:2308-2358`). This is intentional defence-in-depth: trailing-lock submissions during the 2026-05-08 NATURALGAS22MAY26265CE window produced the duplicate-broker-fill sequence (broker_order_ids 842740 + 842946 ~60s apart), so the engine now suppresses **its own** exit submissions for the duration of any durable trip. **This skip is NOT because StateStore is stale** — Step 2 above documents that `runtime.state_store.get_positions(...)` continues to refresh on every `AccountRunner._sync_positions` tick regardless of the kill switch (`app/hub/account_runner.py:419-423`), and `PositionTrailingLockEngine` reads from the **same** `self.state_store.get_positions(...)` source (`app/hub/exit_engines.py:2315`). The trailing-lock skip is a defence against runaway re-submission during incident response, not a stale-data fix. Therefore:

- **Do not rely on trailing-lock to flatten exposure during a SOFT trip.** It will not fire. The operator must flatten manually — either via dashboard "Cancel ALL Open Orders" plus broker UI close, or by clearing the kill switch (Step 4 SOP) once broker-side flat is confirmed.
- HARD vs SOFT chiefly affects strategy-driven and break-glass exits at the router. Trailing-lock is gated at the **engine** side regardless.
- If you want to allow the trailing-lock engine to drain exposure automatically again, the durable record must transition back to `CLEARED` (entries also unblocked) — there is no "let trailing-lock keep exiting" mode that leaves entries blocked.

**Auto-trip path (Codex #256 round-1 P2 correction).** Daily-loss / drawdown auto-trips from `RiskManager` always trip the durable manager at `GLOBAL` scope: the bridge calls `ksm.trip(KillSwitchScope.GLOBAL, "GLOBAL", ...)` unconditionally (`app/core/risk_manager.py:572-576`). There is **no** account or strategy-scoped auto-trip on this path; do not look for an account-scoped record during an auto-trip incident — clear and rearm the `GLOBAL` record. Auto-trips use SOFT (`block_exits=False`) by default — the policy intent is "stop new entry exposure", but per the trailing-lock semantics above, automated trailing-lock exits will also be suppressed for the duration of the trip. To escalate an auto-trip from SOFT to HARD in-place (without a clear/rearm cycle), call `KillSwitchManager.set_block_exits` or use the corresponding dashboard control; the action is audited as `kill_switch.set_block_exits`.

---

## How the kill switch is tripped

### Automatic — daily loss threshold

When realized plus unrealized PnL crosses `-abs(RISK_MAX_DAILY_LOSS)`, the system automatically activates the durable kill switch (Codex #256 round-1 P2 correction). **The auto-trip is always GLOBAL** — the bridge in `app/core/risk_manager.py:572-576` calls `ksm.trip(KillSwitchScope.GLOBAL, "GLOBAL", ...)` unconditionally, irrespective of which account / strategy crossed the threshold. There is no per-account or per-strategy durable record produced on this path. Operators clearing a daily-loss auto-trip must therefore look up and clear the `GLOBAL` record (one durable trip blocks the whole hub), not a per-account scope.

See [Capital Limits Configuration](capital_limits_configuration.md) for daily-loss
and capital-limit sizing guidance.

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

`/readyz` exposes the following kill-switch fields in its JSON payload (Codex #256 round-1 P2 + round-3 P2 corrections — earlier versions misnamed the divergence field AND misdescribed `kill_switch_active_count` / `kill_switch_source`):

- `kill_switch_active_count` (int) — count of **actively-blocking** durable records, i.e. those in `TRIPPED` or `CLEAR_PENDING`. The counter explicitly excludes **both** `INACTIVE` **and** `CLEARED` (`app/risk/kill_switch.py:736-738`: `sum(1 for r in records if r.get("state") not in ("INACTIVE", "CLEARED"))`). Monitors that interpret this as "non-INACTIVE" will under-alert because a `CLEARED`-but-not-yet-rearmed record reports zero here even though the state-machine record is still present. On a `/readyz` failure path the field can also be `-1`, meaning durable state could not be verified (fail-closed sentinel).
- `kill_switch_source` (str) — provenance of the snapshot. Actual emitted values are `kill_switch_manager` (durable manager available and queried), `risk_manager` (durable manager unavailable, fell back to legacy in-memory state from a registered runner), or `unavailable` (neither source reachable — readyz fails with `reason="kill_switch_unavailable: ..."`). The string `durable` is **never** emitted; earlier wording was wrong. Monitors filtering on `kill_switch_source == "durable"` will match zero events.
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

`resource_type=kill_switch` covers `trip` / `request_clear` / `confirm_clear` / `rearm` / `set_block_exits`. **Cancel-all uses a different `resource_type` (Codex #256 round-1 P3 correction):** `/admin/kill-switch/cancel-all` emits `action=kill_switch_cancel_all` with `resource_type=broker_orders` (`app/dashboard/admin_routes.py:2632-2636`). **Break-glass flatten uses yet another (Codex #256 round-2 P2 correction)** — the endpoint emits `action=break_glass_flatten` with `resource_type=position` (`app/dashboard/admin_routes.py:1114-1118`), **not** `resource_type=break_glass`. Earlier runbook wording querying `resource_type=break_glass` was wrong; that query returns nothing. To reconstruct an end-to-end incident timeline, query all three of the actually-emitted resource types:

```http
GET /admin/audit?resource_type=kill_switch
GET /admin/audit?resource_type=broker_orders
GET /admin/audit?resource_type=position&action=break_glass_flatten
```

(The third query is filtered by `action=break_glass_flatten` because `resource_type=position` also covers other position-mutating audit events; the action filter narrows to the break-glass exits only.)

**Safety-page audit merge — current gap (Codex #256 round-5 P2 correction).** Earlier wording said the dashboard's Safety page already merges these three resource types into a single table. **That is only partially true today.** `frontend/src/pages/Safety.tsx` (lines 44-76) merges three audit feeds, but the third query is `action='break_glass'` — and the actual endpoint emits `action='break_glass_flatten'` with `resource_type='position'` (`app/dashboard/admin_routes.py:1114-1118`). The `action='break_glass'` query therefore returns zero rows, and break-glass flatten events do **not** appear on the Safety page Audit Trail. The page does correctly merge:

- `resource_type='kill_switch'` — trip / request_clear / confirm_clear / rearm / set_block_exits.
- `resource_type='broker_orders'` — `kill_switch_cancel_all`.

For break-glass-flatten audit during an incident, **do not rely on the Safety page**. Issue the explicit `GET /admin/audit?resource_type=position&action=break_glass_flatten` query above (or fetch via `gh api` / `curl` directly) to capture those entries until `Safety.tsx` is updated to query `action='break_glass_flatten'`. Note this in the incident timeline so the audit reconstruction is complete.

---

## HTTP API reference (authenticated, ADMIN role)

The calls below are listed in the order an operator must execute them during an incident response (Codex #256 round-4 P2 correction — earlier ordering placed `cancel-all` after `confirm-clear`/`rearm`, which would re-admit entries while pending broker orders were still on the book; per SOP Step 3a `cancel-all` is **mandatory before** `confirm-clear`).

```http
# Trip
POST /admin/kill-switch/trip
X-Admin-Key: <ADMIN_API_KEY>
{"scope": "GLOBAL", "scope_id": "GLOBAL", "reason": "Daily loss threshold exceeded", "block_exits": false}

# Query current state at any time (read-only, no state machine effect)
GET /admin/kill-switch/state
X-Admin-Key: <ADMIN_API_KEY>

# SOP Step 3a — Cancel all open broker orders BEFORE clearing
# (MANDATORY; drains working/pending broker orders so they cannot fill
# against fresh entries once confirm-clear re-admits them. PR #240,
# audit-emitting.) Precondition: the durable KillSwitchManager has a
# record in TRIPPED or CLEAR_PENDING covering the target scope —
# env-only GLOBAL_KILL=1 does NOT satisfy this gate.
POST /admin/kill-switch/cancel-all
X-Admin-Key: <ADMIN_API_KEY>
{"reason": "panic stop — strategy XYZ mis-firing", "broker_account_id": null}

# SOP Step 4 — Request a clear (TRIPPED → CLEAR_PENDING)
POST /admin/kill-switch/request-clear
X-Admin-Key: <ADMIN_API_KEY>
{"scope": "GLOBAL", "scope_id": "GLOBAL", "reason_code": "loss_resolved", "break_glass": false}

# SOP Step 4 — STEP 1 of confirm-clear — issue a kill_switch_clear step-up token (LIVE only)
# Codex #256 round-1 P1: confirm-clear is the transition that restores router
# entry eligibility, so it ALSO requires a dedicated step-up token (not just rearm).
POST /admin/step-up/issue
X-Admin-Key: <ADMIN_API_KEY>
{"action_class": "kill_switch_clear", "resource_id": "GLOBAL"}

# SOP Step 4 — STEP 2 of confirm-clear — Confirm the clear
# (CLEAR_PENDING → CLEARED — entries restored at the router).
# DO NOT call this before cancel-all above has completed cleanly,
# otherwise pending broker orders may fill against fresh entries.
POST /admin/kill-switch/confirm-clear
X-Admin-Key: <ADMIN_API_KEY>
{"scope": "GLOBAL", "scope_id": "GLOBAL", "step_up_token": "<CLEAR_TOKEN_ID>"}

# SOP Step 4 — STEP 1 of rearm — issue a separate kill_switch_rearm step-up token (LIVE only, 5-minute TTL, single-use, actor-bound)
POST /admin/step-up/issue
X-Admin-Key: <ADMIN_API_KEY>
{"action_class": "kill_switch_rearm", "resource_id": "GLOBAL"}

# SOP Step 4 — STEP 2 of rearm — Re-arm (CLEARED → INACTIVE — state-machine hygiene; router already unblocked at confirm-clear)
POST /admin/kill-switch/rearm
X-Admin-Key: <ADMIN_API_KEY>
{"scope": "GLOBAL", "scope_id": "GLOBAL", "step_up_token": "<REARM_TOKEN_ID>"}
```

Most mutations in this API block are audited under `resource_type=kill_switch` and persisted to Postgres immediately. The exceptions (Codex #256 round-1 P3 + round-2 P2 corrections): `/admin/kill-switch/cancel-all` emits `action=kill_switch_cancel_all` with `resource_type=broker_orders` (`app/dashboard/admin_routes.py:2632-2636`) because the cancelled artefacts are broker orders, not kill-switch state; and `/admin/break-glass/flatten` emits `action=break_glass_flatten` with `resource_type=position` (`app/dashboard/admin_routes.py:1114-1118`) — **not** `resource_type=break_glass` as earlier wording in this runbook claimed. Step-up token issuance and consumption are also audited (`resource_type=step_up_token`). When auditing a clear/rearm incident, query `resource_type=kill_switch` and `resource_type=broker_orders` and (if `break-glass/flatten` was used) `resource_type=position&action=break_glass_flatten` to get the full picture.

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
2. **If SOP Step 3a's "helper durable trip" was created so that `/admin/kill-switch/cancel-all` would pass its trip-before-cancel precondition** (see Step 3a above and "Manual — global kill switch via environment" below), that durable record is still `TRIPPED` and will continue to reject entries at the router even after the env var is removed (Codex #256 round-4 P2 correction). Clear the helper trip through the standard durable workflow before progressing — see "After clearing — env-only path cleanup" immediately below this list. If no helper durable trip was created (cancel-all was performed directly in the Angel One terminal as the last-resort option), skip this step.
3. Update the deployment environment to remove or unset `GLOBAL_KILL`.
4. Restart the backend:
   ```powershell
   docker compose -f .\docker-compose.live.single.yml restart backend
   ```
5. Verify in logs that startup validation passes and no kill switch activation is logged.
6. Confirm fresh entries flow through the order router without `ORDER_REJECTED_GLOBAL_KILL` **or** `ORDER_REJECTED_KILL_SWITCH_MANAGER` blocks. The latter is what surfaces if the helper durable trip from step 2 was missed and the durable record is still `TRIPPED`.

> **After clearing — env-only path cleanup (Codex #256 round-4 P2).** If you executed SOP Step 3a's "Option (a) — issue a durable `POST /admin/kill-switch/trip` first so cancel-all is permitted" while the original trip path was env-var-only, the durable record you created for that purpose is still `TRIPPED`. Removing `GLOBAL_KILL` and restarting does **not** clear it — `KillSwitchManager.load_state()` rehydrates the row at startup and the hub interceptor keeps rejecting entries with `ORDER_REJECTED_KILL_SWITCH_MANAGER`. Cleanup is the standard durable sequence, in this order (see the HTTP API block above for full request bodies):
>
> 1. `POST /admin/kill-switch/request-clear` — `TRIPPED → CLEAR_PENDING`.
> 2. Obtain a `kill_switch_clear` step-up token via `POST /admin/step-up/issue` (LIVE only).
> 3. `POST /admin/kill-switch/confirm-clear` — `CLEAR_PENDING → CLEARED`. Entries become eligible at the router at this transition.
> 4. Obtain a separate `kill_switch_rearm` step-up token via `POST /admin/step-up/issue` (LIVE only).
> 5. `POST /admin/kill-switch/rearm` — `CLEARED → INACTIVE`. **The helper record is now in the INACTIVE state — it is not deleted** (Codex #256 round-5 P3 correction). `KillSwitchManager.rearm` (`app/risk/kill_switch.py:440-463`) only transitions the in-memory record's `state` field to `KillSwitchState.INACTIVE`; `save_state` then upserts that row (still keyed on `(scope, scope_id)`) into `kill_switch_state` with `state='INACTIVE'`. On a subsequent process restart, `load_state` filters `WHERE state != 'INACTIVE'` (`app/risk/kill_switch.py:684-703`), so the row is **not** rehydrated into the in-memory manager next boot — but until that restart happens, `GET /admin/kill-switch/state` and the dashboard's kill-switch table will continue to list the helper record as an `INACTIVE` row. This is harmless (an INACTIVE record blocks nothing at the router and the next trip ceremony starts cleanly because `trip()` is permitted from `INACTIVE`); do not chase it as a residual issue during post-incident verification. If you want the Postgres row physically gone, run a manual `DELETE FROM kill_switch_state WHERE scope=:scope AND scope_id=:scope_id AND state='INACTIVE'` after rearm — this is optional and not part of the standard cleanup.
>
> If the helper durable trip was created at a non-GLOBAL scope (e.g. an account-scoped record to satisfy cancel-all for a specific broker account), use the matching `scope` / `scope_id` in each call above. Use the audit log (`GET /admin/audit?resource_type=kill_switch&action=kill_switch_trip`) to identify the record you created if you no longer remember its scope.

### Legacy path — persisted `RiskManager.kill_switch_activated` flag

**Codex #256 round-5 P2 correction.** Earlier runbook wording said the legacy `RiskManager.kill_switch_activated` flag is "reset on restart because it is in-memory" — **that is wrong**. The flag is persisted to `risk_positions.json` alongside the rest of the legacy risk-state snapshot: `RiskManager._persist_state` writes `"kill_switch_activated": self.kill_switch_activated` on every persist tick (`app/core/risk_manager.py:1300`), and `RiskManager._load_state` rehydrates it on startup (`app/core/risk_manager.py:1269`: `self.kill_switch_activated = bool(data.get("kill_switch_activated", False))`). Restarting the backend therefore does **not** clear a legacy auto-trip — the flag survives the restart and `/readyz` keeps reporting `kill_switch_legacy_active=true`. There are only two paths that actually flip the flag back to False:

1. **Automatic daily reset (IST midnight).** `RiskManager._reset_daily_if_new_day` (`app/core/risk_manager.py:1346-1362`) sets `kill_switch_activated = False` when the next process-observed mark of the day rolls over into a new IST date. This fires on the next risk-evaluation tick after midnight IST — it is **not** a restart side-effect and does not require operator action.
2. **Manual cleanup of `risk_positions.json` (incident-recovery only, audit-tracked separately).** Stop the backend, edit `risk_positions.json` to set `"kill_switch_activated": false`, restart. Capture the before/after JSON in the incident timeline because this path is not Phoenix-audited.

PR #231 bridges legacy auto-trips into the durable `KillSwitchManager`, so a present-day legacy auto-trip will also leave a `TRIPPED` durable record behind. Clearing the durable record alone does **not** flip the legacy flag — the durable workflow (`request-clear` / `confirm-clear` / `rearm`) only operates on the `kill_switch_state` table. Conversely, the legacy daily reset does not clear the durable record either. To fully recover from a legacy-driven auto-trip:

1. Confirm `kill_switch_legacy_active=false` in `/readyz` — either wait for the IST-midnight reset or edit `risk_positions.json` as above.
2. Run the standard durable clear/rearm sequence on the bridged `GLOBAL` record (HTTP API or dashboard). See "After clearing — env-only path cleanup" for the call ordering.

**Do not** rely on a process restart alone to recover from a legacy auto-trip; both stores are persisted and both must be cleared explicitly.

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
4. Verify no `RECONCILING` or `MANUAL_REVIEW` lifecycle records exist on **any** runner — these are the only states `request-clear` validates against (`app/dashboard/admin_routes.py:1769-1787`). **Codex #256 round-5 P2 correction: the validator is unconditionally global.** The `_validation_fn` iterates `hub._runners.values()`, walks each runner's `OrderLifecycle._position_records`, and rejects the clear if **any** record on **any** runner is in `RECONCILING` or `MANUAL_REVIEW` — there is no filter on the requested kill-switch scope. So even when the operator is clearing an account- or tenant-scoped record, a stale `MANUAL_REVIEW` record on an entirely unrelated account will still surface as `HTTP 409: Clear request denied: [...]`. The pre-clear check must therefore enumerate `RECONCILING` / `MANUAL_REVIEW` lifecycle records across **all** runners, not just the affected scope:

   ```sql
   -- All RECONCILING / MANUAL_REVIEW lifecycle records, regardless of which scope you intend to clear.
   SELECT scope_key, tenant_id, account_id, strategy_id, ownership_key,
          position_state, state_reason, updated_at
   FROM internal_position_records
   WHERE position_state IN ('RECONCILING', 'MANUAL_REVIEW')
   ORDER BY updated_at DESC;
   ```

   If any row is returned, `request-clear` will fail with HTTP 409 even if the row is on a different account than the kill switch being cleared. Resolve the row (`POST /state/clear-position-record` after operator review, or wait for reconciliation to converge) before retrying. `ORPHAN_REVIEW` ownership records are **not** enforced by this validator and must be reviewed separately if relevant.
5. Use the HTTP clear / rearm API or the dashboard. Do not manually update `kill_switch_state` for routine LIVE operation.
6. If the API is unavailable, hold the stack stopped and escalate to incident recovery. Any direct DB repair must be separately approved, captured as break-glass evidence, and followed by a restart plus `/readyz` validation.

---

## After clearing

- Verify new entry orders are accepted by the router (monitor logs for clean order flow).
- Confirm kill switch state is `INACTIVE` or absent from the audit log.
- Confirm position reconciliation is current. The `request-clear` validator only enforces `RECONCILING` / `MANUAL_REVIEW` lifecycle states (`app/dashboard/admin_routes.py:1769-1787`); `ORPHAN_REVIEW` ownership records are out of band but should still be reviewed and resolved before resuming automated live trading.
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
| 13:07:02 | Legacy auto-trip: `total_dd > limit` triggered `RiskManager.kill_switch_activated=True`. Per Codex #256 round-5 P2 the flag is also persisted to `risk_positions.json`, so it would have survived a restart — though the durable bridge gap (root cause 1 below) was the proximate cause of the 23-minute hub-side admit window. |
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
- [OCI LIVE Deployment](oci_live_deployment.md)
- [Capital Limits Configuration](capital_limits_configuration.md)
- [Orphan Review](resolve_orphan_review.md)
- `ARCHITECTURE.md` §12, §12.1, §15.4
