param(
    [string]$TaskName = "Phoenix Vultr Reverse Tunnel",
    [string]$Delay = "PT120S"
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$tunnelScript = Join-Path $repoRoot "scripts\ops\start_vultr_reverse_tunnel.ps1"
$logDir = Join-Path $repoRoot ".artifacts\vultr_proxy"
$logPath = Join-Path $logDir "reverse_tunnel.task.log"

if (-not (Test-Path -LiteralPath $tunnelScript)) {
    throw "Tunnel script not found: $tunnelScript"
}

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$command = "& '$tunnelScript' *>> '$logPath'"
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -Command `"$command`"" `
    -WorkingDirectory $repoRoot

$trigger = New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
$trigger.Delay = $Delay

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Starts the Phoenix local-to-Vultr reverse SSH tunnel after Windows logon. The tunnel script waits for local Phoenix nginx liveness before connecting." `
    -Force | Out-Null

Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
