# One command from Windows: copies data and .env to the server and starts everything there.
# Usage (from the project folder):
#   powershell -ExecutionPolicy Bypass -File deploy\deploy.ps1 -Server root@203.0.113.10
param([Parameter(Mandatory = $true)][string]$Server)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
if (-not (Test-Path ".env")) { throw "No .env in the project folder (TELEGRAM_BOT_TOKEN=...)" }
if (-not (Test-Path "data")) { throw "No data folder in the project folder" }

Write-Host "1/2 Uploading data, .env and setup script (enter the server password if asked)"
scp -r data .env deploy/server-setup.sh "${Server}:/tmp/"
if ($LASTEXITCODE -ne 0) { throw "scp failed" }

Write-Host "2/2 Installing and starting on the server (password again if asked)"
ssh $Server "tr -d '\r' < /tmp/server-setup.sh > /tmp/bv-setup.sh && sh /tmp/bv-setup.sh"
if ($LASTEXITCODE -ne 0) { throw "setup on the server failed" }
