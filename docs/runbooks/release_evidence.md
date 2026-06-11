# LIVE Release Evidence

## Purpose

Before approving a new LIVE backend release, the operator on duty must collect
and review evidence that the deployed runtime is live-safe. Docker health alone
is liveness evidence; it is not trading-readiness approval.

## Scope

This runbook applies to the current OCI VM deployment. Docker Desktop and Cloud
Run material are non-current for production unless a future OCI VM audit proves
that deployment model is active.

## Preconditions

- The backend is deployed through the current OCI runbook.
- `ADMIN_API_KEY` is available only through the approved operator secret
  process, not from a repo env file.
- Backend-local `/readyz` is reachable from inside `phoenix-oci-backend`.
- Public nginx `/readyz` and `/health/summary` are expected to be redacted and
  are not substitutes for authenticated/internal diagnostics.

## Required Evidence

Collect all of the following without printing secret values:

| Evidence | Pass condition |
|---|---|
| Docker liveness | backend and web are healthy; Postgres reports Docker health `healthy` |
| Backend-local `/readyz` | HTTP 200 and `ready=true` |
| Backend-local `/health/summary` | status is acceptable for the release gate and operating mode is `HUB_AUTHORITATIVE` |
| Public `/readyz` | response is redacted and does not expose runner/account/lease internals |
| Public `/health/summary` | response is redacted; internal schema, watchdog, and tracked-account details are omitted or masked |
| Public `/health/alerts` and `/health/mitigations` | HTTP 200 JSON responses; neither endpoint returns SPA HTML |
| Authenticated `/admin/health/summary` | schema, watchdog, tracked-account, and readiness fields are present for the logged-in operator view |
| Direct BFF diagnostic bypass | `/bff/health/summary`, `/bff/readyz`, and `/bff/dashboard/status` return 404 |
| Static asset routing | current `/static/*` bundle assets return the correct content type; stale `/static/*` paths return 404 instead of SPA HTML |
| Secret permissions | `scripts/validate-live-secret-perms.sh` passes on the VM |
| Deploy env secret scan | `scripts/ops/check_env_secret_material.sh` passes without printing values |
| Host-header boundary | malformed `Host` values return HTTP 400 before admin/BFF auth handling |
| Watchdog contract | `docker inspect phoenix-oci-watchdog --format '{{json .Mounts}}'` returns an empty list |
| Disk headroom | root filesystem has safe free-space buffer |
| Cleanup and isolation | active image tags, rollback set, co-tenant workloads, and storage headroom are documented |
| Release evidence endpoint | authenticated `/admin/release-evidence` passes the criteria below |

## Release Evidence Fields

The authenticated `/admin/release-evidence` bundle must satisfy:

| Field | Pass condition |
|---|---|
| `trade_mode` | `"LIVE"` |
| `runtime_ready` | `true` |
| `is_leader` | `true` |
| `position_authority_restored` | `true` when restored position records exist; `false` is allowed only for a fresh database with zero non-terminal position records and green `/readyz` authority |
| `schema_guard.status` | `"ok"` or empty missing lists |
| `position_record_invariants.terminal_nonzero_net_qty_count` | `0` |
| `startup_recovery.status` | `"ok"` or `"skipped"`; never `"failed"` |
| `stream_worker.running` | `true` for automated LIVE unless a documented replacement plane is active |
| `runner_count` | at least `1` |
| `kill_switch.active_count` | `0` unless the release is intentionally held in a risk halt |
| `safety_flags.enable_capital_checks` | `true` |
| `safety_flags.enable_risk_checks` | `true` |
| `safety_flags.order_submission_outbox_required` | `true` |
| `safety_flags.position_ownership_enabled` | `true` |
| `safety_flags.order_router_enforce_global_kill_switch` | `true` |
| `safety_flags.risk_fail_open_on_missing_pnl` | `false` |
| `synthetic_contamination_cleared` | `true` |

Any field outside pass criteria is a deployment blocker unless an explicit
operator risk-halt or rollback decision is recorded.

## Collection Procedure

1. Run the smoke suite before deploy:
   ```bash
   pytest -m smoke -q
   ```
2. Deploy the new backend image via the active OCI deployment runbook.
3. Wait for Docker liveness, then verify trading readiness:
   ```bash
   docker ps --filter name=phoenix-oci --format "table {{.Names}}\t{{.Image}}\t{{.Status}}"
   docker exec phoenix-oci-backend curl -sS http://localhost:8080/readyz
   docker exec phoenix-oci-backend curl -sS http://localhost:8080/health/summary
   curl -sS http://localhost/readyz
   curl -sS http://localhost/health/summary
   curl -sS http://localhost/health/alerts
   curl -sS http://localhost/health/mitigations
   ```
4. Validate hardening invariants:
   ```bash
   sudo sh /opt/phoenix/app/scripts/validate-live-secret-perms.sh
   sudo PHOENIX_ROOT=/opt/phoenix \
     /opt/phoenix/app/scripts/ops/check_env_secret_material.sh
   docker inspect phoenix-oci-postgres --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}'
   docker inspect phoenix-oci-watchdog --format '{{json .Mounts}}'
   /opt/phoenix/app/scripts/ops/oci_storage_report.sh
   df -h /
   ```
5. Verify malformed Host handling:
   ```bash
   curl -sk -H 'Host: phoenix.invalid%2fadmin' \
     -o /dev/null -w "%{http_code}\n" \
     https://127.0.0.1:8443/admin/health/summary
   curl -sk -H 'Host: phoenix.invalid%2fbff' \
     -o /dev/null -w "%{http_code}\n" \
     https://127.0.0.1:8443/bff/health/summary
   ```
   Both commands should print `400`.
6. Collect the authenticated release bundle:
   ```bash
   ADMIN_KEY="$(sudo cat /run/secrets/admin_api_key)"
   curl -sk -H "X-Admin-Key: ${ADMIN_KEY}" \
     https://127.0.0.1:8443/admin/health/summary
   curl -sk -H "X-Admin-Key: ${ADMIN_KEY}" \
     https://127.0.0.1:8443/admin/release-evidence
   curl -sk -o /dev/null -w "%{http_code}\n" \
     https://127.0.0.1:8443/bff/health/summary
   unset ADMIN_KEY
   ```
7. Review every field against the pass criteria table.
8. If all criteria pass, record the generated timestamp in the deployment log.
9. If any criterion fails, roll back or hold the stack stopped and investigate.

The PowerShell helper remains available for local/operator workstations:

```powershell
.\scripts\capture_release_evidence.ps1 -BaseUrl http://localhost
```

The helper redacts secret-like fields before writing a bundle under
`docs/release-evidence/`.

## Fresh Database Note

On first deployment or after a clean database restore with no prior session
history, `position_authority_restored` can be `false` because no position
records exist in Postgres. This is not a blocker only when backend-local
`/readyz` is green and the deployment record explicitly states that the database
has zero non-terminal position records.

## What The Bundle Does Not Prove

- That strategies are calibrated for today's market conditions.
- That broker credentials are correct beyond observed login/sync evidence.
- That capital limits are economically appropriate.
- That previously exposed credentials have been rotated.

## Related

- [OCI LIVE Deployment](oci_live_deployment.md)
- [OCI Runtime Hardening](oci_runtime_hardening.md)
- [Kill Switch](kill_switch.md)
- [Break-Glass Flatten](break_glass_flatten.md)
