# Break-Glass Flatten Runbook

> **Current runtime note:** this action can place live risk-reducing orders.
> Verify the active Docker Desktop/Vultr backend health and authority path before
> use. OCI VM evidence is historical/restoration-only after 2026-06-29.

**Architecture reference:** §1 (Operating Modes), §3.4 (Ownership States), §15 (API control rules)

Use this runbook when a live position must be forcibly exited through an audited operator action, bypassing normal strategy-driven exit logic. This is an emergency path. Use it only when automated exits are unavailable or blocked.

> **LIVE pre-requisite:** `POST /admin/break-glass/flatten` requires a valid `step_up_token` in `TRADE_MODE=LIVE`. Issue one immediately before calling this endpoint (5-minute TTL, single-use):
> ```http
> POST /admin/step-up/issue
> X-Admin-Key: <ADMIN_API_KEY>
> {"action_class": "break_glass", "resource_id": "<contract-key>"}
> ```
> Use the returned `token_id` as `step_up_token` in the flatten request below.

## Purpose

Submit one emergency exposure-reducing exit for one known contract through the hub order router.

## Scope

This runbook applies to hub-authoritative Phoenix deployments with an active `AccountRunner`. It is not a routine exit path and is not a replacement for the kill switch, EOD exit, or orphan-review workflow.

---

## What break-glass flatten does

`POST /admin/break-glass/flatten` submits a real EXIT order for a single contract through the hub `OrderRouter` pipeline at `BREAK_GLASS` mutation priority.

The route:

1. Requires `ADMIN` role authentication.
2. Validates that a reason string is provided (required for audit trail).
3. Resolves the live contract from authoritative runtime `StateStore` positions.
4. Acquires the scope lock at `BREAK_GLASS` priority via the scope serializer.
5. Records a `break_glass_override_id` on the ownership record (`RELEASING` state with break-glass reason).
6. Submits the exit order through `OrderRouter.submit_order` with `strategy_id=break_glass_flatten`.
7. Emits a full audit event including before/after ownership and order snapshots.

The order still traverses the full `OrderRouter` pipeline (capital, risk, profit, circuit-breaker checks). Break-glass priority ensures it is not blocked by in-flight scope locks from normal strategy flows.

---

## When to use this

- A strategy is unable to exit a live position and automated exit logic is blocked.
- A position is stuck in `DEGRADED`, `RECONCILING`, or `EXIT_PENDING` and the scope lock must be released.
- An operator emergency requires immediate exposure reduction for a known contract.

Do **not** use this for routine exits. Use `POST /admin/manual-eod-exit` for end-of-day operator exits.

---

## Prerequisites

Before calling this endpoint:

- You know the exact contract identity: `underlying`, `expiry`, `strike`, `option_right`, `product_type`.
- You know the `tenant_id` and `broker_account_id`.
- An active `AccountRunner` is running for the target `broker_account_id`.
- An authoritative live position for the contract exists in the runtime `StateStore`.
- Backend-local `/readyz` and authenticated `/admin/health/summary` have been
  checked so position authority, schema, watchdog, and tracked-account state are
  understood before the emergency exit.
- You have `ADMIN` credentials (`ADMIN_API_KEY` via `X-Admin-Key`, or an authenticated admin JWT).
- In LIVE, you have a valid single-use BREAK_GLASS `step_up_token` obtained from `POST /admin/step-up/issue` (see header note above).

---

## Request

```http
POST /admin/break-glass/flatten
X-Admin-Key: <ADMIN_API_KEY>
Content-Type: application/json
X-Request-Id: <unique-id-for-idempotency-tracking>

{
  "tenant_id": "tenant-1",
  "broker_account_id": "A1",
  "underlying": "NIFTY",
  "expiry": "2026-03-27",
  "strike": "22500",
  "option_right": "CE",
  "product_type": "INTRADAY",
  "reason": "<required: brief reason for the emergency exit>",
  "step_up_token": "<required in LIVE>"
}
```

### Required fields

