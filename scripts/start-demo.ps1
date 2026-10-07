# Runs the Peykan simulator + MCP server in one process (Windows-friendly).
# Installs deps if needed, then starts `peykan demo --port <port>`.
param(
    [int]$Port = 6278
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here
Set-Location ..  # repo root

Write-Host "==> Installing dependencies (requirements.txt)..." -ForegroundColor Cyan
python -m pip install -r requirements.txt

Write-Host "==> Installing package (editable)..." -ForegroundColor Cyan
python -m pip install -e .

Write-Host "==> Starting simulator + MCP server on port $Port (one process)..." -ForegroundColor Cyan
peykan demo --port $Port
