$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repoRoot

$vaultPassword = Read-Host "Enter SecretStore password" -AsSecureString
Unlock-SecretStore -Password $vaultPassword

# Local development / PAPER mode only.
$env:ANGEL_API_KEY = Get-Secret -Name ANGEL_API_KEY -AsPlainText
$env:ANGEL_CLIENT_CODE = Get-Secret -Name ANGEL_CLIENT_CODE -AsPlainText
$env:ANGEL_CLIENT_PIN = Get-Secret -Name ANGEL_CLIENT_PIN -AsPlainText
$env:ANGEL_TOTP_SECRET = Get-Secret -Name ANGEL_TOTP_SECRET -AsPlainText
$env:CLIENT_LOCAL_IP = Get-Secret -Name CLIENT_LOCAL_IP -AsPlainText
$env:CLIENT_PUBLIC_IP = Get-Secret -Name CLIENT_PUBLIC_IP -AsPlainText
$env:MAC_ADDRESS = Get-Secret -Name MAC_ADDRESS -AsPlainText

$env:BROKER_SECRET_BACKEND = "env"
$env:TRADE_MODE = "PAPER"
$env:ENABLE_MULTI_HUB = "true"
$env:USE_HUB_ROUTER = "true"
$env:DISABLE_STREAM_WORKER = "false"
$env:ENABLE_DEMO_AUTH = "true"
$env:DASHBOARD_AUTH_DISABLED = "true"

.\.venv\Scripts\Activate.ps1
python -m app.main
