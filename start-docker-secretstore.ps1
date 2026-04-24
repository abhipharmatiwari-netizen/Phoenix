[CmdletBinding()]
param(
    [switch]$LaunchedFromClick
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$composeFile = Join-Path $repoRoot "docker-compose.live.single.yml"

if (-not $env:PHX_SECRETSTORE_BYPASS_RELAUNCH -and (Get-ExecutionPolicy) -ne "Bypass") {
    $relaunchArgs = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", $MyInvocation.MyCommand.Path
    )

    if ($LaunchedFromClick) {
        $relaunchArgs += "-LaunchedFromClick"
    }

    $env:PHX_SECRETSTORE_BYPASS_RELAUNCH = "1"
    try {
        & powershell.exe @relaunchArgs
        exit $LASTEXITCODE
    }
    finally {
        Remove-Item Env:PHX_SECRETSTORE_BYPASS_RELAUNCH -ErrorAction SilentlyContinue
    }
}

function Require-Command {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [string]$HelpMessage
    )

    if (-not (Get-Command -Name $Name -ErrorAction SilentlyContinue)) {
        if ($HelpMessage) {
            throw $HelpMessage
        }
        throw "Required command not found: $Name"
    }
}

function Require-Module {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [string]$HelpMessage
    )

    if (-not (Get-Module -ListAvailable -Name $Name)) {
        if ($HelpMessage) {
            throw $HelpMessage
        }
        throw "Required PowerShell module not found: $Name"
    }

    Import-Module $Name -ErrorAction Stop
}

function Export-RequiredSecretToEnv {
    param(
        [Parameter(Mandatory = $true)]
        [string]$SecretName,
        [Parameter(Mandatory = $true)]
        [string]$EnvName
    )

    $secretValue = Get-Secret -Name $SecretName -AsPlainText
    if ([string]::IsNullOrWhiteSpace($secretValue)) {
        throw "Secret '$SecretName' is empty."
    }

    Set-Item -Path "Env:$EnvName" -Value $secretValue
}

function Set-EnvFromSecretOrDefault {
    param(
        [Parameter(Mandatory = $true)]
        [string]$EnvName,
        [string]$SecretName = $EnvName,
        [string]$DefaultValue = ""
    )

    $currentValue = [Environment]::GetEnvironmentVariable($EnvName, "Process")
    if (-not [string]::IsNullOrWhiteSpace($currentValue)) {
        return
    }

    try {
        $secretValue = Get-Secret -Name $SecretName -AsPlainText -ErrorAction Stop
    }
    catch {
        $secretValue = ""
    }

    if (-not [string]::IsNullOrWhiteSpace($secretValue)) {
        Set-Item -Path "Env:$EnvName" -Value $secretValue
        return
    }

    if (-not [string]::IsNullOrWhiteSpace($DefaultValue)) {
        Set-Item -Path "Env:$EnvName" -Value $DefaultValue
        return
    }

    throw "Required value '$EnvName' is missing in both the current PowerShell session and SecretStore secret '$SecretName'."
}

function Invoke-External {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Description,
        [Parameter(Mandatory = $true)]
        [string[]]$Command
    )

    Write-Host ""
    Write-Host "==> $Description"

    $executable = $Command[0]
    $arguments = @()
    if ($Command.Count -gt 1) {
        $arguments = $Command[1..($Command.Count - 1)]
    }

    & $executable @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $($Command -join ' ')"
    }
}

