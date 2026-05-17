# Dashboard Kill Switch — Operator Guide

> **Current OCI VM note:** verify `phoenix-oci-backend` `/readyz` and
> `/health/summary` before relying on dashboard state. The dashboard is derived
> state; Postgres and backend health are authority.

**Issue:** #238 (admin dashboard kill-switch toggle on the Safety page).
**Related:** [`kill_switch.md`](kill_switch.md) (HTTP API reference),
[`break_glass_flatten.md`](break_glass_flatten.md) (post-trip flatten).

---

## Purpose

Give the on-call operator a one-click panic stop from the admin
dashboard when wrong, duplicated, or burst orders are detected. The
Safety page exposes the durable `KillSwitchManager` state machine and
the order-cancellation path without requiring shell access to the
OCI VM.

## When to use

| Symptom | First action |
| --- | --- |
| Strategy is placing orders that look obviously wrong (wrong side, wrong qty, wrong symbol). | **Trip SOFT** — blocks new entries; trailing-lock can still close. |
| Operator wants to immediately stop everything including exit attempts. | **Trip HARD** — blocks all orders; you'll have to manually flatten via broker UI. |
| Multiple unwanted broker orders are already open. | After tripping, **Cancel ALL Open Orders** to drain them. |
| Burst behaviour stopped, ready to resume normal trading. | **Request Clear → Confirm Clear → Rearm** (in that order). |

## Where it lives

Safety page (`/safety`), top-of-page card titled **Global Kill
Switch**. Only users with the **ADMIN** role see the controls; users
with READONLY/OPERATOR roles see the audit trail only.

## State machine

```
INACTIVE ──(Trip)──▶ TRIPPED ──(Request Clear)──▶ CLEAR_PENDING
   ▲                                                    │
   │                                                    ▼
   │                                                CLEARED
   │                                                    │
   │                                                    ▼
   │                                            (Rearm + step-up)
   │                                                    │
   └────────────────────────────────────────────────────┘
                       (INACTIVE)
```

**Important:** ``KillSwitchManager.trip()`` rejects any existing
non-INACTIVE record, so you cannot re-trip directly from CLEARED.
The correct sequence after a clear cycle is **Confirm Clear →
Rearm (returns to INACTIVE) → Trip** if another incident needs the
switch active again. The dashboard buttons surface only the
state-appropriate action.

The dashboard renders the current state as a coloured pill at the top
right of the panel:

- 🟩 **INACTIVE** — normal operation
- 🟥 **TRIPPED** — new orders blocked (HARD also blocks exits)
- 🟧 **CLEAR_PENDING** — clear requested, awaiting confirmation
- 🟦 **CLEARED** — confirmed, ready to rearm

## Required input

Every action prompts for an operator-entered **reason** (free-text,
non-empty). This reason is persisted to the `audit_events` table and
the JSONL audit log (`logs/audit_events.jsonl`) — both written
atomically by `emit_audit_event` on every toggle attempt.

Trip also exposes a checkbox for **HARD trip (block exits too)**.
Default is SOFT.

## Cancel ALL Open Orders

This is a separate destructive action. It does **not** trip the kill
switch — call **Trip** first if you also need to block new
placements. The cancel-all flow:

1. Enumerate every registered account runner (`hub.list_runner_ids()`)
2. For each runner, walk its `_last_orders` cache
3. Skip any order with a terminal status (`FILLED` / `CANCELLED` / etc.)
4. Call the broker adapter's `cancel_order(broker_order_id, symbol=...)`
   per remaining order
5. Aggregate per-account results into the audit event

