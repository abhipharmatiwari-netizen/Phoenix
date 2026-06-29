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

function Assert-Command {
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

function Assert-Module {
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

function Get-LiveDeployValuesFromPostgres {
    param(
        [Parameter(Mandatory = $true)]
        [string]$TenantId,
        [Parameter(Mandatory = $true)]
        [string]$BrokerAccountId
    )

    $helper = Join-Path $repoRoot "scripts\ops\export_live_deploy_values_from_postgres.py"
    if (-not (Test-Path -LiteralPath $helper)) {
        throw "Postgres deploy-value helper not found: $helper"
    }

    $output = & python $helper --tenant-id $TenantId --broker-account-id $BrokerAccountId 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to load deployment values from Postgres: $output"
    }
    try {
        return ($output | ConvertFrom-Json)
    }
    catch {
        throw "Postgres deploy-value helper returned invalid JSON."
    }
}

function Set-EnvFromValueOrThrow {
    param(
        [Parameter(Mandatory = $true)]
        [string]$EnvName,
        [string]$Value,
        [Parameter(Mandatory = $true)]
        [string]$Source
    )

    $currentValue = [Environment]::GetEnvironmentVariable($EnvName, "Process")
    if (-not [string]::IsNullOrWhiteSpace($currentValue)) {
        return
    }
    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "$EnvName is missing from $Source."
    }
    Set-Item -Path "Env:$EnvName" -Value $Value
}