try {
    Set-Location $repoRoot

    if (-not (Test-Path -LiteralPath $composeFile)) {
        throw "Compose file not found: $composeFile"
    }

    Require-Command -Name "docker" -HelpMessage "Docker CLI not found. Start Docker Desktop and ensure 'docker' is on PATH."
    Require-Module -Name "Microsoft.PowerShell.SecretManagement" -HelpMessage "Install the Microsoft.PowerShell.SecretManagement module before running this helper."
    Require-Module -Name "Microsoft.PowerShell.SecretStore" -HelpMessage "Install the Microsoft.PowerShell.SecretStore module before running this helper."
    Require-Command -Name "Unlock-SecretStore" -HelpMessage "Unlock-SecretStore is unavailable after loading modules."
    Require-Command -Name "Get-Secret" -HelpMessage "Get-Secret is unavailable after loading modules."

    $storeConfig = Get-SecretStoreConfiguration -ErrorAction SilentlyContinue
    if ($storeConfig -and $storeConfig.Authentication -ne "None") {
        $vaultPassword = Read-Host "Enter SecretStore password" -AsSecureString
        Unlock-SecretStore -Password $vaultPassword
    } else {
        Write-Host "SecretStore is configured without a password - skipping unlock."
    }

    Export-RequiredSecretToEnv -SecretName "ADMIN_API_KEY" -EnvName "ADMIN_API_KEY_HOST"
    Export-RequiredSecretToEnv -SecretName "DEMO_AUTH_TOKEN_SECRET" -EnvName "DEMO_AUTH_TOKEN_SECRET_HOST"
    Export-RequiredSecretToEnv -SecretName "CONTROL_PLANE_PG_PASSWORD" -EnvName "CONTROL_PLANE_PG_PASSWORD_HOST"
    Export-RequiredSecretToEnv -SecretName "CLIENT_LOCAL_IP" -EnvName "CLIENT_LOCAL_IP"
    Export-RequiredSecretToEnv -SecretName "CLIENT_PUBLIC_IP" -EnvName "CLIENT_PUBLIC_IP"
    Export-RequiredSecretToEnv -SecretName "MAC_ADDRESS" -EnvName "MAC_ADDRESS"

    Set-EnvFromSecretOrDefault -EnvName "CONTROL_PLANE_PG_HOST" -DefaultValue "host.docker.internal"
    Set-EnvFromSecretOrDefault -EnvName "CONTROL_PLANE_PG_PORT" -DefaultValue "5432"
    Set-EnvFromSecretOrDefault -EnvName "CONTROL_PLANE_PG_DB" -DefaultValue "phoenix"
    Set-EnvFromSecretOrDefault -EnvName "CONTROL_PLANE_PG_USER" -DefaultValue "phoenix_app"
    Set-EnvFromSecretOrDefault -EnvName "CONTROL_PLANE_PG_SSLMODE" -DefaultValue "prefer"
    # LIVE_PG_SSL_SKIP_CHECK must be set to "true" for local Docker deployments where
    # the host Postgres does not have SSL configured.  The compose default is now "false"
    # (requires SSL); local operators must set this explicitly.
    if (-not [Environment]::GetEnvironmentVariable("LIVE_PG_SSL_SKIP_CHECK", "Process")) {
        $env:LIVE_PG_SSL_SKIP_CHECK = "true"
        Write-Host "  [local-deploy] LIVE_PG_SSL_SKIP_CHECK=true (local Postgres without SSL)" -ForegroundColor Yellow
    }
    Set-EnvFromSecretOrDefault -EnvName "HUB_DEFAULT_TENANT_ID" -DefaultValue "tenant-1"
    Set-EnvFromSecretOrDefault -EnvName "HUB_DEFAULT_BROKER_ACCOUNT_ID" -DefaultValue "A1"

    $capitalLimitsJson = [Environment]::GetEnvironmentVariable("CAPITAL_LIMITS_JSON", "Process")
    if ([string]::IsNullOrWhiteSpace($capitalLimitsJson)) {
        try {
            $capitalLimitsJson = Get-Secret -Name "CAPITAL_LIMITS_JSON" -AsPlainText -ErrorAction Stop
        }
        catch {
            $capitalLimitsJson = ""
        }
    }

    if ([string]::IsNullOrWhiteSpace($capitalLimitsJson)) {
        $tenantId = [Environment]::GetEnvironmentVariable("HUB_DEFAULT_TENANT_ID", "Process")
        $brokerAccountId = [Environment]::GetEnvironmentVariable("HUB_DEFAULT_BROKER_ACCOUNT_ID", "Process")
        $capitalLimitsPayload = @{
            "$($tenantId):$($brokerAccountId)" = @{
                max_notional_per_order = 500000
                max_gross_exposure = 1000000
            }
        }
        $capitalLimitsJson = $capitalLimitsPayload | ConvertTo-Json -Compress

        $tradeModeEnv = [Environment]::GetEnvironmentVariable("TRADE_MODE", "Process")
        if ($tradeModeEnv -eq "LIVE") {
            # §98: Generic capital limits in LIVE require explicit operator sign-off.
            # We cannot silently proceed — the operator must confirm they understand
            # the account is either unfunded or has been explicitly risk-reviewed.
            Write-Host ""
            Write-Host "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!" -ForegroundColor Red
            Write-Host "  CAPITAL_LIMITS_JSON is not set for TRADE_MODE=LIVE     " -ForegroundColor Red
            Write-Host "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!" -ForegroundColor Red
            Write-Host ""
            Write-Host "  Generic 5L notional / 10L exposure baseline will be used for:" -ForegroundColor Yellow
            Write-Host "    Account: $($tenantId):$($brokerAccountId)" -ForegroundColor Yellow
            Write-Host ""
            Write-Host "  Generic limits are NOT audited per-account risk limits." -ForegroundColor Yellow
            Write-Host "  They are acceptable ONLY when:" -ForegroundColor Yellow
            Write-Host "    - The account has zero real capital at risk, OR" -ForegroundColor Yellow
            Write-Host "    - An operator has reviewed and approved the defaults for this account." -ForegroundColor Yellow
            Write-Host ""
            Write-Host "  To set account-specific limits, add CAPITAL_LIMITS_JSON to your" -ForegroundColor Cyan
            Write-Host "  PowerShell SecretStore or set it as an env var before running this script." -ForegroundColor Cyan
            Write-Host ""

            # Check for non-interactive / CI override
            $skipConfirm = [Environment]::GetEnvironmentVariable("ALLOW_LIVE_CAPITAL_LIMITS_DEFAULT_ONLY", "Process")
            if ($skipConfirm -eq "true") {
                Write-Host "  ALLOW_LIVE_CAPITAL_LIMITS_DEFAULT_ONLY=true found — skipping interactive prompt." -ForegroundColor Yellow
                Write-Host "  This is an audited exception; document justification in your deployment record." -ForegroundColor Yellow
            }
            else {
                $confirm = Read-Host "  Type YES to acknowledge and continue with generic limits"
                if ($confirm -ne "YES") {
                    Write-Error "Deployment cancelled. Set CAPITAL_LIMITS_JSON before deploying to a funded LIVE account."
                    exit 1
                }
            }
            Write-Host ""
            Write-Host "Setting ALLOW_LIVE_CAPITAL_LIMITS_DEFAULT_ONLY=true (explicit operator acknowledgement)."
            Set-Item -Path "Env:ALLOW_LIVE_CAPITAL_LIMITS_DEFAULT_ONLY" -Value "true"
        }
        else {
            Write-Host ""
            Write-Host "Derived CAPITAL_LIMITS_JSON for $($tenantId):$($brokerAccountId) using the bundled 5L/10L baseline."
            Write-Host "Override by setting the CAPITAL_LIMITS_JSON env var or SecretStore secret before launch."
        }
    }
    else {
        Write-Host ""
        Write-Host "Loaded CAPITAL_LIMITS_JSON from the current PowerShell session or SecretStore."
    }
    Set-Item -Path "Env:CAPITAL_LIMITS_JSON" -Value $capitalLimitsJson

    # Write secrets to temporary files for Docker secret mounts (Issue #56).
    # Files are written to a per-session temp directory under $env:TEMP.
    # Docker Compose reads them via the `secrets:` section in the compose file.
    # This prevents secrets from appearing in `docker inspect` environment output.
    $secretDir = Join-Path $env:TEMP "phx-secrets"
    New-Item -ItemType Directory -Force -Path $secretDir | Out-Null
    # Restrict directory to current user only (best-effort on Windows)
    try {
        $acl = Get-Acl $secretDir
        $acl.SetAccessRuleProtection($true, $false)
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
            [System.Security.Principal.WindowsIdentity]::GetCurrent().Name,
            "FullControl", "Allow"
        )
        $acl.AddAccessRule($rule)
        Set-Acl $secretDir $acl
    } catch {
        Write-Warning "Could not restrict secret dir permissions: $_"
    }
    $env:CONTROL_PLANE_PG_PASSWORD_HOST | Out-File -FilePath (Join-Path $secretDir "control_plane_pg_password") -Encoding utf8 -NoNewline
    $env:ADMIN_API_KEY_HOST             | Out-File -FilePath (Join-Path $secretDir "admin_api_key")             -Encoding utf8 -NoNewline
    $env:DEMO_AUTH_TOKEN_SECRET_HOST    | Out-File -FilePath (Join-Path $secretDir "demo_auth_token_secret")    -Encoding utf8 -NoNewline
    # Export the directory path so docker-compose can resolve ${PHX_SECRET_DIR}
    $env:PHX_SECRET_DIR = $secretDir
    Write-Host ""
    Write-Host "Docker secret files written to: $secretDir"
    Write-Host "  (admin_api_key, demo_auth_token_secret, control_plane_pg_password)"
    Write-Host "  These files are read by Docker Compose secrets - not baked into container env."

    Write-Host ""
    Write-Host "Loaded runtime values into the current PowerShell session:"
    foreach ($name in @(
        "CONTROL_PLANE_PG_HOST",
        "CONTROL_PLANE_PG_PORT",
        "CONTROL_PLANE_PG_DB",
        "CONTROL_PLANE_PG_USER",
        "CONTROL_PLANE_PG_SSLMODE",
        "CAPITAL_LIMITS_JSON",
        "HUB_DEFAULT_TENANT_ID",
        "HUB_DEFAULT_BROKER_ACCOUNT_ID"
    )) {
        $value = [Environment]::GetEnvironmentVariable($name, "Process")
        Write-Host ("  {0}={1}" -f $name, $value)
    }

    Write-Host ""
    Write-Host "Loaded secrets into session (also written to secret files):"
    Write-Host "  ADMIN_API_KEY_HOST"
    Write-Host "  DEMO_AUTH_TOKEN_SECRET_HOST"
    Write-Host "  CONTROL_PLANE_PG_PASSWORD_HOST"
    Write-Host "  CLIENT_LOCAL_IP"
    Write-Host "  CLIENT_PUBLIC_IP"
    Write-Host "  MAC_ADDRESS"

    Invoke-External -Description "Stopping existing LIVE stack" -Command @("docker", "compose", "-f", $composeFile, "down", "--remove-orphans")
    Invoke-External -Description "Starting LIVE stack" -Command @("docker", "compose", "-f", $composeFile, "up", "-d", "--build", "--force-recreate")
    Invoke-External -Description "Showing container status" -Command @("docker", "compose", "-f", $composeFile, "ps")

    # Docker Compose local secrets are bind-mounted files, not Swarm-managed copies.
    # Keep them for the lifetime of the stack so container restarts can still
    # read /run/secrets/*. Remove them only after `docker compose down`.
    Write-Host ""
    Write-Host "Secret files retained for container restart safety: $secretDir"
    Write-Host "  Remove this directory only after stopping the stack with docker compose down."
    Write-Host ""
    Write-Host "=== MANDATORY: Capture release evidence before approving this deployment ===" -ForegroundColor Yellow
    Write-Host "  Wait for the backend health check to pass, then run:" -ForegroundColor Yellow
    Write-Host "    .\scripts\capture_release_evidence.ps1" -ForegroundColor Cyan
    Write-Host "  Attach the output JSON to the deployment record / PR." -ForegroundColor Yellow
    Write-Host "  See docs/runbooks/release_evidence.md for pass criteria." -ForegroundColor Yellow
}
catch {
    Write-Error $_
    exit 1
}
