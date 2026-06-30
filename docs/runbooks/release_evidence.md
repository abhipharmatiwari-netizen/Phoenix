# LIVE Release Evidence

## Purpose

Before approving a new LIVE backend release, the operator on duty must collect
and review evidence that the deployed runtime is live-safe. Docker health alone
is liveness evidence; it is not trading-readiness approval.

## Scope

This runbook applies to LIVE releases for the active deployment path. As of
2026-06-29, that path is the local Docker Desktop stack plus Vultr proxy/tunnel
sidecar. OCI VM material is historical/restoration-only unless a future
migration issue explicitly reinstates OCI as the active target. Cloud Run
material remains non-current unless a future deployment audit proves that model
is active.

## Preconditions

- The backend and web are deployed through `docker-compose.live.single.yml`.
- `phoenix-v9-vultr-tunnel` owns public access through Vultr.
- `ADMIN_API_KEY` is available only through the approved operator secret
  process, not from a repo env file.
- Backend-local `/readyz` is reachable from inside `phoenix-v9-backend`.
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
| Vultr tunnel sidecar | `phoenix-v9-vultr-tunnel` is healthy on nginx liveness when local/Vultr recovery is the active path |
| Public HTTPS domain | `/health` and `/login` are reachable; `/readyz` returns HTTP 200 only when trading readiness is green |
| Direct BFF diagnostic bypass | `/bff/health/summary`, `/bff/readyz`, and `/bff/dashboard/status` return 404 |
| Static asset routing | current `/static/*` bundle assets return the correct content type; stale `/static/*` paths return 404 instead of SPA HTML |
| Local secret inputs | SecretStore/Postgres deploy-value preflight passes without printing values |
| Host-header boundary | malformed `Host` values return HTTP 400 before admin/BFF auth handling |
| Phoenix DB backup automation | local PostgreSQL backup evidence is current before database-affecting maintenance |
| Disk headroom | local Docker/Desktop and Postgres storage have a safe free-space buffer |
| Cleanup and isolation | active image tags, rollback set, Vultr tunnel state, and local storage headroom are documented |
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
2. Deploy through the Docker Desktop runbook and verify the sidecar:
   ```powershell
   docker ps --filter "name=phoenix-v9" --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"
   docker ps --filter "name=phoenix-v9-vultr-tunnel" --format "table {{.Names}}\t{{.Image}}\t{{.Status}}"
   curl.exe -s -o NUL -w "readyz=%{http_code} health=%{http_code}" `
     --max-time 15 `
     https://app.phoenixtechnosolutions.in/readyz `
     https://app.phoenixtechnosolutions.in/health
   ```
3. Wait for Docker liveness, then verify trading readiness:
   ```powershell
   docker ps --filter "name=phoenix-v9" --format "table {{.Names}}\t{{.Image}}\t{{.Status}}"
   docker exec phoenix-v9-backend curl -sS http://localhost:8080/readyz
   docker exec phoenix-v9-backend curl -sS http://localhost:8080/health/summary
   curl.exe -sS http://localhost/readyz
   curl.exe -sS http://localhost/health/summary
   curl.exe -sS http://localhost/health/alerts
   curl.exe -sS http://localhost/health/mitigations
   ```
4. Validate local hardening invariants without printing secrets:
   ```powershell
   docker inspect phoenix-v9-backend --format '{{json .Config.Image}}'
   docker inspect phoenix-v9-web --format '{{json .Config.Image}}'
   docker inspect phoenix-v9-vultr-tunnel --format '{{json .State.Health.Status}}'
   docker system df
   ```
5. Verify malformed Host handling:
   ```powershell
   curl.exe -sk -H "Host: phoenix.invalid%2fadmin" `
     -o NUL -w "%{http_code}`n" `
     https://app.phoenixtechnosolutions.in/admin/health/summary
   curl.exe -sk -H "Host: phoenix.invalid%2fbff" `
     -o NUL -w "%{http_code}`n" `
     https://app.phoenixtechnosolutions.in/bff/health/summary
   ```
   Both commands should print `400`.
6. Collect the authenticated release bundle:
   ```powershell
   $adminKey = Get-Secret -Name ADMIN_API_KEY -AsPlainText
   curl.exe -sk -H "X-Admin-Key: $adminKey" `
     https://app.phoenixtechnosolutions.in/admin/health/summary
   curl.exe -sk -H "X-Admin-Key: $adminKey" `
     https://app.phoenixtechnosolutions.in/admin/release-evidence
   curl.exe -sk -o NUL -w "%{http_code}`n" `
     https://app.phoenixtechnosolutions.in/bff/health/summary
   Remove-Variable adminKey
   ```
7. Review every field against the pass criteria table.
8. If all criteria pass, record the generated timestamp in the deployment log.
9. If any criterion fails, roll back or hold the stack stopped and investigate.

The PowerShell helper remains available for local/operator workstations:

```powershell
.\scripts\capture_release_evidence.ps1 -BaseUrl http://localhost
```

By default, the helper captures full backend-local readiness with
`docker exec phoenix-v9-backend curl http://localhost:8080/readyz`; it does not
validate internal readiness fields from the public nginx `/readyz`, which is
redacted. For non-Docker or alternate container layouts, pass an explicit
backend-local readiness URL:

```powershell
.\scripts\capture_release_evidence.ps1 `
  -BaseUrl http://localhost `
  -ReadyzUrl http://127.0.0.1:8080/readyz
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
- That a restore from the latest Phoenix DB backup has been completed; restore
  proof comes from the restore drill runbook.

## Related

- [OCI LIVE Deployment](oci_live_deployment.md)
- [OCI Runtime Hardening](oci_runtime_hardening.md)
- [Phoenix Postgres Backup](postgres_backup.md)
- [Kill Switch](kill_switch.md)
- [Break-Glass Flatten](break_glass_flatten.md)
