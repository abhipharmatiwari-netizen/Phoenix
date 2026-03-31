# Orphan Review Resolution Runbook

**Architecture reference:** §11.5 (Orphan and ambiguous-state workflow), §3.4 (Ownership states)

A contract enters `ORPHAN_REVIEW` when reconciliation cannot determine ownership conclusively and the scope has exceeded the ambiguity threshold. This state blocks fresh entries for the affected `OwnershipKey` until an operator makes an explicit decision.

---

## What `ORPHAN_REVIEW` means

The runtime holds an ownership record for a contract/account scope, but broker evidence and internal position state cannot be reconciled to a confident conclusion. Possible causes:

- Broker reports a position that the system has no authoritative record for.
- Internal position state and broker position data have diverged beyond reconciliation threshold.
- A crash occurred between order submission and durable lifecycle persistence.
- ATM remap or token refresh left the ownership scope in an unresolvable state.

---

## Operator decisions

`POST /admin/resolve-orphan-review` accepts one of four decisions:

| Decision | Meaning |
|---|---|
| `ADOPT` | Accept the broker-held position as a real live position and bring it under internal ownership. Requires provenance note in `reason`. |
| `FLATTEN` | Submit an emergency exit to flatten the broker-side position. Use only when confident the position is real and should be closed. |
| `SUPPRESS` | Suppress the orphan review for this scope — treat it as resolved without adopting or flattening. Use when the position is confirmed non-existent or already closed externally. |
| `CONTINUE_OBSERVING` | Keep the scope in `ORPHAN_REVIEW` and defer the decision. Entry blocking remains in effect. |

---

## Prerequisites

- You have identified the exact contract: `underlying`, `expiry`, `strike`, `option_right`, `product_type`.
- You have verified the broker's current position for this contract via the broker portal or a broker API call.
- You have reviewed the audit log and reconciliation evidence.
- You have `ADMIN` credentials.
- You have a clear documented reason for the decision.

---

## Request

```http
POST /admin/resolve-orphan-review
Authorization: Bearer <ADMIN_API_KEY>
Content-Type: application/json
X-Request-Id: <unique-id>

{
  "tenant_id": "tenant-1",
  "broker_account_id": "A1",
  "underlying": "NIFTY",
  "expiry": "2026-03-27",
  "strike": "22500",
  "option_right": "CE",
  "product_type": "INTRADAY",
  "decision": "ADOPT",
  "reason": "<required: documented basis for this decision>"
}
```

### Required fields

| Field | Description |
|---|---|
| `tenant_id` | Tenant identifier |
| `broker_account_id` | Broker account |
| `underlying`, `expiry`, `strike`, `option_right`, `product_type` | Contract identity — must match the orphaned scope exactly |
| `decision` | One of: `ADOPT`, `FLATTEN`, `SUPPRESS`, `CONTINUE_OBSERVING` |
| `reason` | Mandatory free-text reason; recorded in audit trail |

---

## Decision guidance

### `ADOPT`

Use when:
- The broker holds a real open position.
- The system lost track of it (e.g. crash during entry, remap failure, orphaned by restart).
- The position should be brought back under authoritative management.

After adoption:
- The position is recorded in authoritative state with provenance from the orphan resolution.
- Normal lifecycle polling and exit management resumes.
- Monitor for reconciliation convergence.

### `FLATTEN`

Use when:
- The broker holds a real open position.
- You have decided the safest action is to close it immediately.
- You do not want to adopt it into the system's authoritative state.

After flatten:
- An EXIT order is submitted.
- Monitor lifecycle polling for terminal fill confirmation.
- For positions where `FLATTEN` fails, consider using `break_glass_flatten.md`.

### `SUPPRESS`

Use when:
- You have confirmed the broker does NOT hold this position.
- The internal state was a ghost or stale entry.
- No broker-side action is needed.

After suppression:
- The ownership scope is released.
- Fresh entries for the contract become eligible again.

### `CONTINUE_OBSERVING`

Use when:
- Evidence is still incoming (e.g. pending broker sync, incomplete lifecycle convergence).
- You want to defer the decision without blocking operator access.

After this decision:
- The scope remains in `ORPHAN_REVIEW`.
- Fresh entries remain blocked.
- Set a reminder to revisit before EOD.

---

## Before making a decision — required checks

1. Check the broker portal or call the broker's positions API directly to confirm the broker's current position quantity and average price for the contract.

2. Review the audit log for the scope:
   ```http
   GET /admin/audit
   ```

3. Review active runners and state:
   ```http
   GET /admin/runners
   ```

4. If you are unsure, use `CONTINUE_OBSERVING` and escalate rather than making a destructive decision.

---

## Evidence to keep

For any orphan review resolution, record:

- timestamp and operator identity
- contract identity and broker-side evidence reviewed
- decision and reason
- audit log excerpt confirming the resolution event
- any lifecycle confirmation (for ADOPT and FLATTEN decisions)

---

## Related

- [Break-Glass Flatten](break_glass_flatten.md)
- `ARCHITECTURE.md` §11.5
