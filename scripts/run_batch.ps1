# Scheduled refresh of the Salesforce docs index (Windows Task Scheduler).
# Action: powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\path\sfdocs-rag\scripts\run_batch.ps1
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
New-Item -ItemType Directory -Force -Path "$Root\data" | Out-Null
$Stamp = Get-Date -Format "yyyyMMdd"
& "$Root\.venv\Scripts\sfdocs.exe" --config "$Root\config.yaml" run-all 1>> "$Root\data\batch-$Stamp.json" 2>> "$Root\data\batch-errors.log"
