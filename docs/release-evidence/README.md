# Release Evidence

This directory holds deployment and restore drill evidence records.

## Contents

| File | Type | Purpose |
|---|---|---|
| `restore_drill_TEMPLATE.md` | Template | Copy and fill for each restore drill |
| `restore_drill_YYYYMMDD.md` | Evidence artifact | Completed restore drill records (one per drill) |

Evidence bundles (JSON snapshots from `GET /admin/release-evidence`) are generated at
deploy time by `scripts/capture_release_evidence.ps1` and written to this directory as
`<timestamp>-evidence.json`. They are not committed to version control by default;
attach them to the PR or deployment record per `docs/runbooks/release_evidence.md`.

## How to capture a release evidence bundle

```powershell
# Collect the structured JSON bundle from a running LIVE stack
$bundle = curl.exe -s -H "X-Admin-Key: $env:ADMIN_API_KEY" `
    http://localhost/admin/release-evidence | ConvertFrom-Json
$bundle | ConvertTo-Json -Depth 10
```

Or use the bundled helper:

```powershell
.\scripts\capture_release_evidence.ps1
```

The helper writes a timestamped JSON file to `docs/release-evidence/` and prints
the pass/fail result against the criteria in `docs/runbooks/release_evidence.md`.

## Restore drill records

After each restore drill, copy `restore_drill_TEMPLATE.md` to
`restore_drill_YYYYMMDD.md`, fill in every field, and commit the file.
An empty or template file does not satisfy the release gate.

See `docs/runbooks/restore_drill.md` for step-by-step drill procedures.
