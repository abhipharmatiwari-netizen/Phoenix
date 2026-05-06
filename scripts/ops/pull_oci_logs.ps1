# pull_oci_logs.ps1
# -------------------------------------------------------------------------
# Pulls a fresh slice of phoenix-vm run logs from OCI via SSH-bastion and
# writes them to D:\0 AMP\Phoenix\ops\oci_log_pulls\<UTC-timestamp>\ so the
# Architect agent can review them.
#
# PREREQ: an active OCI bastion managed-ssh session whose ID is set below.
# PREREQ: SSH key at C:\Users\abhis\.oci\phoenix-vm.key (per user message).
#
# USAGE:
#   pwsh -ExecutionPolicy Bypass -File .\scripts\ops\pull_oci_logs.ps1 `
#        -BastionSessionOcid 'ocid1.bastionsession.oc1.ap-mumbai-1.<ID>' `
#        [-VmIp '10.0.2.83'] [-Hours 24]
#
# After it finishes it prints the dump folder path.  Paste that path back
# into the Cowork chat and the agent will read every file and produce the
# review (errors, full health, strategy/trade activity, infra signals).
# -------------------------------------------------------------------------

[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string]$BastionSessionOcid,

  [string]$KeyPath          = "$env:USERPROFILE\.oci\phoenix-vm.key",
  [string]$VmIp             = '10.0.2.83',
  [string]$VmUser           = 'opc',
  [string]$BastionHost      = 'host.bastion.ap-mumbai-1.oci.oraclecloud.com',
  [int]   $Hours            = 24,
  [string]$WorkspaceRoot    = 'D:\0 AMP\Phoenix'
)

$ErrorActionPreference = 'Stop'

# --- Sanity checks ----------------------------------------------------------
if (-not (Test-Path $KeyPath))      { throw "SSH key not found: $KeyPath" }
if (-not (Test-Path $WorkspaceRoot)) { throw "Workspace not found: $WorkspaceRoot" }

# --- Output folder ----------------------------------------------------------
$stamp   = (Get-Date -AsUTC -Format 'yyyyMMdd-HHmmss') + 'Z'
$dumpDir = Join-Path $WorkspaceRoot ("ops\oci_log_pulls\$stamp")
New-Item -ItemType Directory -Path $dumpDir -Force | Out-Null
Write-Host "Dumping to: $dumpDir" -ForegroundColor Cyan

# --- SSH command builder ----------------------------------------------------
$proxyCmd = "ssh -i `"$KeyPath`" -W %h:%p -p 22 ${BastionSessionOcid}@${BastionHost}"
$sshArgs  = @(
  '-i', $KeyPath
  '-o', "ProxyCommand=$proxyCmd"
  '-o', 'StrictHostKeyChecking=accept-new'
  '-p', '22'
  "${VmUser}@${VmIp}"
)

function Invoke-VmCommand {
  param(
    [Parameter(Mandatory)] [string]$RemoteCmd,
    [Parameter(Mandatory)] [string]$OutFile
  )
  $outPath = Join-Path $dumpDir $OutFile
  Write-Host "  -> $OutFile" -ForegroundColor DarkGray
  & ssh @sshArgs $RemoteCmd 2>&1 | Out-File -FilePath $outPath -Encoding utf8
  if ($LASTEXITCODE -ne 0) {
    Write-Warning "ssh exit $LASTEXITCODE for $OutFile"
  }
}

# --- Pulls ------------------------------------------------------------------
$since = "${Hours}h"

Write-Host "Pulling docker container logs (last $since) ..." -ForegroundColor Yellow
Invoke-VmCommand "sudo docker logs --since $since phoenix-oci-backend  2>&1 | tail -n 5000" 'docker_backend.log'
Invoke-VmCommand "sudo docker logs --since $since phoenix-oci-web      2>&1 | tail -n 2000" 'docker_web.log'
Invoke-VmCommand "sudo docker logs --since $since phoenix-oci-postgres 2>&1 | tail -n 2000" 'docker_postgres.log'

Write-Host "Pulling app file logs from /opt/phoenix/logs ..." -ForegroundColor Yellow
Invoke-VmCommand "sudo ls -la /opt/phoenix/logs/ 2>&1"                                                  'fs_logs_listing.txt'
Invoke-VmCommand "sudo find /opt/phoenix/logs -type f -mtime -2 -name '*.log*' -printf '%T@ %p\n' | sort -n" 'fs_recent_logs.txt'
Invoke-VmCommand "sudo bash -c 'cd /opt/phoenix/logs && tail -n 4000 \$(ls -1t */app*.log 2>/dev/null | head -3) 2>&1'" 'app_recent_tail.log'
Invoke-VmCommand "sudo bash -c 'cd /opt/phoenix/logs && grep -h -E \"ERROR|CRITICAL|Traceback|Exception|FATAL\" \$(ls -1t */app*.log 2>/dev/null | head -5) 2>/dev/null | tail -n 2000'" 'app_errors_only.log'
Invoke-VmCommand "sudo bash -c 'cd /opt/phoenix/logs && grep -h -E \"BAR CLOSED|signal fired|order placed|FILL|postback|P&L|pnl\" \$(ls -1t */app*.log 2>/dev/null | head -5) 2>/dev/null | tail -n 2000'" 'app_strategy_activity.log'
Invoke-VmCommand "sudo tail -n 200 /opt/phoenix/logs/cert-renewal.log 2>&1"                              'cert_renewal.log'

Write-Host "Pulling infra signals ..." -ForegroundColor Yellow
Invoke-VmCommand "sudo docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.RestartCount}}' 2>&1"     'docker_ps.txt'
Invoke-VmCommand "sudo docker stats --no-stream 2>&1"                                                    'docker_stats.txt'
Invoke-VmCommand "df -h 2>&1; echo '---'; free -h 2>&1; echo '---'; uptime 2>&1; echo '---'; top -bn1 | head -20" 'host_resources.txt'
Invoke-VmCommand "sudo journalctl --since '${Hours} hours ago' -p err --no-pager 2>&1 | tail -n 500"     'journal_errors.log'
Invoke-VmCommand "sudo dmesg -T 2>&1 | grep -iE 'oom|killed|out of memory' | tail -n 50"                 'dmesg_oom.log'

Write-Host "Probing Vultr proxy reachability ..." -ForegroundColor Yellow
Invoke-VmCommand "curl -sS -o /dev/null -w '%{http_code} %{time_total}s\n' --max-time 10 -x http://localhost:8888 https://apiconnect.angelone.in/ 2>&1; echo '---'; sudo docker exec phoenix-oci-backend curl -sS -o /dev/null -w 'backend->proxy %{http_code}\n' --max-time 10 -x \$HTTPS_PROXY https://apiconnect.angelone.in/ 2>&1" 'proxy_probe.txt'

Write-Host "Done." -ForegroundColor Green
Write-Host ""
Write-Host "Dump path:" -ForegroundColor Cyan
Write-Host "  $dumpDir"
Write-Host ""
Write-Host "Paste the dump path into the Cowork chat to trigger the review."
