# Release Evidence

This directory holds deployment and restore drill evidence records.

## Contents

| File | Type | Purpose |
|---|---|---|
| `restore_drill_TEMPLATE.md` | Template | Copy and fill for each restore drill |
| `restore_drill_YYYYMMDD.md` | Evidence artifact | Completed restore drill records (one per drill) |
| `admin_console_deployment_YYYYMMDD.md` | Evidence artifact | Completed admin/operator console deployment records |

Evidence bundles (JSON snapshots from authenticated
`GET /admin/release-evidence`) are generated at deploy time by
`scripts/capture_release_evidence.ps1` and written to this directory as
`<timestamp>-evidence.json`. They are not committed to version control by
default; attach them to the PR or deployment record per
`docs/runbooks/release_evidence.md`.

## How to capture a release evidence bundle

```powershell
# Collect the structured JSON bundle from a running LIVE stack.
# ADMIN_API_KEY must come from the approved operator secret path.
$bundle = curl.exe -s -H "X-Admin-Key: $env:ADMIN_API_KEY" `
    http://localhost/admin/release-evidence | ConvertFrom-Json
$bundle | ConvertTo-Json -Depth 10
```

Or use the bundled helper:

```powershell
.\scripts\capture_release_evidence.ps1
```

The helper redacts secret-like fields, writes a timestamped JSON file to
`docs/release-evidence/`, and prints the pass/fail result against the criteria
in `docs/runbooks/release_evidence.md`.

For the active Docker Desktop plus Vultr tunnel runtime, release approval also
requires compose config validation for `docker-compose.live.single.yml`,
backend-local `/readyz`, backend-local `/health/summary`, authenticated
`/admin/health/summary`, authenticated `/admin/release-evidence`, redacted
public `/readyz` and `/health/summary`, JSON `/health/alerts` and
`/health/mitigations`, blocked BFF diagnostic bypasses, mobile Playwright
smoke, nginx security header smoke, stale static-asset routing evidence,
frontend build-output secret scan, Postgres health, Phoenix DB backup
evidence, secret-permission validation, and disk-headroom evidence. Those
checks are listed in the release evidence runbook and should be captured in the
deployment record without committing real secrets.

OCI VM material is historical/restoration-only unless a future migration issue
explicitly reinstates OCI as the active target.

## Admin console deployment records

For admin/operator console releases, create
`admin_console_deployment_YYYYMMDD.md` after the controlled rollout. Include:

- Git commit and PR number.
- Deploy command used.
- Docker container health after deploy.
- Local and public health endpoint status.
- Authenticated admin smoke result.
- Nginx route/security smoke result.
- Mobile Playwright smoke result from the promoted build.
- Confirmation that public summaries remain redacted and BFF diagnostic
  bypasses remain blocked.
- Confirmation that no secrets were committed, logged, or captured.

## Restore drill records

After each restore drill, copy `restore_drill_TEMPLATE.md` to
`restore_drill_YYYYMMDD.md`, fill in every field, and commit the file.
An empty or template file does not satisfy the release gate.

See `docs/runbooks/restore_drill.md` for step-by-step drill procedures.
