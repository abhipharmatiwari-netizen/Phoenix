param(
    [string]$VultrHost = "65.20.69.50",
    [string]$VultrUser = "phoenixproxy",
    [string]$KeyPath = "$env:USERPROFILE\.ssh\phoenix_vultr_proxy_workspace_ed25519",
    [int]$RemotePort = 18080,
    [int]$LocalPort = 80,
    [string]$LocalLivenessPath = "/nginx-health",
    [int]$RestartDelaySeconds = 5
)

$ErrorActionPreference = "Stop"

$createdNew = $false
$mutex = New-Object System.Threading.Mutex($false, "Local\PhoenixVultrReverseTunnel", [ref]$createdNew)
$hasMutex = $false
try {
    $hasMutex = $mutex.WaitOne(0)
    if (-not $hasMutex) {
        Write-Host "$(Get-Date -Format o) Another Phoenix Vultr reverse tunnel process is already running; exiting."
        exit 0
    }

if (-not (Test-Path -LiteralPath $KeyPath)) {
    throw "SSH key not found: $KeyPath"
}

while ($true) {
    $live = $false
    if (-not $LocalLivenessPath.StartsWith("/")) {
        $LocalLivenessPath = "/$LocalLivenessPath"
    }
    $localLivenessUrl = "http://127.0.0.1:${LocalPort}${LocalLivenessPath}"
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $localLivenessUrl -TimeoutSec 5
        $live = ($response.StatusCode -eq 200)
    } catch {
        $live = $false
    }

    if (-not $live) {
        Write-Host "$(Get-Date -Format o) Local Phoenix liveness $localLivenessUrl is not 200; retrying in $RestartDelaySeconds seconds."
        Start-Sleep -Seconds $RestartDelaySeconds
        continue
    }

    Write-Host "$(Get-Date -Format o) Starting reverse tunnel ${VultrUser}@${VultrHost}:127.0.0.1:${RemotePort} -> 127.0.0.1:${LocalPort}"
    & ssh.exe `
        -i $KeyPath `
        -N `
        -T `
        -o ExitOnForwardFailure=yes `
        -o BatchMode=yes `
        -o IdentitiesOnly=yes `
        -o ServerAliveInterval=30 `
        -o ServerAliveCountMax=3 `
        -o StrictHostKeyChecking=accept-new `
        -R "127.0.0.1:${RemotePort}:127.0.0.1:${LocalPort}" `
        "${VultrUser}@${VultrHost}"

    Write-Host "$(Get-Date -Format o) Reverse tunnel exited with code $LASTEXITCODE; restarting in $RestartDelaySeconds seconds."
    Start-Sleep -Seconds $RestartDelaySeconds
}
} finally {
    if ($hasMutex) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
}
