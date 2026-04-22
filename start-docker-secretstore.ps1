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
    Set-EnvFromSecretOrDefault -EnvName "CONTROL_PLANE_PG_SSLMODE" -DefaultValue "require"
    Set-EnvFromSecretOrDefault -EnvName "HUB_DEFAULT_TENANT_ID" -DefaultValue "tenant-1"
    Set-EnvFromSecretOrDefault -EnvName "HUB_DEFAULT_BROKER_ACCOUNT_ID" -DefaultValue "A1"

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

    # Clean up secret files after stack is running - they are now in the container.
    Write-Host ""
    Write-Host "Cleaning up temporary secret files from host..."
    Remove-Item -Path (Join-Path $secretDir "control_plane_pg_password") -ErrorAction SilentlyContinue
    Remove-Item -Path (Join-Path $secretDir "admin_api_key") -ErrorAction SilentlyContinue
    Remove-Item -Path (Join-Path $secretDir "demo_auth_token_secret") -ErrorAction SilentlyContinue
    Write-Host "  Secret files removed from $secretDir"
}
catch {
    Write-Error $_
    exit 1
}
