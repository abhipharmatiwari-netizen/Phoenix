# Control Tower Runbook

Control Tower is split into read-only strategy visibility and audited
management mutations.

## Read-only status

These endpoints stay mounted even when management is disabled:

- `GET /api/control_tower/status`
- `GET /api/control_tower/matrix`

The response includes:

- active tenants in the caller entitlement scope
- active broker accounts with subscriptions
- enabled strategy config rows
- routed strategy IDs
- `capability`, including `read_only`, `mutation_enabled`,
  `management_disabled_reason`, and `blocking_reasons`

In LIVE, `DISABLE_CONTROL_TOWER_ROUTES=true` means management is
disabled for safety, not that read-only visibility disappears. The
dashboard must show Strategy Status and disabled toggles rather than a
raw 404 or "no strategies found" message.

## Mutation gate

`POST /api/control_tower/toggle` is accepted only when all conditions
are true:

- caller is entitled to the tenant/account scope
- request includes a non-empty `reason`
- in LIVE, `CONTROL_TOWER_MUTATIONS_ENABLED=true`
- `DISABLE_CONTROL_TOWER_ROUTES=false`
- durable kill switch has no active non-INACTIVE scopes
- legacy kill switch is inactive and not divergent from durable state
- runtime/schema/readiness checks are healthy
- broker position/order sync state is not stale

Every accepted mutation emits `toggle_control_tower` audit metadata
with actor, tenant, strategy, old/new value, broker account ids,
reason, request id, and timestamp. Failed mutations return
operator-readable blocker reasons.

## Frontend behavior

The dashboard calls `/api/control_tower/status` first. It renders
read-only Strategy Status whenever mutation gates are closed, and only
enables checkboxes when `capability.mutation_enabled=true`. Before a
toggle is sent, the UI collects an operator reason and includes it in
the request payload.

## Deployment note

For the OCI VM default, keep `DISABLE_CONTROL_TOWER_ROUTES=true` unless
there is an approved live-management change window. Enabling mutations
in LIVE requires both:

```text
DISABLE_CONTROL_TOWER_ROUTES=false
CONTROL_TOWER_MUTATIONS_ENABLED=true
```

Recheck `/readyz`, `/health/summary`, and `/api/control_tower/status`
before relying on enabled management controls.