The cancel call uses **message-based disambiguation** on broker
``REJECTED`` responses (PR #240 round-2/round-6 hardening):

- Messages explicitly indicating the order is already gone /
  cancelled / in a terminal state (e.g. ``order_not_found``,
  ``already_cancelled``, ``order completed``, ``terminal state``)
  are counted as ``skipped`` — operator double-clicks remain
  idempotent.
- Messages indicating the order was filled before the cancel
  landed (e.g. ``already filled``, ``already executed``) are
  counted as ``raced_filled`` and trigger an amber banner — the
  operator may need to manually flatten the new exposure.
- Every other ``REJECTED`` (e.g. ``cancel_failed:...`` from Angel's
  broker outage path) is counted as **failed** so the dashboard
  shows ``failed>0`` during a real incident.
- ``ERROR`` / ``FAILURE`` / ``FAILED`` responses are retried up to
  3× with brief backoff; if all attempts return non-terminal,
  they are counted as ``failed``.
- A broker exception (network outage, etc.) is also retried then
  counted as ``failed``.

The dashboard surfaces per-account success / failure under the panel
after the action completes:

```
attempted=3, cancelled=2, failed=0, skipped=1, raced_filled=0,
refresh_failures=0, out_of_scope=0
A1: ok       (att=2, ok=2, fail=0, skip=0, raced_filled=0)
A2: ok       (att=1, ok=0, fail=0, skip=1, raced_filled=0)
```

## Step-up token (LIVE only)

In LIVE mode both **Confirm Clear** and **Rearm** require a
single-use, 5-minute, actor-bound step-up token. PR #240 round-2/3
explicitly removed the dashboard auto-mint — the operator must
obtain a token via a **separate ceremony** and paste it into the
dialog. Auto-minting from the already-authenticated admin session
defeated the protection step-up was meant to add.

### How to obtain a step-up token (LIVE)

Tokens are bound to:
- the **actor** consuming them (i.e. the admin logged into the
  dashboard — use **your own** admin bearer token to issue, not a
  shared / different operator's token, otherwise the consume call
  will fail with an actor mismatch),
- the **action class** (`kill_switch_clear` for Confirm Clear,
  `kill_switch_rearm` for Rearm), and
- the **resource_id** (`GLOBAL` for the GLOBAL scope; a tenant id /
  account id for narrower scopes).

```bash
curl -X POST $PHOENIX/admin/step-up/issue \
     -H "Authorization: Bearer $YOUR_ADMIN_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"action_class": "kill_switch_clear", "resource_id": "GLOBAL"}'
# → {"token_id": "...", "expires_at": "...", "action_class": "kill_switch_clear"}
```

Paste the returned `token_id` into the Step-up token field in the
dashboard dialog. The dashboard surfaces an in-line copy of this
command alongside the input.

In **non-LIVE** (PAPER / SHADOW), the dashboard does **not** prompt
for a token — the backend accepts an empty value and the local
recovery flow proceeds without ceremony.

## LIVE durability

In LIVE mode (`TRADE_MODE=LIVE`):

- Startup **fails closed** if the durable kill-switch state cannot be
  loaded from Postgres (`app/runtime/app_runtime.py:872-878`).
- Every toggle is persisted to Postgres BEFORE the API call returns.
  If the persist fails, the dashboard receives **HTTP 500** with a
  clear message — the operator is never shown a phantom "tripped"
  state that would vanish on restart (#238 acceptance criterion).
- In non-LIVE modes (paper, dev, test) the persist failure is logged
  as a warning and the in-memory toggle still proceeds, so local /
  dev workflows can continue without a control-plane Postgres.

## Audit trail

The Safety page's lower table merges:

- `resource_type=kill_switch` events (trip, request-clear, confirm-clear, rearm)
- `resource_type=broker_orders` events (cancel-all bulk attempts)
- `action=break_glass` events (existing flatten flow)

Every toggle records `actor`, `timestamp`, `reason`, prior state,
requested state, broker-side per-account results, and a request id.
The events are queryable via `GET /admin/audit?resource_type=kill_switch`.

## Operator playbook — common scenarios

### Scenario 1: Strategy is mis-firing, exits are still safe

1. **Trip SOFT** with reason `"strategy XYZ mis-firing — bursts seen at 10:23"`.
2. Verify state pill shows **TRIPPED · SOFT**.
3. Optionally **Cancel ALL Open Orders** to drain pending entries.
4. Investigate. Once safe, **Request Clear → Confirm Clear → Rearm**.

### Scenario 2: Need to stop everything

1. **Trip HARD** with reason — confirm the HARD-trip checkbox before clicking.
2. Verify state pill shows **TRIPPED · HARD**.
3. **Cancel ALL Open Orders** with reason `"panic stop"`.
4. Manually flatten broker-side positions in your broker UI / phone.
5. Once positions are flat, **Request Clear → Confirm Clear → Rearm**.

### Scenario 3: Postgres outage during a trip

The dashboard will surface **HTTP 500 — Kill-switch state could not
be persisted**. The in-memory state is NOT changed. Recover the
control-plane Postgres connection and retry the trip; the call will
succeed when Postgres is reachable again.

If the control plane is broken AND you must immediately stop trading,
fall back to the existing CLI path documented in
[`kill_switch.md`](kill_switch.md) or set `GLOBAL_KILL=1` in the OCI
VM environment.

## Verification from health endpoints

After tripping you can confirm from a shell:

```bash
# Current durable state across all scopes
curl -H "X-Admin-Key: $ADMIN_KEY" https://$VM/admin/kill-switch/state

# Recent kill-switch audit events
curl -H "X-Admin-Key: $ADMIN_KEY" \
  "https://$VM/admin/audit?resource_type=kill_switch&limit=20"
```

Both endpoints back the dashboard panel; running them from outside
the dashboard validates that the UI and backend agree.
