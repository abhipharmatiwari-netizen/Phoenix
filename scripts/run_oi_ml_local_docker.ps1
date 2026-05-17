Param(
    [switch]$Build,
    [switch]$NoWait
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$composeFile = Join-Path $repoRoot ".artifacts\docker-compose.oi-ml-local.yml"

function Get-EnvValue {
    param([Parameter(Mandatory = $true)][string]$Name)
    $value = [Environment]::GetEnvironmentVariable($Name, "Process")
    if (-not [string]::IsNullOrWhiteSpace($value)) { return $value }
    $value = [Environment]::GetEnvironmentVariable($Name, "User")
    if (-not [string]::IsNullOrWhiteSpace($value)) { return $value }
    $value = [Environment]::GetEnvironmentVariable($Name, "Machine")
    if (-not [string]::IsNullOrWhiteSpace($value)) { return $value }
    return ""
}

function Set-EnvIfMissing {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [string[]]$Aliases = @(),
        [string]$DefaultValue = ""
    )
    if (-not [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($Name, "Process"))) {
        return
    }
    foreach ($alias in $Aliases) {
        $value = Get-EnvValue -Name $alias
        if (-not [string]::IsNullOrWhiteSpace($value)) {
            Set-Item -Path "Env:$Name" -Value $value
            return
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($DefaultValue)) {
        Set-Item -Path "Env:$Name" -Value $DefaultValue
    }
}

function Assert-RequiredEnv {
    param([Parameter(Mandatory = $true)][string[]]$Names)
    $missing = @()
    foreach ($name in $Names) {
        if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name, "Process"))) {
            $missing += $name
        }
    }
    if ($missing.Count -gt 0) {
        throw "Missing required environment variables: $($missing -join ', '). Values were not printed."
    }
}

Set-Location $repoRoot

if (-not (Test-Path -LiteralPath $composeFile)) {
    throw "Compose file not found: $composeFile"
}

Set-EnvIfMissing -Name "CONTROL_PLANE_PG_HOST" -Aliases @("PGHOST") -DefaultValue "host.docker.internal"
Set-EnvIfMissing -Name "CONTROL_PLANE_PG_PORT" -Aliases @("PGPORT") -DefaultValue "5432"
Set-EnvIfMissing -Name "CONTROL_PLANE_PG_DB" -Aliases @("PGDATABASE")
Set-EnvIfMissing -Name "CONTROL_PLANE_PG_USER" -Aliases @("PGUSER")
Set-EnvIfMissing -Name "CONTROL_PLANE_PG_PASSWORD" -Aliases @("PGPASSWORD", "CONTROL_PLANE_PG_PASSWORD_HOST")
Set-EnvIfMissing -Name "CONTROL_PLANE_PG_SSLMODE" -Aliases @("PGSSLMODE") -DefaultValue "prefer"
Set-EnvIfMissing -Name "PHOENIX_LOCAL_PORT" -Aliases @() -DefaultValue "18080"

Assert-RequiredEnv -Names @(
    "CONTROL_PLANE_PG_HOST",
    "CONTROL_PLANE_PG_PORT",
    "CONTROL_PLANE_PG_DB",
    "CONTROL_PLANE_PG_USER",
    "CONTROL_PLANE_PG_PASSWORD",
    "CONTROL_PLANE_PG_SSLMODE",
    "PHOENIX_LOCAL_PORT"
)

$configArgs = @("compose", "-f", $composeFile, "config", "--quiet")
& docker @configArgs
if ($LASTEXITCODE -ne 0) {
    throw "docker compose config failed with exit code $LASTEXITCODE"
}

$upArgs = @("compose", "-f", $composeFile, "up", "-d")
if ($Build) {
    $upArgs += "--build"
}
& docker @upArgs
if ($LASTEXITCODE -ne 0) {
    throw "docker compose up failed with exit code $LASTEXITCODE"
}

if (-not $NoWait) {
    $readyUrl = "http://127.0.0.1:$env:PHOENIX_LOCAL_PORT/readyz"
    $deadline = (Get-Date).AddSeconds(120)
    $ready = $false
    while ((Get-Date) -lt $deadline) {
        try {
            $resp = Invoke-WebRequest -Uri $readyUrl -UseBasicParsing -TimeoutSec 5
            if ($resp.StatusCode -eq 200) {
                $ready = $true
                break
            }
        }
        catch {
            Start-Sleep -Seconds 3
        }
    }
    if (-not $ready) {
        docker compose -f $composeFile ps
        throw "/readyz did not return 200 within 120 seconds at $readyUrl"
    }
    Write-Host "Phoenix OI/ML local backend ready at $readyUrl"
}

docker compose -f $composeFile ps
