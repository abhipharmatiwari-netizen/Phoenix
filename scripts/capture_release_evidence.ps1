#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Capture and archive the mandatory LIVE release-evidence bundle.

.DESCRIPTION
    After a successful LIVE deployment, this script:
      1. Calls /readyz and /admin/release-evidence
      2. Validates every required field against the pass criteria
      3. Writes the bundle to docs/release-evidence/<timestamp>-evidence.json
      4. Prints a pass/fail summary

    The archived bundle is the operator approval artefact required by
    release-manifest.json and docs/runbooks/release_evidence.md.

    USAGE:
        .\scripts\capture_release_evidence.ps1 -AdminKey $env:ADMIN_API_KEY
        .\scripts\capture_release_evidence.ps1  # reads ADMIN_API_KEY from session
#>

param(
    [string]$AdminKey    = $env:ADMIN_API_KEY,
    [string]$BaseUrl     = "http://localhost",
    [string]$OutDir      = "$PSScriptRoot\..\docs\release-evidence",
    [string]$BackendContainer = "phoenix-oci-backend",
    [int]   $TimeoutSec  = 30
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
function Invoke-PhoenixApi {
    param([string]$Path)
    $headers = @{ "X-Admin-Key" = $AdminKey }
    try {
        $response = Invoke-RestMethod `
            -Uri "$BaseUrl$Path" `
            -Headers $headers `
            -TimeoutSec $TimeoutSec `
            -Method Get
        return $response
    }
    catch {
        Write-Error "API call to $Path failed: $_"
        throw
    }
}

function Redact-Evidence {
    param($Value)

    if ($null -eq $Value) {
        return $null
    }

    if ($Value -is [System.Collections.IDictionary]) {
        $copy = [ordered]@{}
        foreach ($key in $Value.Keys) {
            $name = [string]$key
            if ($name -match '(?i)(secret|password|token|api[_-]?key|admin[_-]?key|pin|totp|client[_-]?code|private[_-]?ip|public[_-]?ip|ocid)') {
                $copy[$name] = "<REDACTED>"
            } else {
                $copy[$name] = Redact-Evidence $Value[$key]
            }
        }
        return $copy
    }

    if ($Value -is [pscustomobject]) {
        $copy = [ordered]@{}
        foreach ($property in $Value.PSObject.Properties) {
            if ($property.Name -match '(?i)(secret|password|token|api[_-]?key|admin[_-]?key|pin|totp|client[_-]?code|private[_-]?ip|public[_-]?ip|ocid)') {
                $copy[$property.Name] = "<REDACTED>"
            } else {
                $copy[$property.Name] = Redact-Evidence $property.Value
            }
        }
        return [pscustomobject]$copy
    }

    if ($Value -is [System.Collections.IEnumerable] -and $Value -isnot [string]) {
        $items = @()
        foreach ($item in $Value) {
            $items += Redact-Evidence $item
        }
        return $items
    }

    return $Value
}

function Assert-Field {
    param([string]$Label, $Actual, $Expected, [string]$Op = "eq")
    $pass = switch ($Op) {
        "eq"  { $Actual -eq $Expected }
        "ne"  { $Actual -ne $Expected }
        "gte" { $Actual -ge $Expected }
        "true"{ [bool]$Actual }
    }
    $status = if ($pass) { "PASS" } else { "FAIL" }
    $color  = if ($pass) { "Green" } else { "Red" }
    Write-Host ("  [{0}] {1} = {2}" -f $status, $Label, $Actual) -ForegroundColor $color
    return $pass
}

# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------
if (-not $AdminKey) {
    Write-Error "ADMIN_API_KEY is not set. Export it or pass -AdminKey."
    exit 1
}

$null = New-Item -ItemType Directory -Force -Path $OutDir

# ---------------------------------------------------------------------------
# Collect evidence
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "=== Phoenix LIVE Release Evidence Capture ===" -ForegroundColor Cyan
Write-Host "Base URL : $BaseUrl"
Write-Host "Timestamp: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') IST"
Write-Host ""

Write-Host "Fetching /readyz ..." -ForegroundColor Yellow
$readyz = Invoke-PhoenixApi "/readyz"

Write-Host "Fetching /admin/release-evidence ..." -ForegroundColor Yellow
$evidence = Invoke-PhoenixApi "/admin/release-evidence"

# ---------------------------------------------------------------------------
# Validate required fields
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "--- Evidence validation ---" -ForegroundColor Cyan
$allPass = $true

$checks = @(
    @{ Label = "trade_mode == LIVE";                       Actual = $evidence.trade_mode;                                    Expected = "LIVE";   Op = "eq"   }
    @{ Label = "runtime_ready == true";                    Actual = $evidence.runtime_ready;                                 Expected = $true;    Op = "eq"   }
    @{ Label = "is_leader == true";                        Actual = $evidence.is_leader;                                     Expected = $true;    Op = "eq"   }
    @{ Label = "schema_guard.status == ok";                Actual = $evidence.schema_guard.status;                           Expected = "ok";     Op = "eq"   }
    @{ Label = "startup_recovery.status != failed";        Actual = $evidence.startup_recovery.status;                       Expected = "failed"; Op = "ne"   }
    @{ Label = "stream_worker.running == true";            Actual = $evidence.stream_worker.running;                         Expected = $true;    Op = "eq"   }
    @{ Label = "runner_count >= 1";                        Actual = $evidence.runner_count;                                  Expected = 1;        Op = "gte"  }
    @{ Label = "kill_switch.active_count == 0";            Actual = $evidence.kill_switch.active_count;                      Expected = 0;        Op = "eq"   }
    @{ Label = "capital_checks enabled";                   Actual = $evidence.safety_flags.enable_capital_checks;            Expected = $true;    Op = "eq"   }
    @{ Label = "risk_checks enabled";                      Actual = $evidence.safety_flags.enable_risk_checks;               Expected = $true;    Op = "eq"   }
    @{ Label = "outbox required";                          Actual = $evidence.safety_flags.order_submission_outbox_required;  Expected = $true;    Op = "eq"   }
    @{ Label = "ownership enabled";                        Actual = $evidence.safety_flags.position_ownership_enabled;        Expected = $true;    Op = "eq"   }
    @{ Label = "global kill switch router gate enabled";   Actual = $evidence.safety_flags.order_router_enforce_global_kill_switch; Expected = $true; Op = "eq"   }
    @{ Label = "risk_fail_open == false";                  Actual = $evidence.safety_flags.risk_fail_open_on_missing_pnl;    Expected = $false;   Op = "eq"   }
    @{ Label = "terminal position records net_qty == 0";   Actual = $evidence.position_record_invariants.terminal_nonzero_net_qty_count; Expected = 0; Op = "eq"   }
    @{ Label = "synthetic_contamination_cleared";          Actual = $evidence.synthetic_contamination_cleared;               Expected = $null;    Op = "true" }
    @{ Label = "/readyz ready == true";                    Actual = $readyz.ready;                                           Expected = $true;    Op = "eq"   }
    @{ Label = "degraded_scope_count == 0";                Actual = $readyz.degraded_scope_count;                            Expected = 0;        Op = "eq"   }
)

foreach ($c in $checks) {
    $pass = Assert-Field -Label $c.Label -Actual $c.Actual -Expected $c.Expected -Op $c.Op
    if (-not $pass) { $allPass = $false }
}

# ---------------------------------------------------------------------------
# Write bundle
# ---------------------------------------------------------------------------
$ts = Get-Date -Format "yyyyMMddTHHmmssZ"
$outFile = Join-Path $OutDir "$ts-evidence.json"

$bundle = [PSCustomObject]@{
    captured_at    = (Get-Date -Format "o")
    commit_sha     = (git -C "$PSScriptRoot\.." rev-parse HEAD 2>$null)
    backend_container = $BackendContainer
    image_id       = (docker inspect $BackendContainer --format "{{.Image}}" 2>$null)
    all_checks_pass = $allPass
    readyz          = $readyz
    release_evidence = $evidence
}

$redactedBundle = Redact-Evidence $bundle
$redactedBundle | ConvertTo-Json -Depth 20 | Set-Content -Path $outFile -Encoding UTF8
Write-Host ""
Write-Host "Bundle written to: $outFile" -ForegroundColor Cyan

# ---------------------------------------------------------------------------
# Final verdict
# ---------------------------------------------------------------------------
Write-Host ""
if ($allPass) {
    Write-Host "RESULT: ALL CHECKS PASSED — deployment approved" -ForegroundColor Green
    Write-Host "Attach $outFile to the deployment record / PR." -ForegroundColor Green
    exit 0
} else {
    Write-Host "RESULT: ONE OR MORE CHECKS FAILED — DO NOT APPROVE RELEASE" -ForegroundColor Red
    Write-Host "Review failures above, roll back if needed, and re-run after fix." -ForegroundColor Red
    exit 1
}