function Assert-CapitalLimitsJsonHasAccountKey {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Json,
        [Parameter(Mandatory = $true)]
        [string]$TenantId,
        [Parameter(Mandatory = $true)]
        [string]$BrokerAccountId
    )

    try {
        $parsed = $Json | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw "CAPITAL_LIMITS_JSON is not valid JSON: $($_.Exception.Message)"
    }

    if ($null -eq $parsed) {
        throw "CAPITAL_LIMITS_JSON must be a JSON object containing account-specific limits."
    }

    $keys = @()
    foreach ($prop in $parsed.PSObject.Properties) {
        $keys += $prop.Name
    }

    $tenantAccountKey = "$($TenantId):$($BrokerAccountId)"
    if (($keys -notcontains $tenantAccountKey) -and ($keys -notcontains $BrokerAccountId)) {
        throw "CAPITAL_LIMITS_JSON must include account-specific limits for '$tenantAccountKey' or '$BrokerAccountId'."
    }
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

    Assert-Command -Name "docker" -HelpMessage "Docker CLI not found. Start Docker Desktop and ensure 'docker' is on PATH."
    Assert-Module -Name "Microsoft.PowerShell.SecretManagement" -HelpMessage "Install the Microsoft.PowerShell.SecretManagement module before running this helper."
    Assert-Module -Name "Microsoft.PowerShell.SecretStore" -HelpMessage "Install the Microsoft.PowerShell.SecretStore module before running this helper."
    Assert-Command -Name "Unlock-SecretStore" -HelpMessage "Unlock-SecretStore is unavailable after loading modules."
    Assert-Command -Name "Get-Secret" -HelpMessage "Get-Secret is unavailable after loading modules."

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
    # This launcher always targets docker-compose.live.single.yml. Set TRADE_MODE
    # in the host process so the pre-compose capital/risk gates cannot fall
    # through to non-LIVE defaults before Compose injects TRADE_MODE=LIVE.
    $env:TRADE_MODE = "LIVE"
    $env:BROKER_SECRET_BACKEND = "postgres"
    Set-EnvFromSecretOrDefault -EnvName "HUB_DEFAULT_TENANT_ID" -DefaultValue "tenant-1"
    Set-EnvFromSecretOrDefault -EnvName "HUB_DEFAULT_BROKER_ACCOUNT_ID" -DefaultValue "A1"
    $tenantId = [Environment]::GetEnvironmentVariable("HUB_DEFAULT_TENANT_ID", "Process")
    $brokerAccountId = [Environment]::GetEnvironmentVariable("HUB_DEFAULT_BROKER_ACCOUNT_ID", "Process")
    $postgresDeployValues = Get-LiveDeployValuesFromPostgres `
        -TenantId $tenantId `
        -BrokerAccountId $brokerAccountId
    Write-Host "Loaded deployment values from Postgres for $($tenantId):$($brokerAccountId)."
    if ($postgresDeployValues.broker_credentials_ready -ne $true) {
        throw "Postgres broker_credentials preflight did not confirm required broker secrets."
    }
    Write-Host "Verified Postgres broker_credentials for $($tenantId):$($brokerAccountId) (secret values not printed)."

    Set-EnvFromValueOrThrow `
        -EnvName "CLIENT_LOCAL_IP" `
        -Value $postgresDeployValues.client_local_ip `
        -Source "broker_credentials.client_local_ip for $($tenantId):$($brokerAccountId)"
    Set-EnvFromValueOrThrow `
        -EnvName "CLIENT_PUBLIC_IP" `
        -Value $postgresDeployValues.client_public_ip `
        -Source "broker_credentials.client_public_ip for $($tenantId):$($brokerAccountId)"
    Set-EnvFromValueOrThrow `
        -EnvName "MAC_ADDRESS" `
        -Value $postgresDeployValues.mac_address `
        -Source "broker_credentials.mac_address for $($tenantId):$($brokerAccountId)"

    $capitalLimitsJson = [Environment]::GetEnvironmentVariable("CAPITAL_LIMITS_JSON", "Process")
    if ([string]::IsNullOrWhiteSpace($capitalLimitsJson)) {
        $capitalLimitsJson = [string]$postgresDeployValues.capital_limits_json
        if (-not [string]::IsNullOrWhiteSpace($capitalLimitsJson)) {
            Write-Host "Loaded account-specific CAPITAL_LIMITS_JSON from Postgres broker_accounts.meta."
        }
    }

    if ([string]::IsNullOrWhiteSpace($capitalLimitsJson)) {
        $tradeModeEnv = [Environment]::GetEnvironmentVariable("TRADE_MODE", "Process")
        if ($tradeModeEnv -eq "LIVE") {
            # Section 98: Generic capital limits in LIVE require explicit operator sign-off.
            # We cannot silently proceed - the operator must confirm they understand
            # the account is either unfunded or has been explicitly risk-reviewed.
            Write-Host ""
            Write-Host "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!" -ForegroundColor Red
            Write-Host "  CAPITAL_LIMITS_JSON is not set for TRADE_MODE=LIVE     " -ForegroundColor Red
            Write-Host "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!" -ForegroundColor Red
            Write-Host ""
            Write-Host "  Account-specific capital limits are required for:" -ForegroundColor Yellow
            Write-Host "    Account: $($tenantId):$($brokerAccountId)" -ForegroundColor Yellow
            Write-Host ""
            Write-Host "  Add broker_accounts.meta.capital_limits in Postgres for this account." -ForegroundColor Cyan
            Write-Host "  Generic 5L/10L launcher defaults are not allowed for LIVE deployment." -ForegroundColor Cyan
            Write-Host ""
            Write-Error "Deployment cancelled. Set account-specific capital limits in Postgres before deploying LIVE."
            exit 1
        }
        else {
            $capitalLimitsPayload = @{
                "$($tenantId):$($brokerAccountId)" = @{
                    max_notional_per_order = 500000
                    max_gross_exposure = 1000000
                }
            }
            $capitalLimitsJson = $capitalLimitsPayload | ConvertTo-Json -Compress
            Write-Host ""
            Write-Host "Derived CAPITAL_LIMITS_JSON for $($tenantId):$($brokerAccountId) using the bundled 5L/10L baseline."
            Write-Host "Override by setting account-specific limits in Postgres before launch."
        }
    }
    else {
        Write-Host ""
        Assert-CapitalLimitsJsonHasAccountKey `
            -Json $capitalLimitsJson `
            -TenantId $tenantId `
            -BrokerAccountId $brokerAccountId
        Write-Host "Loaded account-specific CAPITAL_LIMITS_JSON from Postgres or an explicit env override (value redacted)."
    }
    Set-Item -Path "Env:CAPITAL_LIMITS_JSON" -Value $capitalLimitsJson

    # Issue #221 / PR #243 round-1 review P2: the LIVE compose files now
    # require ``RISK_MAX_DAILY_LOSS`` to be set explicitly (no fallback
    # default). The launcher must surface a clear error if the operator
    # did not pre-export it, instead of failing later inside ``docker
    # compose up`` with an opaque "variable is required" message. Also
    # forward the optional floor override so very small accounts can
    # lower the LIVE start-up floor without re-rolling the image.
    $tradeModeForRisk = [Environment]::GetEnvironmentVariable("TRADE_MODE", "Process")
    $riskMaxDailyLoss = [Environment]::GetEnvironmentVariable("RISK_MAX_DAILY_LOSS", "Process")
    if ([string]::IsNullOrWhiteSpace($riskMaxDailyLoss)) {
        $riskMaxDailyLoss = [string]$postgresDeployValues.risk_max_daily_loss
        if (-not [string]::IsNullOrWhiteSpace($riskMaxDailyLoss)) {
            Write-Host "Loaded RISK_MAX_DAILY_LOSS from Postgres broker_accounts.meta."
        }
    }
    if ([string]::IsNullOrWhiteSpace($riskMaxDailyLoss)) {
        if ($tradeModeForRisk -eq "LIVE") {
            Write-Host ""
            Write-Host "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!" -ForegroundColor Red
            Write-Host "  RISK_MAX_DAILY_LOSS is not set for TRADE_MODE=LIVE     " -ForegroundColor Red
            Write-Host "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!" -ForegroundColor Red
            Write-Host ""
            Write-Host "  The LIVE compose files require this explicitly (no fallback)." -ForegroundColor Yellow
            Write-Host "  Size per capital tier - see docs/runbooks/oci_live_deployment.md" -ForegroundColor Yellow
            Write-Host "  'Sizing the daily-loss limit by capital tier'. A common starting" -ForegroundColor Yellow
            Write-Host "  point for a INR 1-2L account is 10000." -ForegroundColor Yellow
            Write-Host ""
            Write-Error "Deployment cancelled. Set RISK_MAX_DAILY_LOSS in broker_accounts.meta or as an explicit env override and retry."
            exit 1
        }
        else {
            $riskMaxDailyLoss = "10000"
            Write-Host "RISK_MAX_DAILY_LOSS not set; using sample default 10000 for non-LIVE mode."
        }
    }
    else {
        Write-Host "Loaded RISK_MAX_DAILY_LOSS from the current PowerShell session or Postgres."
    }
    Set-Item -Path "Env:RISK_MAX_DAILY_LOSS" -Value $riskMaxDailyLoss

    $riskFloorOverride = [Environment]::GetEnvironmentVariable("RISK_MAX_DAILY_LOSS_LIVE_FLOOR", "Process")
    if (-not [string]::IsNullOrWhiteSpace($riskFloorOverride)) {
        Write-Host "Forwarding RISK_MAX_DAILY_LOSS_LIVE_FLOOR override = $riskFloorOverride into the container."
        Set-Item -Path "Env:RISK_MAX_DAILY_LOSS_LIVE_FLOOR" -Value $riskFloorOverride
    }

    # Write secrets to temporary files for Docker secret mounts (Issue #56).
    # Files are written to a per-session temp directory under $env:TEMP.
    # Docker Compose reads them via the `secrets:` section in the compose file.
    # This prevents secrets from appearing in `docker inspect` environment output.
    $secretDir = Join-Path $env:TEMP "phx-secrets"
    New-Item -ItemType Directory -Force -Path $secretDir | Out-Null

    # Reset the directory ACL to the current user with FullControl before
    # writing secret files.  A previous run may have applied a restrictive ACL
    # that blocks subsequent writes from the same user account.
    try {
        $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        $acl = New-Object System.Security.AccessControl.DirectorySecurity
        $acl.SetAccessRuleProtection($true, $false)
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
            $currentUser, "FullControl",
            "ContainerInherit,ObjectInherit", "None", "Allow"
        )
        $acl.AddAccessRule($rule)
        Set-Acl -Path $secretDir -AclObject $acl
    } catch {
        Write-Warning "Could not set secret dir permissions (continuing): $_"
    }

    # Helper: write a secret file, resetting its ACL if it already exists.
    function Write-SecretFile {
        param([string]$Path, [string]$Value)
        if (Test-Path $Path) {
            try {
                $fi = New-Object System.IO.FileInfo($Path)
                $acl = $fi.GetAccessControl()
                $acl.SetAccessRuleProtection($false, $true)
                $fi.SetAccessControl($acl)
            } catch {}
            Remove-Item -Path $Path -Force -ErrorAction SilentlyContinue
        }
        # PowerShell 5.1 [System.Text.Encoding]::UTF8 writes a BOM; use explicit
        # no-BOM encoder so Linux containers read the value without a leading U+FEFF.
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($Path, $Value, $utf8NoBom)
    }

    Write-SecretFile -Path (Join-Path $secretDir "control_plane_pg_password") -Value $env:CONTROL_PLANE_PG_PASSWORD_HOST
    Write-SecretFile -Path (Join-Path $secretDir "admin_api_key")             -Value $env:ADMIN_API_KEY_HOST
    Write-SecretFile -Path (Join-Path $secretDir "demo_auth_token_secret")    -Value $env:DEMO_AUTH_TOKEN_SECRET_HOST

    # Section 126 / Issue #5: ANGEL_POSTBACK_TOKEN - required for Angel broker order-status push
    # notifications (ANGEL_POSTBACK_AUTH_MODE=direct_broker in LIVE).  If not set,
    # lifecycle falls back to polling only; a WARNING is logged at startup.
    $angelPostbackToken = [Environment]::GetEnvironmentVariable("ANGEL_POSTBACK_TOKEN", "Process")
    if ([string]::IsNullOrWhiteSpace($angelPostbackToken)) {
        try {
            $angelPostbackToken = Get-Secret -Name "ANGEL_POSTBACK_TOKEN" -AsPlainText -ErrorAction Stop
        }
        catch {
            $angelPostbackToken = ""
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($angelPostbackToken)) {
        Write-SecretFile -Path (Join-Path $secretDir "angel_postback_token") -Value $angelPostbackToken
        Write-Host "  angel_postback_token: loaded from bootstrap secret/env" -ForegroundColor Green
    }
    else {
        # Write an empty file so Docker Compose secret mount succeeds.
        # Startup validator emits a WARNING (not error) when token is absent.
        Write-SecretFile -Path (Join-Path $secretDir "angel_postback_token") -Value ""
        Write-Host "  angel_postback_token: NOT configured - postbacks will return 401, polling fallback active" -ForegroundColor Yellow
        Write-Host "  Configure ANGEL_POSTBACK_TOKEN in the approved bootstrap secret path." -ForegroundColor Cyan
    }

    try {
        $killSwitchOverride = Get-Secret -Name "ADMIN_KILL_SWITCH_OVERRIDE" -AsPlainText -ErrorAction Stop
    }
    catch {
        $killSwitchOverride = ""
    }
    if ([string]::IsNullOrWhiteSpace($killSwitchOverride)) {
        throw "ADMIN_KILL_SWITCH_OVERRIDE must be configured in the approved bootstrap secret path before starting the live Docker stack."
    }
    Write-SecretFile -Path (Join-Path $secretDir "admin_kill_switch_override") -Value $killSwitchOverride
    Write-Host "  admin_kill_switch_override: loaded into file-only Docker secret" -ForegroundColor Green

    # Export the directory path so docker-compose can resolve ${PHX_SECRET_DIR}
    $env:PHX_SECRET_DIR = $secretDir
    Write-Host ""
    Write-Host "Docker secret files written to: $secretDir"
    Write-Host "  (admin_api_key, demo_auth_token_secret, control_plane_pg_password, angel_postback_token, admin_kill_switch_override)"
    Write-Host "  These files are read by Docker Compose secrets - not baked into container env."

    Write-Host ""
    Write-Host "Loaded runtime values into the current PowerShell session:"
    foreach ($name in @(
        "CONTROL_PLANE_PG_HOST",
        "CONTROL_PLANE_PG_PORT",
        "CONTROL_PLANE_PG_DB",
        "CONTROL_PLANE_PG_USER",
        "CONTROL_PLANE_PG_SSLMODE",
        "BROKER_SECRET_BACKEND",
        "CAPITAL_LIMITS_JSON",
        "HUB_DEFAULT_TENANT_ID",
        "HUB_DEFAULT_BROKER_ACCOUNT_ID"
    )) {
        $value = [Environment]::GetEnvironmentVariable($name, "Process")
        if ($name -eq "CAPITAL_LIMITS_JSON") {
            $value = if ([string]::IsNullOrWhiteSpace($value)) { "<missing>" } else { "<present: redacted>" }
        }
        Write-Host ("  {0}={1}" -f $name, $value)
    }

    Write-Host ""
    Write-Host "Loaded secrets into session (also written to secret files):"
    Write-Host "  ADMIN_API_KEY_HOST"
    Write-Host "  DEMO_AUTH_TOKEN_SECRET_HOST"
    Write-Host "  CONTROL_PLANE_PG_PASSWORD_HOST"
    Write-Host ""
    Write-Host "Loaded broker network identity from Postgres broker_credentials:"
    Write-Host "  CLIENT_LOCAL_IP"
    Write-Host "  CLIENT_PUBLIC_IP"
    Write-Host "  MAC_ADDRESS"

    # -----------------------------------------------------------------------
    # PHASE 1 - Tear down existing stack
    # -----------------------------------------------------------------------
    $deployStart = Get-Date
    Write-Host ""
    Write-Host "================================================================" -ForegroundColor Cyan
    Write-Host "  PHASE 1 - Stopping existing stack" -ForegroundColor Cyan
    Write-Host "================================================================" -ForegroundColor Cyan
    Invoke-External -Description "Stopping existing LIVE stack" `
        -Command @("docker", "compose", "-f", $composeFile, "down", "--remove-orphans")

    # -----------------------------------------------------------------------
    # PHASE 2 - Build images
    # Always rebuild so every deploy picks up the latest committed code.
    # Build through Compose so every service with a local build context is
    # tagged before Phase 3 starts with --no-build.
    # -----------------------------------------------------------------------
    Write-Host ""
    Write-Host "================================================================" -ForegroundColor Cyan
    Write-Host "  PHASE 2 - Building Docker images" -ForegroundColor Cyan
    Write-Host "================================================================" -ForegroundColor Cyan
    $buildStart = Get-Date
    Invoke-External -Description "Building Compose images (backend + nginx + vultr-tunnel)" `
        -Command @("docker", "compose", "-f", $composeFile, "build", "backend", "nginx", "vultr-tunnel")
    $buildSecs = [int]((Get-Date) - $buildStart).TotalSeconds
    Write-Host "  Build completed in ${buildSecs}s" -ForegroundColor Green

    # -----------------------------------------------------------------------
    # PHASE 3 - Start stack (migrator -> db-preflight -> backend -> nginx -> vultr-tunnel)
    # --no-build: images were already built in Phase 2; skip duplicate build.
    # --force-recreate: ensures containers pick up new image + config hash.
    # -----------------------------------------------------------------------
    Write-Host ""
    Write-Host "================================================================" -ForegroundColor Cyan
    Write-Host "  PHASE 3 - Starting LIVE stack" -ForegroundColor Cyan
    Write-Host "================================================================" -ForegroundColor Cyan
    Invoke-External -Description "Starting LIVE stack (migrator -> db-preflight -> backend -> nginx -> vultr-tunnel)" `
        -Command @("docker", "compose", "-f", $composeFile, "up", "-d", "--no-build", "--force-recreate")

    Write-Host ""
    Write-Host "  Secret files retained for container restart safety: $secretDir"
    Write-Host "  Remove this directory only after: docker compose down"

    # -----------------------------------------------------------------------
    # PHASE 4 - Wait for backend to become healthy
    # Docker's own backend health check uses /health with a 45 s start_period.
    # We poll docker inspect so the operator sees live progress.
    # -----------------------------------------------------------------------
    Write-Host ""
    Write-Host "================================================================" -ForegroundColor Cyan
    Write-Host "  PHASE 4 - Waiting for backend health check" -ForegroundColor Cyan
    Write-Host "================================================================" -ForegroundColor Cyan
    $healthTimeout  = 180   # seconds - allow full startup + universe build
    $healthInterval = 5     # poll every 5 s
    $elapsed        = 0
    $healthy        = $false

    while ($elapsed -lt $healthTimeout) {
        Start-Sleep $healthInterval
        $elapsed += $healthInterval
        $healthStatus = docker inspect phoenix-v9-backend --format "{{.State.Health.Status}}" 2>$null
        $containerStatus = docker inspect phoenix-v9-backend --format "{{.State.Status}}" 2>$null
        if ($containerStatus -eq "exited" -or $containerStatus -eq "dead") {
            Write-Host ""
            Write-Host "  FATAL: backend container exited unexpectedly." -ForegroundColor Red
            Write-Host "  Last 40 log lines:" -ForegroundColor Red
            $ErrorActionPreference = "Continue"
            docker logs phoenix-v9-backend --tail 40 2>&1
            $ErrorActionPreference = "Stop"
            throw "Backend container exited during startup. Check logs above."
        }
        if ($healthStatus -eq "healthy") {
            $healthy = $true
            break
        }
        $dots = "." * (($elapsed / $healthInterval) % 4 + 1)
        Write-Host ("  [{0,3}s / {1}s] status={2}{3}" -f $elapsed, $healthTimeout, $healthStatus, $dots)
    }

    if (-not $healthy) {
        Write-Host ""
        Write-Host "  TIMEOUT: backend did not become healthy within ${healthTimeout}s." -ForegroundColor Red
        Write-Host "  Last 50 log lines:" -ForegroundColor Red
        $ErrorActionPreference = "Continue"
        docker logs phoenix-v9-backend --tail 50 2>&1
        $ErrorActionPreference = "Stop"
        throw "Health check timeout after ${healthTimeout}s. Deployment FAILED."
    }

    $startupSecs = [int]((Get-Date) - $deployStart).TotalSeconds
    Write-Host ""
    Write-Host ("  Backend HEALTHY after {0}s total (build={1}s + startup={2}s)" -f `
        $startupSecs, $buildSecs, ($startupSecs - $buildSecs)) -ForegroundColor Green

    # -----------------------------------------------------------------------
    # PHASE 5 - Verify /readyz returns 200 and parse key fields
    # -----------------------------------------------------------------------
    Write-Host ""
    Write-Host "================================================================" -ForegroundColor Cyan
    Write-Host "  PHASE 5 - /readyz gate" -ForegroundColor Cyan
    Write-Host "================================================================" -ForegroundColor Cyan
    $readyzOk     = $false
    $readyzReason = "no response"
    $readyzBody   = $null

    for ($i = 0; $i -lt 8; $i++) {
        $readyzResp = $null
        try {
            $readyzResp = Invoke-WebRequest -Uri "http://localhost/readyz" `
                -UseBasicParsing -TimeoutSec 10 -ErrorAction Stop
        } catch {
            $readyzReason = "exception: $($_.Exception.Message)"
        }
        if ($null -ne $readyzResp -and $readyzResp.StatusCode -eq 200) {
            $readyzOk   = $true
            $readyzBody = $readyzResp.Content | ConvertFrom-Json -ErrorAction SilentlyContinue
            break
        }
        elseif ($null -ne $readyzResp) {
            $readyzReason = "HTTP $($readyzResp.StatusCode)"
            $bodyObj = $readyzResp.Content | ConvertFrom-Json -ErrorAction SilentlyContinue
            if ($bodyObj -and $bodyObj.reason) { $readyzReason = $bodyObj.reason }
        }
        Start-Sleep 3
    }

    if (-not $readyzOk) {
        Write-Host "  /readyz NOT OK - reason: $readyzReason" -ForegroundColor Red
        throw "/readyz did not return 200 within retry window. Reason: $readyzReason"
    }
    Write-Host "  /readyz: 200 OK" -ForegroundColor Green
    if ($readyzBody) {
        # Use PSObject.Properties for safe optional-field access in PS5.1.
        # Direct property access on ConvertFrom-Json objects throws when the
        # field is absent; NoteProperty lookup is always safe.
        $keyFields = @(
            "ready", "startup_recovery_status", "position_authority_restored",
            "startup_ownership_gap_detected", "kill_switch_active_count",
            "balance_sync_ready", "stream_worker_running",
            "running_runner_count", "degraded_scope_count"
        )
        $propMap = @{}
        foreach ($p in $readyzBody.PSObject.Properties) { $propMap[$p.Name] = $p.Value }
        foreach ($f in $keyFields) {
            if ($propMap.ContainsKey($f)) {
                $v = $propMap[$f]
                # Determine display color: red for problem values, green otherwise.
                # Use try/catch for numeric coercion to handle boolean fields safely.
                $numVal = 0
                try { $numVal = [int]"$v" } catch { $numVal = 0 }
                $isRed = ($f -match "gap|kill_switch_active|degraded" -and $numVal -gt 0) -or
                         ($f -eq "ready" -and "$v" -eq "False")
                $color = if ($isRed) { "Red" } else { "Green" }
                Write-Host ("    {0,-45} {1}" -f "${f}:", $v) -ForegroundColor $color
            }
        }
    }

    # -----------------------------------------------------------------------
    # PHASE 6 - Key startup events from backend log
    # Confirms every mandatory startup gate fired as expected.
    # -----------------------------------------------------------------------
    Write-Host ""
    Write-Host "================================================================" -ForegroundColor Cyan
    Write-Host "  PHASE 6 - Startup gate evidence (from container log)" -ForegroundColor Cyan
    Write-Host "================================================================" -ForegroundColor Cyan
    # docker logs writes container output to stderr; suppress strict-mode throws
    # by using a local Continue preference for this call only.
    $prevPref = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $logLines = docker logs phoenix-v9-backend 2>&1
    $ErrorActionPreference = $prevPref
    $patterns = @(
        "LIVE mode startup validation",
        "Schema guard passed",
        "startup.ssl_warning",
        "startup.angel_postback_token",
        "leader_lease.acquired",
        "startup.ownership_preload",
        "startup.recovery_pending_marked",
        "startup.kill_switch_loaded",
        "balance_sync.first_success",
        "startup.outbox_evaluated",
        "startup.runtime_ready",
        "startup.indicator_seed_complete",
        "Runtime exit routing mode"
    )
    foreach ($pat in $patterns) {
        $match = $logLines | Where-Object { $_ -match [regex]::Escape($pat) } | Select-Object -Last 1
        if ($match) {
            # Trim timestamp prefix for brevity
            $short = ($match -replace '^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} \[(\w+)\] [\w\.]+ :', '[$1]').Trim()
            $color = if ($match -match "\[WARNING\]") { "Yellow" } elseif ($match -match "\[ERROR\]") { "Red" } else { "Green" }
            Write-Host "  $short" -ForegroundColor $color
        } else {
            Write-Host "  [MISSING] $pat" -ForegroundColor Red
        }
    }

    # -----------------------------------------------------------------------
    # PHASE 7 - Container status snapshot
    # -----------------------------------------------------------------------
    Write-Host ""
    Write-Host "================================================================" -ForegroundColor Cyan
    Write-Host "  PHASE 7 - Container status" -ForegroundColor Cyan
    Write-Host "================================================================" -ForegroundColor Cyan
    Invoke-External -Description "Container status" `
        -Command @("docker", "compose", "-f", $composeFile, "ps")

    # -----------------------------------------------------------------------
    # PHASE 8 - Auto-capture release evidence
    # Calls /readyz + /admin/release-evidence, validates all fields,
    # and writes docs/release-evidence/<timestamp>-evidence.json.
    # -----------------------------------------------------------------------
    Write-Host ""
    Write-Host "================================================================" -ForegroundColor Cyan
    Write-Host "  PHASE 8 - Capturing release evidence" -ForegroundColor Cyan
    Write-Host "================================================================" -ForegroundColor Cyan
    $evidenceScript = Join-Path $repoRoot "scripts\capture_release_evidence.ps1"
    $adminKey = $env:ADMIN_API_KEY_HOST
    if (-not $adminKey) {
        # Try reading directly from the secret file (it's loaded into container env
        # but the session env var is ADMIN_API_KEY_HOST not ADMIN_API_KEY)
        $adminKeyFile = Join-Path $secretDir "admin_api_key"
        if (Test-Path $adminKeyFile) {
            $adminKey = (Get-Content $adminKeyFile -Raw).Trim()
        }
    }
    if ($adminKey -and (Test-Path $evidenceScript)) {
        try {
            & $evidenceScript -AdminKey $adminKey -BaseUrl "http://localhost"
        }
        catch {
            Write-Warning "Release evidence capture failed (non-fatal): $_"
            Write-Host "  Run manually: .\scripts\capture_release_evidence.ps1" -ForegroundColor Yellow
        }
    }
    else {
        Write-Host "  Skipping auto-capture (ADMIN_API_KEY not available in session)." -ForegroundColor Yellow
        Write-Host "  Run manually: .\scripts\capture_release_evidence.ps1" -ForegroundColor Yellow
    }

    # -----------------------------------------------------------------------
    # FINAL VERDICT
    # -----------------------------------------------------------------------
    $totalSecs = [int]((Get-Date) - $deployStart).TotalSeconds
    Write-Host ""
    Write-Host "================================================================" -ForegroundColor Green
    Write-Host "  DEPLOYMENT COMPLETE" -ForegroundColor Green
    Write-Host ("  Total time : {0}s (build {1}s + deploy {2}s)" -f `
        $totalSecs, $buildSecs, ($totalSecs - $buildSecs)) -ForegroundColor Green
    Write-Host "  Trade mode : LIVE" -ForegroundColor Green
    Write-Host "  Stack      : phoenix-live (backend + nginx)" -ForegroundColor Green
    Write-Host "  readyz     : 200 OK" -ForegroundColor Green
    Write-Host "================================================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "  GO - Phoenix is healthy and accepting orders." -ForegroundColor Green
    Write-Host ""
    Write-Host "  Next steps:" -ForegroundColor Cyan
    Write-Host "  1. Review release evidence in docs/release-evidence/" -ForegroundColor Cyan
    Write-Host "  2. Monitor logs: docker logs -f phoenix-v9-backend" -ForegroundColor Cyan
    Write-Host "  3. Check dashboard: http://localhost" -ForegroundColor Cyan
    Write-Host "  4. Confirm /readyz stays green through first 15 min of market open" -ForegroundColor Cyan
    Write-Host ""
    if ($LaunchedFromClick) {
        Write-Host "  Press any key to close this window (stack continues running)..." -ForegroundColor DarkGray
        $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
    }
}
catch {
    Write-Host ""
    Write-Host "================================================================" -ForegroundColor Red
    Write-Host "  DEPLOYMENT FAILED" -ForegroundColor Red
    Write-Host "================================================================" -ForegroundColor Red
    Write-Host ""
    Write-Error $_
    Write-Host ""
    Write-Host "  Troubleshooting:" -ForegroundColor Yellow
    Write-Host "  - docker logs phoenix-v9-backend --tail 100" -ForegroundColor Yellow
    Write-Host "  - docker compose -f docker-compose.live.single.yml ps" -ForegroundColor Yellow
    Write-Host "  - Check docs/runbooks/docker_desktop_live_deployment.md" -ForegroundColor Yellow
    Write-Host ""
    if ($LaunchedFromClick) {
        Write-Host "  Press any key to close (error details above)..." -ForegroundColor DarkGray
        $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
    }
    exit 1
}