| Field | Description |
|---|---|
| `tenant_id` | Tenant identifier |
| `broker_account_id` | Broker account to exit against |
| `underlying` | Instrument underlying (e.g. `NIFTY`, `BANKNIFTY`) |
| `expiry` | Option expiry date (`YYYY-MM-DD`) |
| `strike` | Strike price as string |
| `option_right` | `CE` or `PE` |
| `product_type` | `INTRADAY` or `DELIVERY` |
| `reason` | Mandatory free-text reason; recorded in audit trail |
| `step_up_token` | Required in LIVE; token must be valid for BREAK_GLASS |

---

## Expected responses

### Success — order submitted

HTTP 200:

```json
{
  "status": "break_glass_flatten_submitted",
  "submitted": true,
  "override_id": "break_glass_20260327_143022_admin",
  "ownership_key": "...",
  "tenant_id": "tenant-1",
  "broker_account_id": "A1",
  "contract": "...",
  "reason": "...",
  "exit_reason": "BREAK_GLASS",
  "hub_order_id": "...",
  "order": { "status": "OPEN", ... },
  "before": { ... },
  "ownership": { ... }
}
```

Note: `submitted: true` means the exit order was accepted by the router and submitted to the broker. It does **not** mean the position is confirmed flat. Monitor lifecycle polling for terminal fill confirmation.

### Failure — position not found

HTTP 404 or 409: The contract was not found in the authoritative `StateStore`. Verify that the contract fields exactly match what the runtime holds. Check the `/admin/runners` output and logs.

### Failure — no active runner

HTTP 404: No active `AccountRunner` for the specified `broker_account_id`. The runner must be running before break-glass is possible.

### Failure — order rejected

HTTP 409: The order was rejected by the broker or a router interceptor. Review the `order` field in the response for the rejection reason. The ownership record will have been updated with the `break_glass_override_id`.

### Failure - missing or invalid step-up token

HTTP 403 in LIVE means the endpoint did not receive a usable `step_up_token`. Do not bypass this in production docs or env files. Hold the position under manual supervision and escalate to the operator process that can issue or approve a BREAK_GLASS token.

---

## After the call

1. **Check logs** for lifecycle polling evidence of a terminal fill or rejection.

2. **Verify position is flat** via the state store:
   ```http
   GET /admin/runners
   ```
   or via the dashboard WebSocket.

3. **Check the audit log** for the break-glass event:
   ```http
   GET /admin/audit
   ```
   Filter for `action=break_glass_flatten` and the `override_id` returned in the response.

4. **If the order is pending** — monitor lifecycle polling. Do not re-issue break-glass immediately; allow the lifecycle service to converge.

5. **If the order is rejected or failed** — check broker logs and the order response detail. The ownership record will remain in `RELEASING` state with the break-glass override recorded. Manual DB review or a second break-glass attempt may be required depending on the rejection cause.

---

## Rollback / recovery

Break-glass is a real broker exit and cannot be rolled back after submission. Recovery is operational:

- if no order was submitted, fix the request or token and retry only after revalidating broker state
- if an order was submitted but not terminal, wait for lifecycle convergence before sending another exit
- if the order was rejected, keep the scope blocked, capture evidence, and use orphan-review or manual broker action under incident control

---

## Ownership state after break-glass

The ownership record transitions to `RELEASING` with `break_glass_override_id` set. Once the lifecycle service confirms a terminal fill, the ownership store releases the lock. Until that happens, the scope remains in `RELEASING` and blocks fresh entries for the same contract.

---

## Evidence to keep

For any real break-glass event, record:

- timestamp and operator identity
- `override_id` from the response
- request payload (contract identity and reason)
- full response body
- lifecycle confirmation of terminal fill
- audit log excerpt showing `break_glass_flatten` event

---

## Related

- [OCI LIVE Deployment](oci_live_deployment.md)
- [Orphan Review](resolve_orphan_review.md)
- `ARCHITECTURE.md` §1, §3.4, §10
