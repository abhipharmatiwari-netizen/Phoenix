# Release Evidence

This directory holds deployment and restore drill evidence records.

## Contents

| File | Type | Purpose |
|---|---|---|
| `restore_drill_TEMPLATE.md` | Template | Copy and fill for each restore drill |
| `restore_drill_YYYYMMDD.md` | Evidence artifact | Completed restore drill records (one per drill) |

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

For the current OCI VM, release approval also requires backend-local `/readyz`,
redacted public `/readyz`, Postgres health, watchdog no-mount evidence,
secret-permission validation, and disk-headroom evidence. Those checks are
listed in the release evidence runbook and should be captured in the deployment
record, not committed here by default.

## Restore drill records

After each restore drill, copy `restore_drill_TEMPLATE.md` to
`restore_drill_YYYYMMDD.md`, fill in every field, and commit the file.
An empty or template file does not satisfy the release gate.

See `docs/runbooks/restore_drill.md` for step-by-step drill procedures.
